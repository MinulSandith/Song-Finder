"""Bridges the saved-songs list to the cloned bulk-downloader tool."""

import os
import shutil
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

from backend import netfix  # noqa: F401  (patches socket.getaddrinfo on import)
from backend.activity_log import log_event

REPO_DIR = Path(__file__).resolve().parent.parent / "Download-Simply-Videos-From-YouTube"
DOWNLOADS_DIR = REPO_DIR / "downloads"

if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from download import download_single_video  # noqa: E402  (path set up above)

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
# Held while a job downloads, so jobs (a bulk run, a single "Download audio"
# click) also go one after another instead of in parallel.
_run_lock = threading.Lock()


def _ensure_ffmpeg_on_path() -> bool:
    """Make sure ffmpeg is reachable, even if this process's PATH predates
    an ffmpeg install (e.g. a winget install done after this server started).
    """

    if shutil.which("ffmpeg") is not None:
        return True

    winget_packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget_packages.is_dir():
        for exe in winget_packages.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
            os.environ["PATH"] = str(exe.parent) + os.pathsep + os.environ.get("PATH", "")
            return shutil.which("ffmpeg") is not None

    return False


def start_bulk_download(
    songs: list[dict],
    audio_only: bool = True,
    max_resolution: Optional[int] = None,
) -> str:
    """Downloads songs ({url, title}) one at a time, in the given order."""

    if not songs:
        raise ValueError("No URLs to download")

    if not _ensure_ffmpeg_on_path():
        log_event("error", "Download: blocked — ffmpeg is not installed")
        raise RuntimeError(
            "ffmpeg is not installed or not on PATH. Install it "
            "(e.g. `winget install ffmpeg`) and restart the server."
        )

    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "status": "running",
            "total": len(songs),
            "done": 0,  # finished (downloaded or failed)
            "failed": [],  # titles that failed
            "current": None,  # title downloading now; None while waiting for another job
            "output_path": str(DOWNLOADS_DIR),
        }

    kind = "audio (MP3)" if audio_only else "video (MP4)"
    log_event("info", f"Download: queued {len(songs)} item(s) as {kind}")

    def run():
        try:
            with _run_lock:
                for i, song in enumerate(songs, start=1):
                    title = song.get("title") or song["url"]
                    with _lock:
                        _jobs[job_id]["current"] = title
                    print(f"[{i}/{len(songs)}] {title}")
                    try:
                        result = download_single_video(
                            song["url"], str(DOWNLOADS_DIR), i, audio_only, max_resolution
                        )
                        ok = result.get("success", False)
                        print(result.get("message", ""))
                    except Exception as exc:
                        ok = False
                        print(f"Failed: {exc}")
                    with _lock:
                        _jobs[job_id]["done"] = i
                        if not ok:
                            _jobs[job_id]["failed"].append(title)
                    if not ok:
                        log_event("warning", f"Download: failed — {title}")

            with _lock:
                job = _jobs[job_id]
                job["status"] = "done"
                job["current"] = None
                failed = len(job["failed"])
            ok_count = len(songs) - failed
            msg = f"Download: completed {ok_count}/{len(songs)} item(s) → {DOWNLOADS_DIR}"
            log_event("success" if not failed else "warning", msg)
        except Exception as exc:
            with _lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(exc)
            log_event("error", f"Download: failed — {exc}")

    threading.Thread(target=run, daemon=True).start()
    return job_id


def get_job_status(job_id: str) -> Optional[dict]:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None
