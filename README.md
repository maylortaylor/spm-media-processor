# spm-media-processor

**St Pete Music (SPM) media processor** — a local CLI tool that turns raw live music recordings from Suite E Studios (St. Pete, FL) into trimmed, YouTube-ready clips.

Built specifically for multi-band nights at Suite E where one continuous stream gets split by band, named correctly, and enriched with YouTube metadata for the St Pete Music channel.

**Non-destructive** — originals never touched. All output goes to `_processed/{event_folder_name}/`.

## Requirements

- Python 3.11+
- FFmpeg (must be in PATH or configured in `config.json`)
- `ANTHROPIC_API_KEY` environment variable

## Install

```bash
python3 -m venv venv
source venv/bin/activate    # Mac/Linux  |  venv\Scripts\activate on Windows
pip install -r requirements.txt
python process.py configure
```

See `CLAUDE.md` for full setup instructions (Mac and Windows).

## Pipeline (single event)

Run these commands in order for one event folder:

| Step | Command | What it does |
|---|---|---|
| 1 | `python process.py scan <folder>` | Classify files, look up Google Calendar, identify bands via Claude |
| 2 | `python process.py analyze <folder>` | Extract audio, detect set boundaries by volume |
| 3 | `python process.py review <folder>` | Browser waveform review *(auto-skipped for short videos)* |
| 4 | `python process.py export <folder>` | Cut clips (stream copy, no re-encode) |
| 5 | `python process.py metadata <folder>` | Claude generates YouTube title/description/tags |

## Batch commands

```bash
# Process all events in a year folder
python process.py batch "EVENTS/2026/"

# Process entire archive (multiple years) — preview first
python process.py deep-batch "EVENTS/" --dry-run
python process.py deep-batch "EVENTS/" --year 2026

# Copy-rename images and short clips by EXIF camera source
python process.py rename "03.24 - Band1 Band2" --dry-run
python process.py rename "03.24 - Band1 Band2"
```

Renamed files use format: `MM.DD.YYYY - {EventOrDay} - {device} image 01.jpg`  
Device labels: `phone`, `canon`, `sony`, `nikon`, `camera` (unknown).

## Detection Tuning

If auto-detection cuts too aggressively or misses gaps:

```bash
python process.py analyze "folder" --gap-db 18 --gap-sec 45   # fewer cuts
python process.py analyze "folder" --gap-db 8  --gap-sec 20   # more cuts
```

See `CLAUDE.md` for the full tuning guide and agent workflow reference.
