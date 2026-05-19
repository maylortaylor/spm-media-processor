# CLAUDE.md — spm-media-processor

**SPM Media Processor** — processes raw live recordings from Suite E Studios (St. Pete, FL) into YouTube-ready clips for the St Pete Music (SPM) channel. Handles multi-band nights where one continuous recording needs to be split by band, named correctly, and enriched with YouTube metadata.

All operations are **non-destructive** — original event folders are never modified. All output goes to `_processed/{event_folder_name}/` (or the configured `default_output_dir`). When in doubt, use `--dry-run`.

## Quick Reference

```bash
# First-time venv setup (run once per machine)
python3 -m venv venv
source venv/bin/activate          # Mac/Linux
# venv\Scripts\activate           # Windows

pip install -r requirements.txt
python process.py configure

# Always activate venv before use
source venv/bin/activate
python process.py scan        <folder>     # Classify + calendar lookup
python process.py analyze     <folder>     # Detect segments
python process.py review      <folder>     # Browser review (long videos only)
python process.py export      <folder>     # Cut clips
python process.py metadata    <folder>     # Generate YouTube metadata JSON
python process.py batch       <year-dir>   # Process all event folders in a year directory
python process.py rename      <folder>     # Copy-rename images/short clips by EXIF source
python process.py deep-batch  <root-dir>   # Process all years under a root directory
```

Required env var: `ANTHROPIC_API_KEY`
FFmpeg must be installed and in PATH (or set in `config.json`).

---

## Agent Workflow Guide

An agent helping a user run this tool should follow these decision trees:

### Single event folder (most common)
```bash
python process.py scan     "<folder>"   # Step 1: identify bands + event
python process.py analyze  "<folder>"   # Step 2: detect set boundaries
python process.py review   "<folder>"   # Step 3: browser UI (auto-skipped for short videos)
python process.py export   "<folder>"   # Step 4: cut clips
python process.py metadata "<folder>"   # Step 5: generate YouTube metadata JSON
```

### Full year of events
```bash
python process.py batch "EVENTS/2026/"  # runs scan+analyze+review+export per subfolder
```

### 4TB backlog / multi-year run
```bash
python process.py deep-batch "EVENTS/" --dry-run   # preview first — verify folder discovery
python process.py deep-batch "EVENTS/"              # then run for real
python process.py deep-batch "EVENTS/" --year 2024  # limit to one year
```

### Images and short clips only
```bash
python process.py rename "<folder>" --dry-run  # preview renames
python process.py rename "<folder>"            # copy-rename (originals untouched)
```

### Decision points to surface to the user
- `scan_result.json` shows wrong bands → re-run `scan`, or edit the JSON manually
- Too many false cuts (quiet moments cut) → re-run `analyze --gap-db 18 --gap-sec 60`
- Gaps missed (sets running together) → re-run `analyze --gap-db 8 --gap-sec 20`
- Review shows wrong segment labels → edit band names in browser before saving
- After export → always run `metadata` before uploading to YouTube

### deep-batch notes for agents
- Deep-batch is **non-interactive** — band name prompts are auto-answered as "Unknown Artist"
- Multi-band videos that need review are exported with unapproved segments skipped
- After deep-batch: check the manifest JSON for errored folders, then run `review` + `export` on each one manually
- Use `--no-rename` to skip the image/short-clip renaming step if not needed

## Deep Context

| Load when... | Info |
|---|---|
| First run on a new machine | See **Machine Setup** below |
| config.json is missing or wrong | See **Config Schema** below |
| Google Calendar isn't working | See **Calendar Setup** below |
| Segments not finding set boundaries | See **Tuning Analysis** below |
| Images/clips showing wrong device label | See **Rename Device Labels** below |

---

## Machine Setup

### Mac
```bash
brew install ffmpeg python
cd spm-media-processor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python process.py configure
```

### Windows
1. Install Python 3.11+ from https://python.org (check "Add to PATH")
2. Download FFmpeg from https://ffmpeg.org/download.html, extract, add `bin/` folder to System PATH
3. Open Command Prompt in project directory:
```cmd
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
set ANTHROPIC_API_KEY=sk-ant-...
python process.py configure
```
The `configure` wizard will auto-detect FFmpeg and walk through the rest.

---

## Config Schema (`config.json`)

```json
{
  "ffmpeg_path": "ffmpeg",
  "ffprobe_path": "ffprobe",
  "google_calendar_id": "your-calendar-id@group.calendar.google.com",
  "default_output_dir": null,
  "single_band_threshold_min": 75,
  "gap_db": 12.0,
  "gap_sec": 30.0,
  "min_segment_min": 5
}
```

- `ffmpeg_path`: path to ffmpeg binary (default: "ffmpeg" assumes it's in PATH)
- `google_calendar_id`: Suite E Studios calendar ID (from StPeteMusic `.env.local`)
- `default_output_dir`: null = `_processed/` inside each event folder; or set a global output path
- `single_band_threshold_min`: videos shorter than this (minutes) auto-approve as single band
- `gap_db`: how many dB below peak volume counts as a "between-sets" gap (default 12)
- `gap_sec`: how many seconds of quiet = a real gap, not a brief pause (default 30)
- `min_segment_min`: minimum segment length in minutes to count as a real set (default 5)

---

## Calendar Setup

1. Go to https://console.cloud.google.com/
2. Create a project, enable Google Calendar API
3. Create OAuth2 credentials (Desktop application type)
4. Download as `credentials.json`, place in this project directory
5. Run `python process.py configure` — a browser window will open for authentication
6. `token.json` is created and saved for future use

The calendar is read-only. It returns the event name and description for the date
parsed from the folder name. It does NOT provide set times or band order.

---

## Tuning Analysis

The volume analysis works by:
1. Extracting audio as a 64kbps MP3 from the video
2. Getting the peak volume of the file (dBFS)
3. Running FFmpeg `silencedetect` at threshold = `peak - gap_db`

This catches both true silence AND quieter between-set background music.

**If too many false cuts** (quiet band moments being cut):
- Increase `--gap-db` to require a larger volume drop (e.g., `--gap-db 18`)
- Increase `--gap-sec` to require longer gaps (e.g., `--gap-sec 60`)

**If gaps are missed** (sets running together):
- Decrease `--gap-db` (e.g., `--gap-db 8`)
- Decrease `--gap-sec` (e.g., `--gap-sec 20`)

**Override per-folder:**
```bash
python process.py analyze "path/to/folder" --gap-db 15 --gap-sec 45
```

---

## File Categories (by video size)

| Category | Size | Processing |
|---|---|---|
| skip | < 50 MB | Ignored |
| phone_clip | 50 MB – 2 GB | Not processed (originals kept) |
| short_set | 2 – 8 GB | Analyzed |
| medium_stream | 8 – 25 GB | Analyzed |
| full_stream | 25 GB+ | Analyzed |

Videos ≤ `single_band_threshold_min` minutes → auto-approved as a single band (no review UI).
Videos longer → detect segments, open waveform review in browser.

---

## Output File Naming

Clips are saved as: `MM.DD.YYYY - Band Name - Full Set.mp4`
Metadata JSON: `MM.DD.YYYY - Band Name - Full Set_metadata.json`

Example: `03.15.2024 - The Midnight - Full Set.mp4`

---

---

## Rename Device Labels

The `rename` command reads EXIF camera make/model and maps it to a short label:

| EXIF make/model contains | Label |
|---|---|
| Apple, iPhone | `phone` |
| Samsung, Google, Pixel, Motorola, etc. | `phone` |
| Canon | `canon` |
| Nikon | `nikon` |
| Sony | `sony` |
| Fuji | `fuji` |
| GoPro | `gopro` |
| DJI | `dji` |
| Unknown / no EXIF | `camera` |

To add a new camera model, edit `_DEVICE_PATTERNS` at the top of `rename_media.py`.

Rename output format: `MM.DD.YYYY - {Context} - {label} image {counter:02d}.jpg`

Context priority: event name from `scan_result.json` → band names → day of week.

---

## Credentials Files (gitignored)

- `config.json` — machine config
- `credentials.json` — Google OAuth client secrets
- `token.json` — Google OAuth token
- `known_bands.json` — band names seen across sessions (auto-populated)
