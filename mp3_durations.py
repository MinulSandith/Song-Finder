"""Write the duration of every MP3 in a folder to a single file.

Usage:
    python mp3_durations.py <folder> [-o durations.txt] [-r]

Requires: pip install mutagen
"""

import argparse
import sys
from pathlib import Path

from backend.mp3_durations import format_duration, scan_folder, write_report


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

    files, errors = scan_folder(args.folder, args.recursive)
    for err in errors:
        print(f"Skipping {err['name']}: {err['error']}", file=sys.stderr)
    if not files:
        print(f"No readable MP3 files found in {args.folder}", file=sys.stderr)
        return 1

    write_report(files, args.output)
    total = sum(f["seconds"] for f in files)
    print(f"Wrote {len(files)} durations to {args.output} (total {format_duration(total)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
