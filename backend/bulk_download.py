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

from download import download_youtube_content  # noqa: E402  (path set up above)

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


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
    urls: list[str],
    audio_only: bool = True,
    max_resolution: Optional[int] = None,
    max_workers: int = 3,
) -> str:
    if not urls:
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
            "total": len(urls),
            "output_path": str(DOWNLOADS_DIR),
        }

    kind = "audio (MP3)" if audio_only else "video (MP4)"
    log_event("info", f"Download: started {len(urls)} item(s) as {kind}")

    def run():
        try:
            download_youtube_content(
                urls,
                str(DOWNLOADS_DIR),
                max_workers=max_workers,
                audio_only=audio_only,
                max_resolution=max_resolution,
            )
            with _lock:
                _jobs[job_id]["status"] = "done"
            log_event("success", f"Download: completed {len(urls)} item(s) → {DOWNLOADS_DIR}")
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
