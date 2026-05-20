# CLAUDE.md — spm-media-processor

**SPM Media Processor** — processes raw live recordings from Suite E Studios (St. Pete, FL) into YouTube-ready clips for the St Pete Music channel. Handles multi-band nights where one continuous recording is split by band, named correctly, and enriched with YouTube metadata.

All operations are **non-destructive** — original event folders are never modified. All output lands in `_processed/{event_folder_name}/` (or `default_output_dir`).

## Quick Reference

```bash
# GUI (primary workflow)
python gui_server.py              # opens http://127.0.0.1:8765 in browser
# or: ./start_gui.command (Mac double-click) / start_gui.bat (Windows double-click)

# CLI (scripting / headless)
source venv/bin/activate
python process.py scan        <folder>    # classify + calendar lookup
python process.py analyze     <folder>    # detect set boundaries
python process.py review      <folder>    # browser waveform review
python process.py export      <folder>    # cut clips (stream copy, no re-encode)
python process.py metadata    <folder>    # generate YouTube metadata JSON
python process.py batch       <year-dir>  # scan → analyze → review → export per subfolder
python process.py rename      <folder>    # copy-rename images/short clips by EXIF source
python process.py deep-batch  <root-dir>  # process all years under a root directory
```

Required env var: `ANTHROPIC_API_KEY` (in `.env` or shell)
FFmpeg must be installed and in PATH (or set in `config.json` / ⚙ Settings).

---

## GUI Architecture

The GUI is a **FastAPI server** (`gui_server.py`) that serves a browser-based dashboard. It wraps the existing Python pipeline modules in-process using a `ThreadPoolExecutor` with stdout capture, streaming log output to the browser via SSE (Server-Sent Events).

### Key files

| File | Purpose |
|---|---|
| `gui_server.py` | FastAPI app: all API routes, `JobManager`, SSE, ported review routes |
| `gui/index.html` | Dashboard shell: entry screen, event card collection, settings panel, batch progress |
| `gui/static/app.js` | Wizard state machine, card rendering, SSE client, approve/edit/bulk actions |
| `gui/static/style.css` | Dark theme (matches `templates/review.html`) |
| `start_gui.command` | Mac launcher (double-click in Finder) |
| `start_gui.bat` | Windows launcher (double-click in Explorer) |
| `templates/review.html` | Existing waveform review UI — served unchanged at `/review`, opened in new tab |
| `process.py` | CLI entry point (unchanged — GUI calls modules directly) |
| `classify.py` | `scan_folder()` — Claude event classification |
| `analyze.py` | `run_analyze()` — audio extraction + silencedetect |
| `export.py` | `run_export()` — FFmpeg stream copy |
| `metadata.py` | `run_metadata()` — Claude YouTube metadata |
| `review_server.py` | Legacy CLI review server (unchanged, still works for CLI use) |
| `config.py` | `load_config()`, `save_config()`, `load_known_bands()`, `save_known_band()`, `get_workspace()` |

### API surface

**Config & bands:**
- `GET /api/config`, `POST /api/config`
- `GET /api/bands`, `POST /api/bands`, `DELETE /api/bands/{name}`
- `GET /api/browse` — opens native OS folder picker via tkinter, returns `{path}`

**Discovery & workspace:**
- `POST /api/discover` — list event subfolders, load cached scan results
- `GET /api/workspace?folder=` — scan result + status summary
- `PATCH /api/workspace/scan` — update event_name, bands, notes, confirmed flag in scan_result.json
- `GET /api/workspace/segments?folder=` — segment approval status
- `GET /api/workspace/exports?folder=` — exported MP4s + metadata

**Pipeline jobs** (all return `{job_id}`):
- `POST /api/scan`, `POST /api/scan-all`
- `POST /api/analyze`, `POST /api/analyze-batch`
- `POST /api/export`, `POST /api/export-batch`
- `POST /api/metadata`, `POST /api/metadata-batch`

**Job streaming:**
- `GET /api/job/{id}` — poll status
- `GET /api/job/{id}/stream` — SSE: `{"type":"log","line":"..."}` or structured `{"type":"folder_done","folder":"...","scan_result":{...}}`

**Review (ported from `review_server.py`):**
- `POST /api/review/start` — set active workspace/segments context
- `GET /review`, `GET /data`, `GET /scan`, `GET /bands`, `GET /audio/{f}`, `GET /peaks/{f}`, `POST /save`

### JobManager pattern

Long-running pipeline functions run in a `ThreadPoolExecutor`. Each job gets a `queue.Queue` where stdout writes are captured by `_CapturingWriter` (replaces `sys.stdout` in the job thread). The SSE route reads from this queue and yields lines to the browser. This means all existing `print()` calls in the pipeline modules appear in the browser log panel with no changes to those modules.

---

## GUI Workflow (agent context)

When working on this codebase in the context of the GUI:

1. **Entry point is a year folder** (`EVENTS/2026/`) — not a single event folder. The dashboard discovers all subfolders and shows one card per event.

2. **Scan result caching** — `POST /api/discover` loads `scan_result.json` from workspace immediately (no Claude call). Only folders without a scan result trigger `scan_folder()`.

3. **Approve-guesses pattern** — Claude's inferences (event_name, bands) are stored in `scan_result.json`. The UI lets the user edit and approve them before anything else runs. Approval sets `confirmed: true` in `scan_result.json` via `PATCH /api/workspace/scan`.

4. **Review tab pattern** — `review.html` is served at `/review` and opened in a new browser tab per event. The dashboard polls `/api/workspace/segments` every 3 seconds to detect when the user saves and closes the review tab.

5. **Batch concurrency** — `scan-all` runs up to 5 concurrent Claude calls (bounded by `threading.Semaphore(5)`). `analyze-batch` runs up to 3 concurrent (more CPU-heavy). Export is serial (disk I/O bound).

---

## CLI Workflow (agent context)

### Single event folder
```bash
python process.py scan     "<folder>"   # identify bands + event via Claude
python process.py analyze  "<folder>"   # detect set boundaries from audio
python process.py review   "<folder>"   # browser UI (auto-skipped for short videos)
python process.py export   "<folder>"   # cut clips
python process.py metadata "<folder>"   # generate YouTube metadata JSON
```

### Full year of events
```bash
python process.py batch "EVENTS/2026/"  # scan + analyze + review + export per subfolder
```

### Multi-year archive
```bash
python process.py deep-batch "EVENTS/" --dry-run   # preview first
python process.py deep-batch "EVENTS/"              # run for real
python process.py deep-batch "EVENTS/" --year 2024  # limit to one year
```

### CLI decision points
- Wrong bands in scan → re-run `scan`, or `PATCH /api/workspace/scan` from the GUI
- Too many false cuts → `analyze --gap-db 18 --gap-sec 60`
- Gaps missed → `analyze --gap-db 8 --gap-sec 20`
- After deep-batch → check the manifest JSON for errored folders, then run `review` + `export` on each

---

## Config Schema (`config.json`)

```json
{
  "ffmpeg_path": "ffmpeg",
  "ffprobe_path": "ffprobe",
  "google_calendar_id": "",
  "default_output_dir": null,
  "claude_model": "claude-sonnet-4-6",
  "single_band_threshold_min": 75,
  "gap_db": 12.0,
  "gap_sec": 30.0,
  "min_segment_min": 5
}
```

- `default_output_dir` — null = `_processed/` adjacent to each event folder; or set a global path
- `single_band_threshold_min` — videos shorter than this (min) auto-approve as single band, no review
- `gap_db` — dB drop below peak volume that signals a between-set gap (increase for fewer cuts)
- `gap_sec` — seconds of quiet required to count as a real gap (increase for fewer cuts)

---

## Workspace structure

```
_processed/
  {event_folder_name}/
    scan_result.json              ← Claude classification + approval state
    {filename}_audio.mp3          ← Extracted 64kbps audio for analysis
    {filename}_segments.json      ← Segment time ranges + labels + approval
    {filename}_peaks.json         ← Waveform peaks cache for review.html
    MM.DD.YYYY - Band - Full Set.mp4
    MM.DD.YYYY - Band - Full Set_metadata.json
    renamed/
      MM.DD.YYYY - Event - phone image 01.jpg
    rename_manifest.json
```

`scan_result.json` now includes a `confirmed` boolean (set by GUI approval) and optionally `skipped: true` (set by GUI skip action).

---

## Analysis tuning

Volume analysis pipeline:
1. Extract audio as 64kbps MP3
2. Get peak volume (dBFS)
3. Run FFmpeg `silencedetect` at threshold = `peak - gap_db`
4. Filter: keep gaps ≥ `gap_sec` seconds
5. Convert gaps to segments; discard segments < `min_segment_min` minutes

**Fewer cuts:** increase `gap_db` (18–20) and `gap_sec` (45–60)
**More cuts:** decrease `gap_db` (8–10) and `gap_sec` (15–20)

---

## File categories (by video size)

| Category | Size | Action |
|---|---|---|
| `skip` | < 50 MB | Ignored |
| `phone_clip` | 50 MB – 2 GB | Not analyzed |
| `short_set` | 2 – 8 GB | Analyzed |
| `medium_stream` | 8 – 25 GB | Analyzed |
| `full_stream` | 25 GB+ | Analyzed |

---

## Rename device labels

`rename_media.py` maps EXIF camera make/model to a label:

| Make/model contains | Label |
|---|---|
| Apple, iPhone, Samsung, Google, Pixel | `phone` |
| Canon | `canon` |
| Nikon | `nikon` |
| Sony | `sony` |
| GoPro | `gopro` |
| Unknown | `camera` |

Output: `MM.DD.YYYY - {Event} - {label} image {counter:02d}.jpg`
Context priority: event name → band names → day of week.

To add a camera model: edit `_DEVICE_PATTERNS` in `rename_media.py`.

---

## Credentials (gitignored)

| File | Purpose |
|---|---|
| `.env` | `ANTHROPIC_API_KEY` |
| `config.json` | Machine config (also editable via ⚙ Settings in GUI) |
| `credentials.json` | Google OAuth client secrets |
| `token.json` | Google OAuth token (auto-created on first calendar auth) |
| `known_bands.json` | Band names cache (auto-populated; also editable in ⚙ Settings) |
