"""Read MP3 durations from a folder, write them to one report file, and
delete selected MP3s. Used by the Downloads tab and by mp3_durations.py.
"""

import csv
from pathlib import Path

from mutagen.mp3 import MP3, HeaderNotFoundError

LONG_SECONDS = 5 * 60


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def scan_folder(folder: Path, recursive: bool = False) -> tuple[list[dict], list[dict]]:
    """Returns (files, errors). Each file is {name, seconds, duration, long},
    with name relative to folder; unreadable MP3s go to errors.
    """

    pattern = "**/*" if recursive else "*"
    paths = sorted(p for p in folder.glob(pattern) if p.is_file() and p.suffix.lower() == ".mp3")

    files, errors = [], []
    for path in paths:
        name = path.relative_to(folder).as_posix()
        try:
            seconds = MP3(path).info.length
        except (HeaderNotFoundError, OSError) as e:
            errors.append({"name": name, "error": str(e)})
            continue
        files.append({
            "name": name,
            "seconds": round(seconds, 2),
            "duration": format_duration(seconds),
            "long": seconds > LONG_SECONDS,
        })
    return files, errors


def write_report(files: list[dict], output: Path) -> None:
    """Writes the durations as a .csv (by extension) or an aligned text table."""

    total = sum(f["seconds"] for f in files)
    if output.suffix.lower() == ".csv":
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["file", "duration", "seconds"])
            writer.writerows((f["name"], f["duration"], f["seconds"]) for f in files)
            writer.writerow(["TOTAL", format_duration(total), round(total, 2)])
        return

    total_label = f"TOTAL ({len(files)} files)"
    width = max([len(f["name"]) for f in files] + [len(total_label)])
    with output.open("w", encoding="utf-8") as fh:
        for f in files:
            fh.write(f"{f['name']:<{width}}  {f['duration']:>8}\n")
        fh.write("-" * (width + 10) + "\n")
        fh.write(f"{total_label:<{width}}  {format_duration(total):>8}\n")


def delete_files(folder: Path, names: list[str]) -> tuple[list[str], list[dict]]:
    """Deletes the named MP3s, refusing anything that isn't an .mp3 inside
    folder (so a crafted name like "../x" can't reach other files).
    Returns (deleted, errors).
    """

    root = folder.resolve()
    deleted, errors = [], []
    for name in names:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != ".mp3":
            errors.append({"name": name, "error": "not an MP3 in this folder"})
            continue
        try:
            path.unlink()
            deleted.append(name)
        except OSError as e:
            errors.append({"name": name, "error": str(e)})
    return deleted, errors
