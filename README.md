# spm-media-processor

**St Pete Music (SPM) media processor** — turns raw live music recordings from Suite E Studios (St. Pete, FL) into trimmed, named, YouTube-ready clips with generated metadata.

Built for multi-band nights at Suite E where one continuous stream gets split by band, named correctly, and enriched with YouTube metadata for the St Pete Music channel.

**Non-destructive** — originals never touched. All output goes to a workspace folder (`_processed/{event_folder_name}/`).

---

## GUI Quick Start (recommended)

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

### 3. Set your API key

Create a `.env` file in the project root:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Launch the GUI

**Mac:** double-click `start_gui.command` in Finder, or:

```bash
python gui_server.py
```

**Windows:** double-click `start_gui.bat`, or:

```cmd
python gui_server.py
```

A browser window opens automatically at `http://127.0.0.1:8765`.

---

## GUI Workflow

### Step 1 — Select a year folder

Paste or browse to a folder that contains event subfolders (e.g. `EVENTS/2026/`). Hit **Load & Scan All**.

```
EVENTS/
  2026/
    03.15 - Final Friday/
      stream.mp4
    04.24 - future vintage, moonshow, mootbooxle/
      full_show.mp4
    05.16 - The Palms/
      ...
```

### Step 2 — Review Claude's guesses

After scanning, each event folder gets a card showing Claude's inferences from the folder name and your Google Calendar (if configured):

- **Event name** — editable inline
- **Bands** — badge list with add/remove; autocomplete from your known bands list
- **Files** — which videos will be analyzed vs skipped
- **Pipeline status** — which stages are done

Hit **Approve ✓** on each card when the guesses look right. You can also skip events entirely.

### Step 3 — Analyze, Review, Export

Use the bulk action buttons in the dashboard header to run stages across all approved events at once:

| Button | What it does |
|---|---|
| **Scan Pending** | Scans folders without a scan result yet (re-runs Claude) |
| **Analyze Approved** | Detects set boundaries via audio volume for all approved events |
| **Export Ready** | Cuts clips for all events where segments are approved |
| **Generate Metadata** | Claude generates YouTube title/description/tags for all exported clips |

Each action streams real-time log output. A progress bar at the bottom tracks batch operations.

### Step 4 — Review waveform (multi-band events)

For events with multiple bands, click **Review** on the card to open the waveform editor in a new tab. Drag region boundaries, label each segment with the band name, and click Save. The dashboard detects completion and updates the card automatically.

### Step 5 — Before/after and metadata

After export, each card shows the before (source files + sizes) and after (output clips + sizes). After metadata generation, the title, description, and tags appear inline on the card for final editing before uploading to YouTube.

### Settings

Click ⚙ in the top-right corner to edit:
- FFmpeg path (with auto-detect)
- Default output directory
- Analysis thresholds (gap detection sensitivity)
- Google Calendar ID
- Known bands list (add/remove)

---

## Event folder naming

The tool parses dates from folder names to look up the calendar and name output clips.

**Expected format:** `MM.DD - Event Name` or `MM.DD.YYYY - Event Name`

```
2026/
  03.15 - The Midnight After Party/
  04.22.2026 - Final Friday/
  05.16 - The Palms Open Mic/
```

Bands in the folder name (`04.24 - future vintage, moonshow, mootbooxle`) are also parsed as guesses. Claude cross-references the folder name, file names, and calendar to produce the final classification.

---

## Output files

| File | Format |
|---|---|
| Exported clip | `MM.DD.YYYY - Band Name - Full Set.mp4` |
| YouTube metadata | `MM.DD.YYYY - Band Name - Full Set_metadata.json` |
| Renamed images | `MM.DD.YYYY - {Event} - phone image 01.jpg` |

All output lands in the workspace: `_processed/{event_folder_name}/` (or `default_output_dir` in settings).

---

## File size categories

| Category | Size | GUI behavior |
|---|---|---|
| `skip` | < 50 MB | Ignored |
| `phone_clip` | 50 MB – 2 GB | Not analyzed (shown as skip) |
| `short_set` | 2 – 8 GB | Analyzed |
| `medium_stream` | 8 – 25 GB | Analyzed |
| `full_stream` | 25 GB+ | Analyzed |

Videos ≤ `single_band_threshold_min` minutes (default 75) are auto-approved as a single band with no waveform review needed.

---

## Analysis tuning

If the auto-detection cuts too aggressively or misses gaps, adjust the thresholds in ⚙ Settings (or the collapsible section on the entry screen):

| Setting | Default | Fewer cuts | More cuts |
|---|---|---|---|
| Volume drop (gap_db) | 12 dB | Increase to 18–20 | Decrease to 8–10 |
| Min gap duration (gap_sec) | 30 sec | Increase to 45–60 | Decrease to 15–20 |

These can be overridden per-session on the entry screen without changing the saved config.

---

## Local files (gitignored — never committed)

| File | Purpose |
|---|---|
| `.env` | `ANTHROPIC_API_KEY` |
| `config.json` | Machine-specific config (FFmpeg paths, thresholds, calendar ID) |
| `credentials.json` | Google OAuth client secrets |
| `token.json` | Google OAuth token (auto-created on first calendar auth) |
| `known_bands.json` | Band names seen across sessions (auto-populated, also editable in Settings) |
| `venv/` | Python virtual environment |

---

## Google Calendar (optional)

Calendar lookup adds the event name automatically during scan.

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Create a project → enable **Google Calendar API**
3. Create **OAuth2 credentials** (Desktop application type)
4. Download as `credentials.json`, place in the project root
5. Add your Calendar ID in ⚙ Settings
6. On first use, a browser window opens for one-time OAuth consent

---

## CLI usage (advanced)

The original CLI is still fully functional for scripting or headless use:

```bash
source venv/bin/activate

# Single event
python process.py scan     "EVENTS/2026/04.24 - Final Friday"
python process.py analyze  "EVENTS/2026/04.24 - Final Friday"
python process.py review   "EVENTS/2026/04.24 - Final Friday"
python process.py export   "EVENTS/2026/04.24 - Final Friday"
python process.py metadata "EVENTS/2026/04.24 - Final Friday"

# Batch (year folder)
python process.py batch "EVENTS/2026/"

# Multi-year archive
python process.py deep-batch "EVENTS/" --dry-run
python process.py deep-batch "EVENTS/" --year 2026

# Rename images/short clips by EXIF camera
python process.py rename "EVENTS/2026/04.24 - Final Friday" --dry-run
python process.py rename "EVENTS/2026/04.24 - Final Friday"

# First-time config
python process.py configure
```
