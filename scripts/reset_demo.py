#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RUNVAULT_DATA_DIR", ROOT / "data")).resolve()
DB_PATH = Path(os.environ.get("RUNVAULT_DB_PATH", DATA_DIR / "runvault.sqlite3")).resolve()
ORIGINALS_DIR = DATA_DIR / "originals"
THUMBNAILS_DIR = DATA_DIR / "thumbnails"
DOWNLOADS_DIR = DATA_DIR / "downloads"

TRACKS = [
    ("rooftop", "Rooftop Sprint", 10, 20_000, 10 * 60_000),
    ("crane", "Crane Dash", 20, 20_000, 10 * 60_000),
    ("metro", "Metro Line", 30, 20_000, 10 * 60_000),
]

DEMO_RUNS = [
    ("Nico", "rooftop", 48_312, "#48b7a7"),
    ("Alejandro", "rooftop", 51_908, "#d2a24c"),
    ("Maya", "crane", 54_021, "#7f8cff"),
    ("Luis", "crane", 62_449, "#e06c75"),
    ("Sam", "metro", 69_113, "#72b47e"),
]


def utc_now(offset_minutes: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(minutes=offset_minutes)).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for path in (DATA_DIR, ORIGINALS_DIR, THUMBNAILS_DIR, DOWNLOADS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from app.main import init_db as app_init_db

    app_init_db()


def clear_demo() -> None:
    ensure_dirs()
    if DB_PATH.exists():
        DB_PATH.unlink()
    for path in (ORIGINALS_DIR, THUMBNAILS_DIR, DOWNLOADS_DIR):
        shutil.rmtree(path, ignore_errors=True)
    ensure_dirs()
    init_db()


def make_video(output: Path, label: str, color: str) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=1280x720:d=4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=4",
        "-vf",
        f"drawtext=text='{label}':fontcolor=white:fontsize=54:x=(w-text_w)/2:y=(h-text_h)/2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output),
    ]
    subprocess.run(cmd, check=True)


def seed() -> None:
    clear_demo()
    with sqlite3.connect(DB_PATH) as conn:
        for index, (username, track_id, completion_time_ms, color) in enumerate(DEMO_RUNS):
            run_id = uuid.uuid4().hex
            video_path = ORIGINALS_DIR / f"{run_id}.mp4"
            thumb_path = THUMBNAILS_DIR / f"{run_id}.jpg"
            make_video(video_path, f"{username} {completion_time_ms / 1000:.3f}s", color)
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    "00:00:01",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    "-vf",
                    "scale='min(640,iw)':-2",
                    str(thumb_path),
                ],
                check=True,
            )
            conn.execute(
                """
                INSERT INTO runs (
                  id, username, completion_time_ms, track_id, original_filename, original_path,
                  thumbnail_path, upload_time, status, file_size_bytes, sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    username,
                    completion_time_ms,
                    track_id,
                    f"{username}-{track_id}.mp4",
                    str(video_path),
                    str(thumb_path),
                    utc_now(index * 7),
                    "ready",
                    video_path.stat().st_size,
                    "demo",
                ),
            )


def reset_generated() -> None:
    ensure_dirs()
    shutil.rmtree(DOWNLOADS_DIR, ignore_errors=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM video_variants")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset or seed RunVault demo data.")
    parser.add_argument("--seed", action="store_true", help="Create small synthetic demo videos and rows.")
    parser.add_argument("--generated-only", action="store_true", help="Delete generated download variants only.")
    args = parser.parse_args()

    if args.generated_only:
        reset_generated()
    elif args.seed:
        seed()
    else:
        clear_demo()


if __name__ == "__main__":
    main()
