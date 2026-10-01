"""Write the duration of every MP3 in a folder to a single file.

Usage:
    python mp3_durations.py <folder> [-o durations.txt] [-r]

Requires: pip install mutagen
"""

import argparse
import csv
import sys
from pathlib import Path

from mutagen.mp3 import MP3, HeaderNotFoundError


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect MP3 durations into one file.")
    parser.add_argument("folder", type=Path, help="Folder containing MP3 files")
    parser.add_argument("-o", "--output", type=Path, default=Path("durations.txt"),
                        help="Output file (.txt or .csv). Default: durations.txt")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Also search subfolders")
    args = parser.parse_args()

    if not args.folder.is_dir():
        print(f"Not a folder: {args.folder}", file=sys.stderr)
        return 1

    pattern = "**/*" if args.recursive else "*"
    files = sorted(p for p in args.folder.glob(pattern)
                   if p.is_file() and p.suffix.lower() == ".mp3")
    if not files:
        print(f"No MP3 files found in {args.folder}", file=sys.stderr)
        return 1

    rows = []
    total = 0.0
    for path in files:
        name = str(path.relative_to(args.folder))
        try:
            seconds = MP3(path).info.length
        except (HeaderNotFoundError, OSError) as e:
            print(f"Skipping {name}: {e}", file=sys.stderr)
            continue
        total += seconds
        rows.append((name, format_duration(seconds), round(seconds, 2)))

    if args.output.suffix.lower() == ".csv":
        with args.output.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["file", "duration", "seconds"])
            writer.writerows(rows)
            writer.writerow(["TOTAL", format_duration(total), round(total, 2)])
    else:
        width = max(len(name) for name, _, _ in rows)
        with args.output.open("w", encoding="utf-8") as f:
            for name, duration, _ in rows:
                f.write(f"{name:<{width}}  {duration:>8}\n")
            f.write("-" * (width + 10) + "\n")
            f.write(f"{'TOTAL (' + str(len(rows)) + ' files)':<{width}}  {format_duration(total):>8}\n")

    print(f"Wrote {len(rows)} durations to {args.output} (total {format_duration(total)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
