import shutil
from datetime import datetime
from pathlib import Path

from analyze import get_duration
from classify import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, format_date, parse_folder_date, scan_folder
from config import get_workspace, load_config
from rename_media import (
    IMAGE_EXTENSIONS,
    get_image_exif,
    get_video_metadata,
    load_scan_result,
    normalize_device_label,
)

CLIP_THRESHOLD_SECONDS = 180.0

BUCKETS = ("PICS", "CLIPS", "VIDEOS", "AUDIO", "misc")

# Internal workspace artifacts — never copied to output buckets
_INTERNAL_SUFFIXES = frozenset(["_audio.mp3", "_segments.json", "_peaks.json", "_metadata.json"])
_INTERNAL_NAMES = frozenset(["scan_result.json", "rename_manifest.json"])


def _is_internal(path: Path, workspace: Path) -> bool:
    if path.is_relative_to(workspace):
        return True
    n = path.name
    if n in _INTERNAL_NAMES:
        return True
    for suf in _INTERNAL_SUFFIXES:
        if n.endswith(suf):
            return True
    return False


def _load_exported_sources(workspace: Path) -> set[str]:
    sources: set[str] = set()
    for sf in workspace.glob("*_segments.json"):
        try:
            import json

            with open(sf) as f:
                data = json.load(f)
            src = data.get("source_file")
            if src:
                sources.add(str(Path(src).resolve()))
        except Exception:
            pass
    return sources


def _classify_file(filepath: Path, exported_sources: set[str], config: dict) -> str:
    ext = filepath.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "PICS"
    if ext in AUDIO_EXTENSIONS:
        return "AUDIO"
    if ext in VIDEO_EXTENSIONS:
        if str(filepath.resolve()) in exported_sources:
            return "skip"
        try:
            dur = get_duration(filepath, config)
        except Exception:
            dur = float("inf")
        return "CLIPS" if dur <= CLIP_THRESHOLD_SECONDS else "VIDEOS"
    return "misc"


def build_organize_plan(
    folder: Path,
    workspace: Path,
    scan_data: dict,
    config: dict,
) -> list[dict]:
    exported_sources = _load_exported_sources(workspace)

    event_date = None
    raw_date = scan_data.get("event_date") if scan_data else None
    if raw_date:
        for fmt in ("%m.%d.%Y", "%Y-%m-%d"):
            try:
                event_date = datetime.strptime(raw_date, fmt).date()
                break
            except (ValueError, TypeError):
                pass
    if event_date is None:
        event_date = parse_folder_date(folder.name)

    date_str = format_date(event_date) if event_date else "unknown-date"

    counters: dict[tuple[str, str], int] = {}
    plan: list[dict] = []

    for filepath in sorted(folder.rglob("*")):
        if not filepath.is_file():
            continue
        if _is_internal(filepath, workspace):
            continue

        bucket = _classify_file(filepath, exported_sources, config)
        if bucket == "skip":
            continue

        ext = filepath.suffix.lower()

        if bucket == "PICS":
            meta = get_image_exif(filepath)
            label = normalize_device_label(meta["make"], meta["model"])
        elif bucket in ("CLIPS", "VIDEOS"):
            meta = get_video_metadata(filepath, config)
            label = normalize_device_label(meta["make"], meta["model"])
        elif bucket == "AUDIO":
            label = "audio"
        else:
            label = None

        if bucket == "misc":
            dest_name = filepath.name
        else:
            key = (label, bucket)
            counters[key] = counters.get(key, 0) + 1
            dest_name = f"{date_str} - {label} {counters[key]:02d}{ext}"

        dest = workspace / bucket / dest_name
        plan.append({"source": filepath, "dest": dest, "bucket": bucket, "reason": dest_name})

    return plan


def run_organize(
    folder: Path,
    output_dir: Path | None = None,
    dry_run: bool = False,
    skip_confirm: bool = False,
    config: dict | None = None,
) -> dict:
    folder = folder.resolve()
    if config is None:
        config = load_config()
    workspace = get_workspace(folder, output_dir)

    # Auto-scan + confirm
    scan_data = load_scan_result(workspace)

    if not scan_data:
        print("No scan result found — running scan first...")
        scan_data = scan_folder(folder, output_dir)

    if not skip_confirm and not scan_data.get("confirmed"):
        print("\nEvent metadata (unconfirmed):")
        print(f"  Date:  {scan_data.get('event_date', 'unknown')}")
        print(f"  Event: {scan_data.get('event_name', 'unknown')}")
        bands = scan_data.get("bands", [])
        print(f"  Bands: {', '.join(bands) if bands else 'none detected'}")
        resp = input("\nConfirm and continue? [Y/n]: ").strip().lower()
        if resp == "n":
            print("Aborted. Edit scan_result.json or re-run scan, then retry organize.")
            return {}

    plan = build_organize_plan(folder, workspace, scan_data, config)

    if not plan:
        print(f"  Nothing to organize in: {folder.name}")
        return {b: 0 for b in BUCKETS} | {"skipped": 0}

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"\n{prefix}Organizing {len(plan)} file(s) from: {folder.name}")

    col = 52
    print(f"  {'Source':<{col}}  Destination")
    print("  " + "-" * 110)

    counts: dict[str, int] = {b.lower(): 0 for b in BUCKETS}
    counts["skipped"] = 0

    for entry in plan:
        src_label = entry["source"].name
        dst_label = f"{entry['bucket']}/{entry['dest'].name}"
        print(f"  {src_label:<{col}}  → {dst_label}")

        if not dry_run:
            dest = entry["dest"]
            if dest.exists() and dest.stat().st_size == entry["source"].stat().st_size:
                counts["skipped"] += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry["source"], dest)
            counts[entry["bucket"].lower()] += 1

    if dry_run:
        bucket_tally = {}
        for entry in plan:
            b = entry["bucket"].lower()
            bucket_tally[b] = bucket_tally.get(b, 0) + 1
        for b, n in bucket_tally.items():
            print(f"\n  Would copy {n} file(s) to {b.upper()}/")
    else:
        print(f"\n  PICS: {counts['pics']}  CLIPS: {counts['clips']}  "
              f"VIDEOS: {counts['videos']}  AUDIO: {counts['audio']}  "
              f"misc: {counts['misc']}  skipped: {counts['skipped']}")

    return counts
