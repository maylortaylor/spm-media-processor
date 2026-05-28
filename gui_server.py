"""
FastAPI-based GUI server for spm-media-processor.

Run with: python gui_server.py  (or python3, no venv activation needed)
Opens a browser at http://127.0.0.1:8765 automatically.
"""

from __future__ import annotations

# Bootstrap: if uvicorn isn't importable, re-exec using the venv Python.
import os
import sys

try:
    import uvicorn  # noqa: F401 — just checking availability
except ModuleNotFoundError:
    from pathlib import Path

    here = Path(__file__).parent
    venv_py = here / "venv" / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    if venv_py.exists():
        os.execv(str(venv_py), [str(venv_py)] + sys.argv)
    else:
        print("venv not found. Run: python3 -m venv venv && venv/bin/pip install -r requirements.txt")
        sys.exit(1)

import asyncio
import io
import json
import os
import struct
import subprocess
import sys
import threading
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Generator
from urllib.parse import unquote

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

load_dotenv()
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# ── project imports ─────────────────────────────────────────────────────────
from config import (
    DEFAULTS,
    get_workspace,
    load_config,
    load_known_bands,
    save_config,
    save_known_band,
)
from classify import scan_folder
from analyze import run_analyze
from export import run_export
from metadata import run_metadata

HERE = Path(__file__).parent
PORT = 8765

app = FastAPI(title="SPM Media Processor")

# Serve gui static assets
app.mount("/static", StaticFiles(directory=HERE / "gui" / "static"), name="static")


# ═══════════════════════════════════════════════════════════════════════════
# Job Manager — runs pipeline functions in threads, captures stdout for SSE
# ═══════════════════════════════════════════════════════════════════════════


class _Job:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.status = "running"
        self.result: Any = None
        self.error: str | None = None
        self.lines: list[str] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._done = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _push(self, line: str) -> None:
        self.lines.append(line)
        if self._loop:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, line)

    def push_event(self, payload: dict) -> None:
        """Push a structured JSON event (type != 'log')."""
        self._push(f"__EVENT__{json.dumps(payload)}")

    def finish(self, result: Any = None, error: str | None = None) -> None:
        self.status = "done" if error is None else "error"
        self.result = result
        self.error = error
        self._done.set()
        if self._loop:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)  # sentinel

    async def stream(self) -> Generator[str, None, None]:
        self._loop = asyncio.get_running_loop()
        # If job finished before SSE connected, inject sentinel so we don't hang
        if self._done.is_set():
            self._queue.put_nowait(None)
        # Replay lines buffered before SSE connected
        for line in self.lines:
            yield _sse_line(line)
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield _sse_line(item)
        yield "data: __DONE__\n\n"


def _sse_line(text: str) -> str:
    if text.startswith("__EVENT__"):
        return f"data: {text[len('__EVENT__') :]}\n\n"
    return f"data: {json.dumps({'type': 'log', 'line': text})}\n\n"


class _CapturingWriter(io.TextIOBase):
    """Replaces sys.stdout in a job thread; each write goes to the job queue."""

    def __init__(self, job: _Job, original: Any) -> None:
        self._job = job
        self._original = original
        self._buf = ""

    def write(self, s: str) -> int:
        try:
            self._original.write(s)
            self._original.flush()
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._job._push(line.rstrip("\r"))
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._job._push(self._buf)
            self._buf = ""


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._executor = ThreadPoolExecutor(max_workers=10)

    def _run_in_thread(self, job: _Job, fn, *args, **kwargs) -> None:
        old_stdout = sys.stdout
        writer = _CapturingWriter(job, old_stdout)
        sys.stdout = writer
        try:
            result = fn(*args, **kwargs)
            writer.flush()
            job.finish(result=result)
        except Exception as exc:
            writer.flush()
            job.finish(error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        finally:
            sys.stdout = old_stdout

    def submit(self, fn, *args, **kwargs) -> str:
        job_id = str(uuid.uuid4())[:8]
        job = _Job(job_id)
        self._jobs[job_id] = job
        self._executor.submit(self._run_in_thread, job, fn, *args, **kwargs)
        return job_id

    def get(self, job_id: str) -> _Job | None:
        return self._jobs.get(job_id)


jobs = JobManager()


# ═══════════════════════════════════════════════════════════════════════════
# Review context (one active review at a time, same pattern as review_server.py)
# ═══════════════════════════════════════════════════════════════════════════

_review_ctx: dict[str, Path | None] = {"workspace": None, "segments_file": None}


# ═══════════════════════════════════════════════════════════════════════════
# Routes — frontend shell
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(HERE / "gui" / "index.html")


@app.get("/review", include_in_schema=False)
async def review_page():
    return FileResponse(HERE / "templates" / "review.html")


@app.get("/open-review", include_in_schema=False)
async def open_review_redirect(folder: str, output_dir: str = "", segments_file: str = ""):
    """Set review context then redirect to review.html — so card buttons are plain links."""
    folder_path = Path(folder)
    workspace = get_workspace(folder_path, Path(output_dir) if output_dir else None)

    if segments_file:
        seg_path = workspace / segments_file
    else:
        pending = []
        for sf in sorted(workspace.glob("*_segments.json")):
            with open(sf) as f:
                d = json.load(f)
            if not d.get("auto_approved") and not all(s.get("approved") for s in d.get("segments", [])):
                pending.append(sf)
        seg_path = (
            pending[0]
            if pending
            else sorted(workspace.glob("*_segments.json"))[0]
            if list(workspace.glob("*_segments.json"))
            else None
        )

    _review_ctx["workspace"] = workspace
    _review_ctx["segments_file"] = seg_path
    return RedirectResponse("/review")


# ═══════════════════════════════════════════════════════════════════════════
# Routes — Config & Bands
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/config")
async def get_config():
    cfg = load_config()
    # Warn about missing env vars
    warnings = []
    if not os.environ.get("GEMINI_API_KEY"):
        warnings.append("GEMINI_API_KEY is not set — scan and metadata features will fail")
    cfg["_warnings"] = warnings
    return cfg


@app.post("/api/config")
async def post_config(request: Request):
    body = await request.json()
    current = load_config()
    for k, v in body.items():
        if k.startswith("_"):
            continue
        if k in DEFAULTS or k in current:
            current[k] = v
    save_config(current)
    return {"ok": True}


@app.get("/api/bands")
async def get_bands():
    return {"bands": load_known_bands()}


@app.post("/api/bands")
async def add_band(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name required")
    save_known_band(name)
    return {"bands": load_known_bands()}


@app.delete("/api/bands/{name}")
async def delete_band(name: str):
    name = unquote(name)
    bands = load_known_bands()
    bands = [b for b in bands if b != name]
    bfile = HERE / "known_bands.json"
    with open(bfile, "w") as f:
        json.dump({"bands": sorted(bands)}, f, indent=2)
    return {"bands": bands}


@app.get("/api/browse")
async def browse_folder():
    """Open a native OS folder picker and return the selected path."""
    try:
        if sys.platform == "darwin":
            # osascript is far more reliable than tkinter on macOS — always comes to front
            result = subprocess.run(
                ["osascript", "-e", "set f to choose folder", "-e", "POSIX path of f"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            path = result.stdout.strip().rstrip("/")
        elif sys.platform == "win32":
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    '[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null; '
                    "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                    "$d.ShowDialog() | Out-Null; Write-Output $d.SelectedPath",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            path = result.stdout.strip()
        else:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            path = filedialog.askdirectory(title="Select Folder")
            root.destroy()
        return {"path": path or ""}
    except Exception as exc:
        return {"path": "", "error": str(exc)}


# ═══════════════════════════════════════════════════════════════════════════
# Routes — Discovery & Workspace State
# ═══════════════════════════════════════════════════════════════════════════


@app.post("/api/discover")
async def discover(request: Request):
    """List event subfolders in a year folder; return scan data if available."""
    body = await request.json()
    year_folder = Path(body.get("year_folder", ""))
    output_dir = body.get("output_dir") or None
    if output_dir:
        output_dir = Path(output_dir)

    if not year_folder.is_dir():
        raise HTTPException(400, f"Not a directory: {year_folder}")

    events = []
    for d in sorted(year_folder.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
            continue
        workspace = get_workspace(d, output_dir)
        scan_file = workspace / "scan_result.json"
        scan_data = None
        if scan_file.exists():
            with open(scan_file) as f:
                scan_data = json.load(f)
        events.append(
            {
                "folder": str(d),
                "folder_name": d.name,
                "workspace": str(workspace),
                "scan_result": scan_data,
                "status": _event_status(workspace),
            }
        )
    return {"events": events, "year_folder": str(year_folder)}


def _event_status(workspace: Path) -> dict:
    """Derive pipeline completion status from workspace files."""
    scan_exists = (workspace / "scan_result.json").exists()
    seg_files = list(workspace.glob("*_segments.json"))
    analyzed = len(seg_files) > 0
    exports = list(workspace.glob("*.mp4"))

    all_approved = False
    needs_review = False
    if seg_files:
        statuses = []
        for sf in seg_files:
            with open(sf) as f:
                d = json.load(f)
            if d.get("auto_approved"):
                statuses.append("approved")
            elif all(s.get("approved") for s in d.get("segments", [])):
                statuses.append("approved")
            else:
                statuses.append("pending")
        all_approved = all(s == "approved" for s in statuses)
        needs_review = any(s == "pending" for s in statuses)

    return {
        "scanned": scan_exists,
        "analyzed": analyzed,
        "needs_review": needs_review,
        "all_approved": all_approved,
        "exported": len(exports) > 0,
        "export_count": len(exports),
    }


@app.get("/api/workspace")
async def get_workspace_state(folder: str, output_dir: str = ""):
    folder_path = Path(folder)
    workspace = get_workspace(folder_path, Path(output_dir) if output_dir else None)
    scan_file = workspace / "scan_result.json"
    scan_data = None
    if scan_file.exists():
        with open(scan_file) as f:
            scan_data = json.load(f)
    return {
        "folder": folder,
        "workspace": str(workspace),
        "scan_result": scan_data,
        "status": _event_status(workspace),
    }


@app.patch("/api/workspace/scan")
async def patch_workspace_scan(request: Request):
    """Update fields in scan_result.json (event_name, bands, notes, confirmed)."""
    body = await request.json()
    folder = body.get("folder", "")
    output_dir = body.get("output_dir") or None
    if not folder:
        raise HTTPException(400, "folder required")

    folder_path = Path(folder)
    workspace = get_workspace(folder_path, Path(output_dir) if output_dir else None)
    scan_file = workspace / "scan_result.json"

    if not scan_file.exists():
        raise HTTPException(404, "scan_result.json not found — run scan first")

    with open(scan_file) as f:
        data = json.load(f)

    for field in ("event_name", "bands", "notes", "confirmed", "skipped"):
        if field in body:
            data[field] = body[field]

    # Persist any new band names to known_bands
    if "bands" in body:
        for b in body["bands"]:
            save_known_band(b)

    with open(scan_file, "w") as f:
        json.dump(data, f, indent=2)

    return data


@app.get("/api/workspace/segments")
async def get_segments_status(folder: str, output_dir: str = ""):
    folder_path = Path(folder)
    workspace = get_workspace(folder_path, Path(output_dir) if output_dir else None)
    seg_files = sorted(workspace.glob("*_segments.json"))
    results = []
    for sf in seg_files:
        with open(sf) as f:
            d = json.load(f)
        auto = d.get("auto_approved", False)
        segs = d.get("segments", [])
        all_ok = auto or all(s.get("approved") for s in segs)
        results.append(
            {
                "file": sf.name,
                "path": str(sf),
                "auto_approved": auto,
                "segment_count": len(segs),
                "all_approved": all_ok,
            }
        )
    return {"segments": results, "all_approved": all(r["all_approved"] for r in results) if results else False}


@app.get("/api/workspace/exports")
async def get_exports(folder: str, output_dir: str = ""):
    folder_path = Path(folder)
    workspace = get_workspace(folder_path, Path(output_dir) if output_dir else None)
    exports = []
    for mp4 in sorted(workspace.glob("*.mp4")):
        size_mb = mp4.stat().st_size / 1024 / 1024
        meta_file = mp4.with_suffix("").with_name(mp4.stem + "_metadata.json")
        meta = None
        if meta_file.exists():
            with open(meta_file) as f:
                meta = json.load(f)
        exports.append(
            {
                "filename": mp4.name,
                "path": str(mp4),
                "size_mb": round(size_mb, 1),
                "metadata": meta,
            }
        )
    return {"exports": exports}


# ═══════════════════════════════════════════════════════════════════════════
# Routes — Pipeline Jobs
# ═══════════════════════════════════════════════════════════════════════════


def _parse_output_dir(raw: str | None) -> Path | None:
    return Path(raw) if raw else None


@app.post("/api/scan")
async def start_scan(request: Request):
    body = await request.json()
    folder = body.get("folder", "")
    if not folder:
        raise HTTPException(400, "folder required")
    output_dir = _parse_output_dir(body.get("output_dir"))
    job_id = jobs.submit(scan_folder, Path(folder), output_dir)
    return {"job_id": job_id}


@app.post("/api/scan-all")
async def start_scan_all(request: Request):
    """Scan all event folders in a year folder (parallel, up to 5 at a time)."""
    body = await request.json()
    year_folder = Path(body.get("year_folder", ""))
    output_dir = _parse_output_dir(body.get("output_dir"))
    if not year_folder.is_dir():
        raise HTTPException(400, f"Not a directory: {year_folder}")

    # Only scan folders that don't have a scan_result yet, or force=True
    force = body.get("force", False)
    event_folders = [d for d in sorted(year_folder.iterdir()) if d.is_dir() and not d.name.startswith((".", "_"))]

    # Use a threading.Event + list so the job thread can safely read the job_id
    # without racing against the assignment on the next line after jobs.submit().
    _jid: list[str] = []
    _ready = threading.Event()

    def _scan_all_job():
        import concurrent.futures
        _ready.wait()
        my_job_id = _jid[0]

        pending = []
        for d in event_folders:
            workspace = get_workspace(d, output_dir)
            scan_file = workspace / "scan_result.json"
            if force or not scan_file.exists():
                pending.append(d)
            else:
                # Already scanned — emit a folder_done event from cached data
                with open(scan_file) as f:
                    existing = json.load(f)
                jobs.get(my_job_id).push_event(
                    {
                        "type": "folder_done",
                        "folder": d.name,
                        "folder_path": str(d),
                        "scan_result": existing,
                        "cached": True,
                    }
                )

        print(f"Scanning {len(pending)} folder(s) (skipping {len(event_folders) - len(pending)} already scanned)")

        sem = threading.Semaphore(5)
        total = len(pending)
        done_count = 0

        def _scan_one(d: Path):
            nonlocal done_count
            sem.acquire()
            try:
                print(f"\n[{d.name}] Starting scan...")
                result = scan_folder(d, output_dir)
                done_count += 1
                jobs.get(my_job_id).push_event(
                    {
                        "type": "folder_done",
                        "folder": d.name,
                        "folder_path": str(d),
                        "scan_result": result,
                        "cached": False,
                    }
                )
                jobs.get(my_job_id).push_event(
                    {
                        "type": "progress",
                        "done": done_count,
                        "total": total,
                    }
                )
            except Exception as exc:
                print(f"\n[{d.name}] Scan failed: {exc}")
                jobs.get(my_job_id).push_event(
                    {
                        "type": "folder_error",
                        "folder": d.name,
                        "folder_path": str(d),
                        "error": str(exc),
                    }
                )
            finally:
                sem.release()

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(_scan_one, d) for d in pending]
            concurrent.futures.wait(futures)

        print(f"\nScan complete: {len(event_folders)} event(s) total")
        return {"scanned": len(pending), "cached": len(event_folders) - len(pending)}

    job_id = jobs.submit(_scan_all_job)
    _jid.append(job_id)
    _ready.set()
    return {"job_id": job_id}


@app.post("/api/analyze")
async def start_analyze(request: Request):
    body = await request.json()
    folder = body.get("folder", "")
    if not folder:
        raise HTTPException(400, "folder required")
    output_dir = _parse_output_dir(body.get("output_dir"))
    cfg = load_config()
    job_id = jobs.submit(
        run_analyze,
        Path(folder),
        output_dir=output_dir,
        gap_db=float(body.get("gap_db", cfg["gap_db"])),
        gap_sec=float(body.get("gap_sec", cfg["gap_sec"])),
        single_band_threshold=float(body.get("single_band_threshold", cfg["single_band_threshold_min"])),
        non_interactive=True,
    )
    return {"job_id": job_id}


@app.post("/api/analyze-batch")
async def start_analyze_batch(request: Request):
    body = await request.json()
    folders = body.get("folders", [])
    output_dir = _parse_output_dir(body.get("output_dir"))
    cfg = load_config()
    gap_db = float(body.get("gap_db", cfg["gap_db"]))
    gap_sec = float(body.get("gap_sec", cfg["gap_sec"]))
    single_band_threshold = float(body.get("single_band_threshold", cfg["single_band_threshold_min"]))

    def _batch():
        sem = threading.Semaphore(3)
        done_count = 0
        total = len(folders)

        def _one(folder_str: str):
            nonlocal done_count
            sem.acquire()
            try:
                f = Path(folder_str)
                print(f"\n[{f.name}] Analyzing...")
                run_analyze(
                    f,
                    output_dir=output_dir,
                    gap_db=gap_db,
                    gap_sec=gap_sec,
                    single_band_threshold=single_band_threshold,
                    non_interactive=True,
                )
                done_count += 1
                jobs.get(_batch._job_id).push_event(
                    {"type": "folder_done", "folder": f.name, "folder_path": folder_str, "stage": "analyze"}
                )
                jobs.get(_batch._job_id).push_event({"type": "progress", "done": done_count, "total": total})
            except Exception as exc:
                jobs.get(_batch._job_id).push_event(
                    {"type": "folder_error", "folder": Path(folder_str).name, "error": str(exc)}
                )
            finally:
                sem.release()

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            concurrent.futures.wait([ex.submit(_one, f) for f in folders])

        return {"done": done_count, "total": total}

    job_id = jobs.submit(_batch)
    _batch._job_id = job_id
    return {"job_id": job_id}


@app.post("/api/export")
async def start_export(request: Request):
    body = await request.json()
    folder = body.get("folder", "")
    if not folder:
        raise HTTPException(400, "folder required")
    output_dir = _parse_output_dir(body.get("output_dir"))
    job_id = jobs.submit(run_export, Path(folder), output_dir=output_dir)
    return {"job_id": job_id}


@app.post("/api/export-batch")
async def start_export_batch(request: Request):
    body = await request.json()
    folders = body.get("folders", [])
    output_dir = _parse_output_dir(body.get("output_dir"))

    def _batch():
        done_count = 0
        total = len(folders)
        for folder_str in folders:
            f = Path(folder_str)
            try:
                print(f"\n[{f.name}] Exporting...")
                run_export(f, output_dir=output_dir)
                done_count += 1
                jobs.get(_batch._job_id).push_event({"type": "folder_done", "folder": f.name, "stage": "export"})
            except Exception as exc:
                jobs.get(_batch._job_id).push_event({"type": "folder_error", "folder": f.name, "error": str(exc)})
            jobs.get(_batch._job_id).push_event({"type": "progress", "done": done_count, "total": total})
        return {"done": done_count, "total": total}

    job_id = jobs.submit(_batch)
    _batch._job_id = job_id
    return {"job_id": job_id}


@app.post("/api/metadata")
async def start_metadata(request: Request):
    body = await request.json()
    folder = body.get("folder", "")
    if not folder:
        raise HTTPException(400, "folder required")
    output_dir = _parse_output_dir(body.get("output_dir"))
    job_id = jobs.submit(run_metadata, Path(folder), output_dir=output_dir)
    return {"job_id": job_id}


@app.post("/api/metadata-batch")
async def start_metadata_batch(request: Request):
    body = await request.json()
    folders = body.get("folders", [])
    output_dir = _parse_output_dir(body.get("output_dir"))

    def _batch():
        done_count = 0
        total = len(folders)
        for folder_str in folders:
            f = Path(folder_str)
            try:
                print(f"\n[{f.name}] Generating metadata...")
                run_metadata(f, output_dir=output_dir)
                done_count += 1
                jobs.get(_batch._job_id).push_event({"type": "folder_done", "folder": f.name, "stage": "metadata"})
            except Exception as exc:
                jobs.get(_batch._job_id).push_event({"type": "folder_error", "folder": f.name, "error": str(exc)})
            jobs.get(_batch._job_id).push_event({"type": "progress", "done": done_count, "total": total})
        return {"done": done_count, "total": total}

    job_id = jobs.submit(_batch)
    _batch._job_id = job_id
    return {"job_id": job_id}


# ═══════════════════════════════════════════════════════════════════════════
# Routes — Job Status & SSE
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"status": job.status, "result": job.result, "error": job.error}


@app.get("/api/job/{job_id}/stream")
async def stream_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return StreamingResponse(
        job.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# Routes — Review (ported from review_server.py ReviewHandler)
# ═══════════════════════════════════════════════════════════════════════════


@app.post("/api/review/start")
async def review_start(request: Request):
    body = await request.json()
    folder = body.get("folder", "")
    segments_file = body.get("segments_file", "")
    if not folder:
        raise HTTPException(400, "folder required")

    folder_path = Path(folder)
    output_dir = _parse_output_dir(body.get("output_dir"))
    workspace = get_workspace(folder_path, output_dir)

    if segments_file:
        seg_path = workspace / segments_file
    else:
        # Pick first pending segments file
        pending = []
        for sf in sorted(workspace.glob("*_segments.json")):
            with open(sf) as f:
                d = json.load(f)
            if not d.get("auto_approved") and not all(s.get("approved") for s in d.get("segments", [])):
                pending.append(sf)
        seg_path = pending[0] if pending else None

    _review_ctx["workspace"] = workspace
    _review_ctx["segments_file"] = seg_path
    return {"url": f"http://127.0.0.1:{PORT}/review"}


@app.get("/data")
async def review_data():
    sf = _review_ctx.get("segments_file")
    if sf and Path(sf).exists():
        with open(sf) as f:
            return JSONResponse(json.load(f))
    return JSONResponse({})


@app.get("/bands")
async def review_bands():
    return JSONResponse({"bands": load_known_bands()})


@app.get("/scan")
async def review_scan():
    ws = _review_ctx.get("workspace")
    if ws:
        sf = Path(ws) / "scan_result.json"
        if sf.exists():
            with open(sf) as f:
                return JSONResponse(json.load(f))
    return JSONResponse({})


NUM_PEAKS = 4000


def _build_peaks(audio_file: Path) -> dict:
    config = load_config()
    ffmpeg = config.get("ffmpeg_path", "ffmpeg")
    result = subprocess.run(
        [ffmpeg, "-i", str(audio_file), "-ac", "1", "-ar", "100", "-f", "f32le", "-"],
        capture_output=True,
        check=True,
    )
    count = len(result.stdout) // 4
    if count == 0:
        return {"peaks": [], "duration": 0}
    samples = struct.unpack(f"{count}f", result.stdout)
    chunk = max(1, count // NUM_PEAKS)
    peaks = [max(abs(s) for s in samples[i : i + chunk]) for i in range(0, count, chunk)]
    max_val = max(peaks) if peaks else 1.0
    return {
        "peaks": [round(p / max_val, 4) for p in peaks],
        "duration": count / 100,
    }


@app.get("/peaks/{filename:path}")
async def review_peaks(filename: str):
    ws = _review_ctx.get("workspace")
    if not ws:
        raise HTTPException(503, "No active review context")
    audio_file = Path(ws) / filename
    if not str(audio_file.resolve()).startswith(str(Path(ws).resolve())):
        raise HTTPException(403, "Access denied")
    peaks_file = audio_file.with_name(audio_file.stem + "_peaks.json")
    if not peaks_file.exists():
        print(f"  Generating peaks for {audio_file.name} (one-time, ~30s)...")
        data = _build_peaks(audio_file)
        with open(peaks_file, "w") as f:
            json.dump(data, f)
    with open(peaks_file) as f:
        return JSONResponse(json.load(f))


@app.get("/audio/{filename:path}")
async def review_audio(filename: str, request: Request):
    ws = _review_ctx.get("workspace")
    if not ws:
        raise HTTPException(503, "No active review context")
    filepath = Path(ws) / filename
    if not str(filepath.resolve()).startswith(str(Path(ws).resolve())):
        raise HTTPException(403, "Access denied")
    if not filepath.exists():
        raise HTTPException(404, "Audio file not found")

    file_size = filepath.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        parts = range_header.replace("bytes=", "").split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        length = end - start + 1

        def _iter():
            with open(filepath, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            _iter(),
            status_code=206,
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
                "Accept-Ranges": "bytes",
            },
        )

    def _iter_full():
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        _iter_full(),
        headers={
            "Content-Type": "audio/mpeg",
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
        },
    )


@app.post("/save")
async def review_save(request: Request):
    sf = _review_ctx.get("segments_file")
    if not sf:
        raise HTTPException(503, "No active review context")
    data = await request.json()
    with open(sf, "w") as f:
        json.dump(data, f, indent=2)
    for seg in data.get("segments", []):
        save_known_band(seg.get("label", ""))
    return {"status": "saved"}


# ═══════════════════════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════════════════════


def _check_env() -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        print("WARNING: GEMINI_API_KEY is not set — scan and metadata will fail")
    cfg = load_config()
    ffmpeg = cfg.get("ffmpeg_path", "ffmpeg")
    try:
        subprocess.run([ffmpeg, "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print(f'WARNING: ffmpeg not found at "{ffmpeg}" — analyze and export will fail')


def _find_free_port(start: int = PORT) -> int:
    import socket

    for port in range(start, start + 5):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


if __name__ == "__main__":
    _check_env()
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"SPM Media Processor GUI — {url}")
    print("Press Ctrl+C to stop.\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
