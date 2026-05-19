# Raspberry Pi Replay Server
This a local multimedia portal for a Unity racing game: upload a recorded run, store the original video, index run metadata in SQLite, show a per-track leaderboard, play replays, export CSV, and generate requested FFmpeg download variants on demand.

## Run Locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 scripts/reset_demo.py --seed
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://127.0.0.1:8000/
```

The fallback upload form uses the default token `DEMO_UPLOAD_TOKEN`. 

Developer tools are available at:

```text
http://127.0.0.1:8000/developer.html
```

The default developer token is `DEMO_ADMIN_TOKEN`. The developer page can add/edit/delete tracks, delete runs and their media files, retry thumbnails, and clear generated download variants.

## Main Files

- `app/main.py`: FastAPI API, SQLite schema, media endpoints, FFmpeg jobs.
- `static/`: replay portal frontend.
- `scripts/reset_demo.py`: reset runtime state or seed small generated demo videos.
- `deploy/runvault.service`: systemd unit for the Pi account `wxp`.

