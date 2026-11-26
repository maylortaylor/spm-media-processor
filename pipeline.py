"""Safe scan + analyze runner for event folders that haven't been processed yet.

    python process.py scan-new E:\\2026 --dry-run     # list what would run, touch nothing
    python process.py scan-new E:\\2026               # scan + analyze, smallest folders first

Why this exists instead of `batch` / `deep-batch`: those re-scan folders that are already scanned
(overwriting scan_result.json and its GUI-confirmed names), and they prompt with input().
This runner:
  * only touches folders with no scan_result.json (scan) or with videos that have no *_segments.json (analyze)
  * never prompts (analyze runs non-interactive)
  * skips folders matching pipeline_scope.json (karaoke, film, WUSF jazz, clip-only folders)
  * is resumable: state comes from files on disk, so a stop or crash loses nothing
  * stops cleanly when a file named `pipeline.stop` appears in the workspace root
It never exports, renders or uploads anything and never writes inside an event folder.
"""

from __future__ import annotations

import fnmatch
import json
import time
import traceback
from datetime import datetime
from pathlib import Path

from analyze import PROCESSABLE
from classify import VIDEO_EXTENSIONS

SCOPE_FILE = Path(__file__).with_name("pipeline_scope.json")


def load_scope(path: Path = SCOPE_FILE) -> dict:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {"exclude": data.get("exclude", []), "min_video_mb": data.get("min_video_mb", 50)}


def is_excluded(name: str, patterns: list[str]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, p.lower()) for p in patterns)


def largest_video_mb(folder: Path) -> float:
    best = 0
    for f in folder.rglob("*"):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
            best = max(best, f.stat().st_size)
    return best / (1024 * 1024)


def total_video_gb(folder: Path) -> float:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS) / 1024**3


def unanalyzed_videos(workspace: Path) -> list[str]:
    """Names of processable videos in scan_result.json that have no <stem>_segments.json yet."""
    scan_file = workspace / "scan_result.json"
    if not scan_file.exists():
        return []
    scan = json.loads(scan_file.read_text(encoding="utf-8"))
    return [
        f["name"]
        for f in scan.get("files", [])
        if f.get("type") == "video"
        and f.get("category") in PROCESSABLE
        and not (workspace / f"{Path(f['name']).stem}_segments.json").exists()
    ]


def plan(root: Path, scope: dict, processed_dir: Path | None = None) -> list[dict]:
    """One row per event folder: what to do (scan+analyze, analyze, nothing) and why."""
    processed = processed_dir or root / "_processed"
    rows = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        ws = processed / folder.name
        scanned = (ws / "scan_result.json").exists()
        row = {"folder": str(folder), "name": folder.name, "workspace": str(ws), "action": None, "reason": ""}
        if is_excluded(folder.name, scope["exclude"]):
            row["reason"] = "excluded by pipeline_scope.json"
        elif largest_video_mb(folder) < scope["min_video_mb"]:
            row["reason"] = f"no video over {scope['min_video_mb']} MB"
        elif not scanned:
            row["action"] = "scan+analyze"
        elif unanalyzed_videos(ws):
            row["action"] = "analyze"
            row["reason"] = f"{len(unanalyzed_videos(ws))} video(s) without segments"
        else:
            row["reason"] = "already scanned and analyzed"
        row["gb"] = round(total_video_gb(folder), 1) if row["action"] else None
        rows.append(row)
    return sorted(rows, key=lambda r: (r["action"] is None, r["gb"] or 0))


def run_events(rows: list[dict], processed_dir: Path, stop_file: Path) -> dict:
    """Process rows in order. Every failure is recorded and the run continues."""
    from analyze import run_analyze
    from classify import scan_folder

    started = datetime.now()
    results = []
    todo = [r for r in rows if r["action"]]
    for i, row in enumerate(todo, 1):
        if stop_file.exists():
            print(f"Stop file found ({stop_file}); stopping before {row['name']}.")
            break
        folder = Path(row["folder"])
        print(f"\n[{i}/{len(todo)}] {row['name']}  ({row['action']}, {row['gb']} GB)")
        t0 = time.time()
        res = {"name": row["name"], "action": row["action"], "ok": True, "error": None}
        try:
            if row["action"] == "scan+analyze":
                if (Path(row["workspace"]) / "scan_result.json").exists():
                    raise RuntimeError("scan_result.json appeared since planning; refusing to overwrite it")
                scan_folder(folder, processed_dir)
            run_analyze(folder, processed_dir, non_interactive=True)
        except Exception as e:  # keep going; the report lists what failed
            res.update(ok=False, error=f"{type(e).__name__}: {e}")
            traceback.print_exc()
        res["seconds"] = round(time.time() - t0)
        res["still_unanalyzed"] = unanalyzed_videos(Path(row["workspace"]))
        results.append(res)
    return {
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now().isoformat(timespec="seconds"),
        "processed": len(results),
        "failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }


def run_scan_new(root: Path, dry_run: bool = False, limit: int | None = None) -> None:
    root = Path(root).resolve()
    processed = root / "_processed"
    scope = load_scope()
    rows = plan(root, scope, processed)
    if limit is not None:  # trial run: only the first N (smallest) folders
        seen = 0
        for r in rows:
            if r["action"] and (seen := seen + 1) > limit:
                r["action"], r["reason"] = None, "beyond --limit"
    todo = [r for r in rows if r["action"]]
    print(f"{len(todo)} folder(s) to process, {len(rows) - len(todo)} skipped\n")
    for r in rows:
        mark = r["action"] or "skip"
        extra = f"{r['gb']} GB" if r["action"] else r["reason"]
        print(f"  {mark:13} {r['name']}   [{extra}]")
    if dry_run or not todo:
        return
    processed.mkdir(exist_ok=True)
    report = run_events(rows, processed, processed / "pipeline.stop")
    out = processed / f"pipeline_report_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nDone: {report['processed']} processed, {report['failed']} failed. Report: {out}")
    print("Next: python process.py status", root, " then review in the GUI: python process.py review <event folder>")
