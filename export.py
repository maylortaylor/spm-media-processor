import json
import re
import subprocess
import sys
from pathlib import Path

from config import load_config, get_workspace


def _parse_date_from_folder(folder: Path) -> str | None:
    """Fallback date parser from folder naming convention: 'M.DD - name' inside a YYYY parent."""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})", folder.name)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    year_m = re.match(r"^(\d{4})$", folder.parent.name)
    if year_m:
        return f"{year_m.group(1)}-{month:02d}-{day:02d}"
    return f"{month:02d}-{day:02d}"


def seconds_to_hhmmss(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def sanitize_filename(name: str) -> str:
    for char in r'\/:*?"<>|':
        name = name.replace(char, "")
    return name.strip()


def cut_segment(source: Path, start: float, end: float, output: Path, config: dict) -> None:
    duration = end - start
    subprocess.run(
        [
            config.get("ffmpeg_path", "ffmpeg"),
            "-ss",
            str(start),
            "-i",
            str(source),
            "-t",
            str(duration),
            "-c",
            "copy",
            "-avoid_negative_ts",
            "1",
            "-y",
            str(output),
        ],
        check=True,
        capture_output=True,
    )


def run_export(folder: Path, output_dir: Path | None = None) -> None:
    folder = folder.resolve()
    config = load_config()
    workspace = get_workspace(folder, output_dir)

    scan_file = workspace / "scan_result.json"
    event_date = None
    if scan_file.exists():
        try:
            with open(scan_file) as f:
                scan_data = json.load(f)
            event_date = scan_data.get("event_date")
        except (PermissionError, OSError):
            pass
    if not event_date:
        event_date = _parse_date_from_folder(folder)

    try:
        segment_files = sorted(workspace.glob("*_segments.json"))
    except PermissionError:
        print(f"Permission denied reading workspace: {workspace}")
        print("Grant Full Disk Access to your terminal in System Settings → Privacy & Security → Full Disk Access")
        return
    if not segment_files:
        if not workspace.exists():
            print(f"Workspace not found: {workspace}\nRun analyze first.")
        else:
            print(f"No *_segments.json files in: {workspace}\nRun analyze first.")
        return

    print(f"\nOutput directory: {workspace}")

    exported = 0
    skipped = 0

    for sf in segment_files:
        with open(sf) as f:
            data = json.load(f)

        source = Path(data["source_file"])
        if not source.exists():
            print(f"  Source missing, skipping: {source.name}")
            continue

        for seg in data.get("segments", []):
            if not seg.get("approved") and not data.get("auto_approved"):
                print(f"  Segment {seg['id']} not approved — skipping. Run review first.")
                skipped += 1
                continue

            label = sanitize_filename(seg.get("label", f"Set {seg['id']}"))
            date_prefix = event_date or "unknown-date"
            clip_name = f"{date_prefix} - {label} - Full Set"
            output_file = workspace / f"{clip_name}.mp4"

            if output_file.exists():
                print(f"  Already exported: {output_file.name}")
                skipped += 1
                continue

            start = seg["start"]
            end = seg["end"]
            duration_min = (end - start) / 60
            print(f"  Exporting: {output_file.name} ({duration_min:.1f} min)...")

            try:
                cut_segment(source, start, end, output_file, config)
                print(f"    Done → {output_file.name}")
                exported += 1
            except subprocess.CalledProcessError as e:
                print(f"    ERROR: {e.stderr.decode()[:200]}")

    print(f"\nExported {exported} clip(s), skipped {skipped}.")
    if exported > 0:
        print(f'Run: {sys.executable} process.py metadata "{folder}"')
