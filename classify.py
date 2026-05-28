import json
import re
import os
import sys
from datetime import date
from pathlib import Path

from google import genai as _genai

from config import load_config, get_workspace
from calendar_client import get_events_on_date

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".wmv", ".flv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".flac", ".m4a"}

SIZE_CATEGORIES = [
    (50 * 1024 * 1024, "skip"),
    (2 * 1024**3, "phone_clip"),
    (8 * 1024**3, "short_set"),
    (25 * 1024**3, "medium_stream"),
    (float("inf"), "full_stream"),
]


def categorize_by_size(size_bytes: int) -> str:
    for threshold, category in SIZE_CATEGORIES:
        if size_bytes < threshold:
            return category
    return "full_stream"


def parse_folder_date(folder_name: str) -> date | None:
    """Parse MM.DD or MM.DD.YYYY from folder name."""
    match = re.match(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", folder_name)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    if match.group(3):
        year = int(match.group(3))
    else:
        today = date.today()
        year = today.year
        if month > today.month or (month == today.month and day > today.day):
            year -= 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def format_date(d: date) -> str:
    return f"{d.month:02d}.{d.day:02d}.{d.year}"


def _heuristic_classify(folder_name: str, event_date, calendar_events: list) -> dict:
    """Extract event name and bands from folder name without AI."""
    rest = re.sub(r"^\d{1,2}\.\d{1,2}(?:\.\d{4})?\s*", "", folder_name).strip()
    rest = re.sub(r"^[\s\-=—]+", "", rest).strip()

    notes = ""
    paren_match = re.search(r"\(([^)]+)\)", rest)
    if paren_match:
        notes = paren_match.group(1).strip()
        rest = re.sub(r"\s*\([^)]+\)", "", rest).strip()

    bands = [b.strip() for b in rest.split(",") if b.strip()]

    if calendar_events:
        event_name = calendar_events[0].get("summary", folder_name)
    elif bands:
        event_name = bands[0]
    else:
        event_name = folder_name

    return {
        "event_date": format_date(event_date) if event_date else None,
        "event_name": event_name,
        "bands": bands,
        "notes": notes,
    }


def scan_folder(folder: Path, output_dir: Path | None = None) -> dict:
    config = load_config()
    folder = folder.resolve()
    folder_name = folder.name
    workspace = get_workspace(folder, output_dir)

    print(f"\nScanning: {folder}")
    print(f"Workspace: {workspace}")

    # Recursively collect video/audio files — size filter handles separating
    # large recordings from small clips and phone photos automatically
    files = []
    for f in sorted(folder.rglob("*")):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext not in VIDEO_EXTENSIONS and ext not in AUDIO_EXTENSIONS:
            continue
        size = f.stat().st_size
        rel_path = str(f.relative_to(folder))
        entry = {
            "name": f.name,
            "path": str(f),
            "relative_path": rel_path,
            "size_bytes": size,
            "size_gb": round(size / 1024**3, 2),
            "type": "video" if ext in VIDEO_EXTENSIONS else "audio",
            "category": categorize_by_size(size) if ext in VIDEO_EXTENSIONS else "audio_file",
        }
        files.append(entry)

    # Calendar lookup
    event_date = parse_folder_date(folder_name)
    calendar_events = []
    if event_date and config.get("google_calendar_id"):
        print(f"Looking up calendar events for {format_date(event_date)}...")
        calendar_events = get_events_on_date(config["google_calendar_id"], event_date)
        if calendar_events:
            print(f"  Found {len(calendar_events)} calendar event(s)")
        else:
            print("  No calendar events found")

    # Classification — AI or heuristic
    video_files = [f for f in files if f["type"] == "video"]

    use_ai = config.get("scan_use_ai", True)
    claude_data = None

    if use_ai:
        print(f"Asking Gemini to classify {len(video_files)} video file(s)...")
        try:
            client = _genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

            file_list_text = "\n".join(f"  {f['relative_path']} ({f['size_gb']} GB)" for f in video_files) or "  (none)"

            cal_text = "Not found"
            if calendar_events:
                cal_text = "\n".join(
                    f"  - {e['summary']}: {e['description'][:200] if e['description'] else '(no description)'}"
                    for e in calendar_events
                )

            prompt = f"""Analyze this folder of live music event recordings.

Folder name: {folder_name}
Parsed date: {format_date(event_date) if event_date else "unknown"}
Calendar events on this date:
{cal_text}

Video files (path relative to event folder, size):
{file_list_text}

Return a JSON object (no markdown, no explanation) with:
- "event_date": "MM.DD.YYYY" from folder name (or null)
- "event_name": best guess at the event/show name
- "bands": array of band names visible in folder name or calendar (empty array if none found)
- "notes": any relevant context about this event (1-2 sentences max)

Example: {{"event_date":"03.15.2024","event_name":"Final Friday","bands":["Band A","Band B"],"notes":"Monthly showcase at Suite E."}}"""

            response = client.models.generate_content(
                model=config.get("gemini_model", "gemini-2.0-flash"),
                contents=prompt,
            )

            try:
                claude_data = json.loads(response.text.strip())
            except (json.JSONDecodeError, AttributeError):
                claude_data = None

        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                print("  Gemini quota exceeded — falling back to heuristic classification")
            else:
                print(f"  Gemini error — falling back to heuristic classification: {e}")
            claude_data = None

    if claude_data is None:
        if not use_ai:
            print("Using heuristic classification (AI disabled)")
        claude_data = _heuristic_classify(folder_name, event_date, calendar_events)

    result = {
        "folder": str(folder),
        "folder_name": folder_name,
        "workspace": str(workspace),
        "event_date": claude_data.get("event_date"),
        "event_name": claude_data.get("event_name", folder_name),
        "bands": claude_data.get("bands", []),
        "notes": claude_data.get("notes", ""),
        "calendar_events": calendar_events,
        "files": files,
    }

    out_file = workspace / "scan_result.json"
    with open(out_file, "w") as f:
        json.dump(result, f, indent=2)

    return result


def run_scan(folder: Path, output_dir: Path | None = None) -> None:
    result = scan_folder(folder, output_dir)

    print(f"\n{'=' * 60}")
    print(f"Event:  {result['event_name']}")
    print(f"Date:   {result['event_date'] or 'unknown'}")
    if result["bands"]:
        print(f"Bands:  {', '.join(result['bands'])}")
    if result["notes"]:
        print(f"Notes:  {result['notes']}")

    print(f"\n{'File':<55} {'Size':>8}  Category")
    print("-" * 78)
    for f in result["files"]:
        label = f["relative_path"]
        print(f"  {label:<53} {f['size_gb']:>6.2f}GB  {f['category']}")

    processable = [f for f in result["files"] if f["category"] in ("short_set", "medium_stream", "full_stream")]
    print(f"\n{len(processable)} file(s) will be analyzed.")
    print(f'Run: {sys.executable} process.py analyze "{folder}"')
