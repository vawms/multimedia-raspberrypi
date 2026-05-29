from __future__ import annotations

import csv
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RUNVAULT_DATA_DIR", BASE_DIR / "data")).resolve()
DB_PATH = Path(os.environ.get("RUNVAULT_DB_PATH", DATA_DIR / "runvault.sqlite3")).resolve()
STATIC_DIR = BASE_DIR / "static"
UPLOAD_TOKEN = os.environ.get("RUNVAULT_UPLOAD_TOKEN", "DEMO_UPLOAD_TOKEN")
ADMIN_TOKEN = os.environ.get("RUNVAULT_ADMIN_TOKEN", "DEMO_ADMIN_TOKEN")
MAX_UPLOAD_BYTES = int(os.environ.get("RUNVAULT_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))

ORIGINALS_DIR = DATA_DIR / "originals"
THUMBNAILS_DIR = DATA_DIR / "thumbnails"
DOWNLOADS_DIR = DATA_DIR / "downloads"
LOG_DIR = BASE_DIR / "logs"

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
TRACK_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
ALLOWED_CONTENT_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
    "application/octet-stream",
}

TRACKS = [
    ("rooftop", "Level 1", 10, 20_000, 10 * 60_000),
    ("crane", "Level 2", 20, 20_000, 10 * 60_000),
]

QUALITY_HEIGHTS = {
    "480p": 480,
    "240p": 240,
    "144p": 144,
}
DOWNLOAD_OPTIONS = {
    "formats": ["mp4"],
    "qualities": ["480p", "240p", "144p"],
    "fps": ["original", "30", "20"],
}

app = FastAPI(title="RunVault Replay Server", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ensure_directories() -> None:
    for path in (DATA_DIR, ORIGINALS_DIR, THUMBNAILS_DIR, DOWNLOADS_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


@contextmanager
def db() -> Any:
    ensure_directories()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    ensure_directories()
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracks (
              id TEXT PRIMARY KEY,
              display_name TEXT NOT NULL,
              sort_order INTEGER NOT NULL,
              min_time_ms INTEGER NOT NULL,
              max_time_ms INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY,
              username TEXT NOT NULL,
              completion_time_ms INTEGER NOT NULL,
              track_id TEXT NOT NULL REFERENCES tracks(id),
              original_filename TEXT NOT NULL,
              original_path TEXT NOT NULL,
              thumbnail_path TEXT,
              upload_time TEXT NOT NULL,
              status TEXT NOT NULL,
              file_size_bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS video_variants (
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
              format TEXT NOT NULL,
              quality TEXT NOT NULL,
              height INTEGER NOT NULL,
              fps TEXT NOT NULL,
              path TEXT,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              completed_at TEXT,
              error_message TEXT,
              UNIQUE(run_id, format, quality, fps)
            );
            """
        )
        existing_tracks = conn.execute("SELECT COUNT(*) AS count FROM tracks").fetchone()["count"]
        if existing_tracks == 0:
            conn.executemany(
                """
                INSERT INTO tracks (id, display_name, sort_order, min_time_ms, max_time_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                TRACKS,
            )


@app.on_event("startup")
def startup() -> None:
    init_db()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def require_upload_token(authorization: str | None = Header(default=None)) -> None:
    if not UPLOAD_TOKEN:
        return
    expected = f"Bearer {UPLOAD_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid upload token")


def require_admin_token(authorization: str | None = Header(default=None)) -> None:
    if not ADMIN_TOKEN:
        return
    expected = f"Bearer {ADMIN_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid admin token")


class TrackCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=32)
    display_name: str = Field(..., min_length=1, max_length=80)
    sort_order: int = Field(default=100, ge=0, le=10000)
    min_time_ms: int = Field(default=1, ge=1)
    max_time_ms: int = Field(default=600000, ge=1)


class TrackUpdate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=80)
    sort_order: int = Field(default=100, ge=0, le=10000)
    min_time_ms: int = Field(default=1, ge=1)
    max_time_ms: int = Field(default=600000, ge=1)


def validate_username(username: str) -> str:
    value = username.strip()
    if not USERNAME_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="Username must be 1-24 letters, numbers, underscores, or hyphens")
    return value


def validate_track_payload(track: TrackCreate | TrackUpdate) -> None:
    if isinstance(track, TrackCreate) and not TRACK_ID_RE.fullmatch(track.id):
        raise HTTPException(status_code=400, detail="Track id must be 1-32 lowercase letters, numbers, underscores, or hyphens")
    if track.min_time_ms > track.max_time_ms:
        raise HTTPException(status_code=400, detail="min_time_ms cannot be greater than max_time_ms")


def get_track_or_404(conn: sqlite3.Connection, track_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="Unknown track_id")
    return row


def validate_time(track: sqlite3.Row, completion_time_ms: int) -> None:
    if completion_time_ms <= 0:
        raise HTTPException(status_code=400, detail="completion_time_ms must be positive")
    if completion_time_ms < track["min_time_ms"] or completion_time_ms > track["max_time_ms"]:
        raise HTTPException(status_code=400, detail="completion_time_ms is outside the plausible range for this track")


def validate_upload_file(video: UploadFile) -> str:
    source_name = Path(video.filename or "").name
    extension = Path(source_name).suffix.lower()
    if extension not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Video extension must be one of {sorted(ALLOWED_VIDEO_EXTENSIONS)}")
    if video.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported video content type")
    return extension


def save_upload(video: UploadFile, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with destination.open("wb") as output:
        while True:
            chunk = video.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                output.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Uploaded video exceeds server limit")
            digest.update(chunk)
            output.write(chunk)
    if total == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded video is empty")
    return total, digest.hexdigest()


def run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=20 * 60)


def generate_thumbnail(run_id: str) -> None:
    with db() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return
        conn.execute("UPDATE runs SET status = ? WHERE id = ?", ("thumbnailing", run_id))

    output = THUMBNAILS_DIR / f"{run_id}.jpg"
    result = run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            run["original_path"],
            "-frames:v",
            "1",
            "-update",
            "1",
            "-vf",
            "scale='min(640,iw)':-2",
            str(output),
        ]
    )
    with db() as conn:
        if result.returncode == 0 and output.exists():
            conn.execute(
                "UPDATE runs SET status = ?, thumbnail_path = ?, error_message = NULL WHERE id = ?",
                ("ready", str(output), run_id),
            )
        else:
            conn.execute(
                "UPDATE runs SET status = ?, error_message = ? WHERE id = ?",
                ("thumbnail_failed", (result.stderr or "thumbnail generation failed")[-1200:], run_id),
            )


def build_transcode_args(source: str, output: str, quality: str, fps: str) -> list[str]:
    height = QUALITY_HEIGHTS[quality]
    vf_parts = [f"scale=-2:{height}"]
    if fps != "original":
        vf_parts.append(f"fps={int(fps)}")
    return [
        "ffmpeg",
        "-y",
        "-i",
        source,
        "-vf",
        ",".join(vf_parts),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "26",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        output,
    ]


def process_variant(variant_id: str) -> None:
    with db() as conn:
        variant = conn.execute(
            """
            SELECT video_variants.*, runs.original_path
            FROM video_variants
            JOIN runs ON runs.id = video_variants.run_id
            WHERE video_variants.id = ?
            """,
            (variant_id,),
        ).fetchone()
        if variant is None:
            return
        conn.execute("UPDATE video_variants SET status = ?, error_message = NULL WHERE id = ?", ("processing", variant_id))

    output = DOWNLOADS_DIR / f"{variant_id}.{variant['format']}"
    result = run_ffmpeg(build_transcode_args(variant["original_path"], str(output), variant["quality"], variant["fps"]))
    with db() as conn:
        if result.returncode == 0 and output.exists() and output.stat().st_size > 0:
            conn.execute(
                """
                UPDATE video_variants
                SET status = ?, path = ?, completed_at = ?, error_message = NULL
                WHERE id = ?
                """,
                ("ready", str(output), utc_now(), variant_id),
            )
        else:
            output.unlink(missing_ok=True)
            conn.execute(
                """
                UPDATE video_variants
                SET status = ?, completed_at = ?, error_message = ?
                WHERE id = ?
                """,
                ("failed", utc_now(), (result.stderr or "transcode failed")[-1200:], variant_id),
            )


def public_run(row: sqlite3.Row) -> dict[str, Any]:
    item = row_to_dict(row)
    item.pop("original_path", None)
    item.pop("thumbnail_path", None)
    item.pop("sha256", None)
    item["original_url"] = f"/media/{row['id']}/original"
    item["thumbnail_url"] = f"/media/{row['id']}/thumbnail.jpg" if row["thumbnail_path"] else None
    return item


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "database": str(DB_PATH),
        "data_dir": str(DATA_DIR),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }


@app.get("/api/tracks")
def tracks() -> dict[str, Any]:
    with db() as conn:
        rows = conn.execute("SELECT id, display_name, sort_order FROM tracks ORDER BY sort_order").fetchall()
    return {"tracks": [row_to_dict(row) for row in rows]}


@app.post("/api/runs")
def create_run(
    background_tasks: BackgroundTasks,
    username: str = Form(...),
    completion_time_ms: int = Form(...),
    track_id: str = Form(...),
    video: UploadFile = File(...),
    _: None = Depends(require_upload_token),
) -> JSONResponse:
    username = validate_username(username)
    extension = validate_upload_file(video)

    with db() as conn:
        track = get_track_or_404(conn, track_id)
        validate_time(track, completion_time_ms)

    run_id = uuid.uuid4().hex
    destination = ORIGINALS_DIR / f"{run_id}{extension}"
    size, sha256 = save_upload(video, destination)

    with db() as conn:
        conn.execute(
            """
            INSERT INTO runs (
              id, username, completion_time_ms, track_id, original_filename, original_path,
              upload_time, status, file_size_bytes, sha256
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                username,
                completion_time_ms,
                track_id,
                Path(video.filename or destination.name).name,
                str(destination),
                utc_now(),
                "uploaded",
                size,
                sha256,
            ),
        )
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    background_tasks.add_task(generate_thumbnail, run_id)
    return JSONResponse(status_code=201, content={"run": public_run(row)})


@app.get("/api/leaderboard")
def leaderboard(track_id: str | None = None, search: str | None = None) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if track_id:
        clauses.append("runs.track_id = ?")
        params.append(track_id)
    if search:
        clauses.append("runs.username LIKE ?")
        params.append(f"%{search.strip()}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT runs.*, tracks.display_name AS track_name
            FROM runs
            JOIN tracks ON tracks.id = runs.track_id
            {where}
            ORDER BY runs.completion_time_ms ASC, runs.upload_time ASC
            LIMIT 200
            """,
            params,
        ).fetchall()
    return {"runs": [public_run(row) for row in rows]}


@app.get("/api/leaderboard.csv")
def leaderboard_csv(track_id: str | None = None, search: str | None = None) -> Response:
    data = leaderboard(track_id, search)["runs"]
    lines: list[str] = []
    writer = csv.DictWriter(
        _ListWriter(lines),
        fieldnames=["rank", "username", "track_id", "track_name", "completion_time_ms", "upload_time", "status"],
    )
    writer.writeheader()
    for rank, run in enumerate(data, start=1):
        writer.writerow(
            {
                "rank": rank,
                "username": run["username"],
                "track_id": run["track_id"],
                "track_name": run["track_name"],
                "completion_time_ms": run["completion_time_ms"],
                "upload_time": run["upload_time"],
                "status": run["status"],
            }
        )
    return Response("".join(lines), media_type="text/csv")


class _ListWriter:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def write(self, text: str) -> None:
        self.lines.append(text)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            """
            SELECT runs.*, tracks.display_name AS track_name
            FROM runs JOIN tracks ON tracks.id = runs.track_id
            WHERE runs.id = ?
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"run": public_run(row)}


@app.get("/media/{run_id}/original")
def original_video(run_id: str) -> FileResponse:
    with db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None or not Path(row["original_path"]).exists():
        raise HTTPException(status_code=404, detail="Original video not found")
    return FileResponse(row["original_path"], filename=row["original_filename"])


@app.get("/media/{run_id}/thumbnail.jpg")
def thumbnail(run_id: str) -> FileResponse:
    with db() as conn:
        row = conn.execute("SELECT thumbnail_path FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None or not row["thumbnail_path"] or not Path(row["thumbnail_path"]).exists():
        raise HTTPException(status_code=404, detail="Thumbnail not ready")
    return FileResponse(row["thumbnail_path"], media_type="image/jpeg")


@app.get("/api/runs/{run_id}/download-options")
def download_options(run_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return DOWNLOAD_OPTIONS


@app.post("/api/runs/{run_id}/downloads")
def request_download(run_id: str, payload: dict[str, str], background_tasks: BackgroundTasks) -> dict[str, Any]:
    fmt = payload.get("format", "")
    quality = payload.get("quality", "")
    fps = payload.get("fps", "")
    if fmt not in DOWNLOAD_OPTIONS["formats"] or quality not in DOWNLOAD_OPTIONS["qualities"] or fps not in DOWNLOAD_OPTIONS["fps"]:
        raise HTTPException(status_code=400, detail="Unsupported download option")

    with db() as conn:
        run = conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        existing = conn.execute(
            "SELECT * FROM video_variants WHERE run_id = ? AND format = ? AND quality = ? AND fps = ?",
            (run_id, fmt, quality, fps),
        ).fetchone()
        if existing is not None:
            variant = existing
        else:
            variant_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO video_variants (id, run_id, format, quality, height, fps, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (variant_id, run_id, fmt, quality, QUALITY_HEIGHTS[quality], fps, "queued", utc_now()),
            )
            variant = conn.execute("SELECT * FROM video_variants WHERE id = ?", (variant_id,)).fetchone()
            background_tasks.add_task(process_variant, variant_id)

    return {"job": public_variant(variant)}


@app.get("/api/download-jobs/{job_id}")
def download_job(job_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM video_variants WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Download job not found")
    return {"job": public_variant(row)}


def public_variant(row: sqlite3.Row) -> dict[str, Any]:
    item = row_to_dict(row)
    item.pop("path", None)
    item["download_url"] = f"/downloads/{row['id']}" if row["status"] == "ready" else None
    return item


def unlink_if_present(path_value: str | None) -> None:
    if path_value:
        Path(path_value).unlink(missing_ok=True)


def remove_run(conn: sqlite3.Connection, run_id: str) -> None:
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    variants = conn.execute("SELECT path FROM video_variants WHERE run_id = ?", (run_id,)).fetchall()
    unlink_if_present(run["original_path"])
    unlink_if_present(run["thumbnail_path"])
    for variant in variants:
        unlink_if_present(variant["path"])
    conn.execute("DELETE FROM video_variants WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))


@app.get("/api/admin/summary")
def admin_summary(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    usage = shutil.disk_usage(DATA_DIR)
    with db() as conn:
        tracks = conn.execute(
            """
            SELECT tracks.*, COUNT(runs.id) AS run_count
            FROM tracks
            LEFT JOIN runs ON runs.track_id = tracks.id
            GROUP BY tracks.id
            ORDER BY tracks.sort_order, tracks.display_name
            """
        ).fetchall()
        run_count = conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"]
        variant_count = conn.execute("SELECT COUNT(*) AS count FROM video_variants").fetchone()["count"]
    return {
        "tracks": [row_to_dict(row) for row in tracks],
        "run_count": run_count,
        "variant_count": variant_count,
        "data_dir": str(DATA_DIR),
        "database": str(DB_PATH),
        "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
    }


@app.get("/api/admin/runs")
def admin_runs(track_id: str | None = None, _: None = Depends(require_admin_token)) -> dict[str, Any]:
    params: list[Any] = []
    where = ""
    if track_id:
        where = "WHERE runs.track_id = ?"
        params.append(track_id)
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT runs.*, tracks.display_name AS track_name,
              (SELECT COUNT(*) FROM video_variants WHERE video_variants.run_id = runs.id) AS variant_count
            FROM runs
            JOIN tracks ON tracks.id = runs.track_id
            {where}
            ORDER BY runs.upload_time DESC
            LIMIT 500
            """,
            params,
        ).fetchall()
    return {"runs": [public_run(row) | {"variant_count": row["variant_count"]} for row in rows]}


@app.post("/api/admin/tracks")
def admin_create_track(track: TrackCreate, _: None = Depends(require_admin_token)) -> dict[str, Any]:
    validate_track_payload(track)
    with db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO tracks (id, display_name, sort_order, min_time_ms, max_time_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                (track.id, track.display_name.strip(), track.sort_order, track.min_time_ms, track.max_time_ms),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Track id already exists") from None
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track.id,)).fetchone()
    return {"track": row_to_dict(row)}


@app.patch("/api/admin/tracks/{track_id}")
def admin_update_track(track_id: str, track: TrackUpdate, _: None = Depends(require_admin_token)) -> dict[str, Any]:
    validate_track_payload(track)
    with db() as conn:
        existing = conn.execute("SELECT id FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Track not found")
        conn.execute(
            """
            UPDATE tracks
            SET display_name = ?, sort_order = ?, min_time_ms = ?, max_time_ms = ?
            WHERE id = ?
            """,
            (track.display_name.strip(), track.sort_order, track.min_time_ms, track.max_time_ms, track_id),
        )
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return {"track": row_to_dict(row)}


@app.delete("/api/admin/tracks/{track_id}")
def admin_delete_track(track_id: str, _: None = Depends(require_admin_token)) -> dict[str, Any]:
    with db() as conn:
        existing = conn.execute("SELECT id FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Track not found")
        run_count = conn.execute("SELECT COUNT(*) AS count FROM runs WHERE track_id = ?", (track_id,)).fetchone()["count"]
        if run_count:
            raise HTTPException(status_code=409, detail="Delete that track's runs before deleting the track")
        conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
    return {"deleted": True, "track_id": track_id}


@app.delete("/api/admin/runs/{run_id}")
def admin_delete_run(run_id: str, _: None = Depends(require_admin_token)) -> dict[str, Any]:
    with db() as conn:
        remove_run(conn, run_id)
    return {"deleted": True, "run_id": run_id}


@app.post("/api/admin/runs/{run_id}/retry-thumbnail")
def admin_retry_thumbnail(run_id: str, background_tasks: BackgroundTasks, _: None = Depends(require_admin_token)) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        conn.execute("UPDATE runs SET status = ?, error_message = NULL WHERE id = ?", ("uploaded", run_id))
    background_tasks.add_task(generate_thumbnail, run_id)
    return {"queued": True, "run_id": run_id}


@app.post("/api/admin/reset-generated")
def admin_reset_generated(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    removed = 0
    with db() as conn:
        variants = conn.execute("SELECT path FROM video_variants").fetchall()
        for variant in variants:
            if variant["path"] and Path(variant["path"]).exists():
                removed += 1
            unlink_if_present(variant["path"])
        conn.execute("DELETE FROM video_variants")
    return {"deleted_variants": removed}


@app.get("/downloads/{variant_id}")
def download_variant(variant_id: str) -> FileResponse:
    with db() as conn:
        row = conn.execute("SELECT * FROM video_variants WHERE id = ?", (variant_id,)).fetchone()
    if row is None or row["status"] != "ready" or not row["path"] or not Path(row["path"]).exists():
        raise HTTPException(status_code=404, detail="Download variant not ready")
    return FileResponse(row["path"], filename=f"runvault-{row['run_id']}-{row['quality']}-{row['fps']}.{row['format']}")


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
