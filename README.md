# spm-media-processor

**St Pete Music (SPM) media processor** — a local CLI tool that turns raw live music recordings from Suite E Studios (St. Pete, FL) into trimmed, YouTube-ready clips.

Built specifically for multi-band nights at Suite E where one continuous stream gets split by band, named correctly, and enriched with YouTube metadata for the St Pete Music channel.

**Non-destructive** — originals never touched. All output goes to `_processed/{event_folder_name}/`.

---

## Getting Started

### 1. Prerequisites

| Requirement | How to get it |
|---|---|
| Python 3.11+ | `brew install python` (Mac) · [python.org](https://python.org) (Windows) |
| FFmpeg | `brew install ffmpeg` (Mac) · [ffmpeg.org/download](https://ffmpeg.org/download.html) (Windows — add `bin/` to PATH) |
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com) → API Keys |

### 2. Clone and install

```bash
git clone https://github.com/maylortaylor/spm-media-processor.git
cd spm-media-processor

python3 -m venv venv
source venv/bin/activate       # Mac/Linux
# venv\Scripts\activate        # Windows

pip install -r requirements.txt
```

### 3. Set your Anthropic API key

Create a `.env` file in the project root (gitignored):

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
```

Or export it in your shell:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

The tool reads it automatically at startup. Without it, `scan` and `metadata` commands will fail.

### 4. Run the setup wizard

```bash
python process.py configure
```

This creates `config.json` (gitignored) with your local paths and preferences:

| Setting | Default | Description |
|---|---|---|
| `ffmpeg_path` | `ffmpeg` | Path to ffmpeg binary |
| `ffprobe_path` | `ffprobe` | Path to ffprobe binary |
| `google_calendar_id` | _(empty)_ | Suite E Google Calendar ID (optional) |
| `default_output_dir` | `null` | Output root; null = `_processed/` inside each event folder |
| `claude_model` | `claude-sonnet-4-6` | Claude model used for classify/metadata |
| `single_band_threshold_min` | `75` | Videos shorter than this (min) auto-approve as single band |
| `gap_db` | `12.0` | dB drop below peak that signals a between-set gap |
| `gap_sec` | `30.0` | Seconds of quiet required to count as a real gap |
| `min_segment_min` | `5` | Minimum segment length (minutes) to count as a real set |

### 5. (Optional) Google Calendar integration

Calendar lookup adds the event name to scan results automatically. Skip this if you don't need it.

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Create a project → enable **Google Calendar API**
3. Create **OAuth2 credentials** (type: Desktop application)
4. Download as `credentials.json` and place it in the project root
5. Re-run `python process.py configure` and enter your calendar ID
6. A browser window opens for one-time OAuth consent — `token.json` is saved for future runs

Both `credentials.json` and `token.json` are gitignored.

---

## Local files (gitignored — never committed)

| File | Purpose |
|---|---|
| `.env` | `ANTHROPIC_API_KEY` |
| `config.json` | Your machine-specific config (created by `configure`) |
| `credentials.json` | Google OAuth client secrets |
| `token.json` | Google OAuth token (auto-created on first calendar auth) |
| `known_bands.json` | Band names seen across sessions (auto-populated) |
| `venv/` | Python virtual environment |
| `_processed/` | Output clips and intermediate files |

---

## Event folder naming

The tool parses dates from folder names to look up the calendar and name output clips.

**Expected format:** `MM.DD - Band1 Band2` or `MM.DD.YYYY - Band1 Band2`

```
EVENTS/
  2026/
    03.15 - The Midnight After Party/
      stream.mp4
    04.22.2026 - Final Friday/
      camera1.mov
      camera2.mov
```

Folders that don't start with `MM.DD` are skipped by `batch` and `deep-batch`.

---

## Pipeline (single event)

Run these commands in order for one event folder:

| Step | Command | What it does |
|---|---|---|
| 1 | `python process.py scan <folder>` | Classify files, look up Google Calendar, identify bands via Claude |
| 2 | `python process.py analyze <folder>` | Extract audio, detect set boundaries by volume |
| 3 | `python process.py review <folder>` | Browser waveform review *(auto-skipped for short videos)* |
| 4 | `python process.py export <folder>` | Cut clips (stream copy, no re-encode) |
| 5 | `python process.py metadata <folder>` | Claude generates YouTube title/description/tags |

---

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

---

## Detection Tuning

If auto-detection cuts too aggressively or misses gaps:

```bash
python process.py analyze "folder" --gap-db 18 --gap-sec 45   # fewer cuts
python process.py analyze "folder" --gap-db 8  --gap-sec 20   # more cuts
```

See `CLAUDE.md` for the full tuning guide and agent workflow reference.
