"""Make StPeteMusic thumbnails for vault videos.

Reads the metadata spm-media-processor already writes (scan_result.json and *_segments.json),
plans one thumbnail per set (plus one per multi-band event), and renders them with the
roboborealis-platform tool (tools/youtube/brand/render-thumbnails.ts --video ...).

    python thumbnails.py plan   --root E:\\2026 [--event 1.30]        # dry run, prints the plan
    python thumbnails.py render --root E:\\2026 [--event 1.30]        # renders the JPEGs
    python thumbnails.py approve --manifest <file> --id <id>[,<id>]  # mark rows ready to upload

Rules: never invents a band, event or date; never touches a source video; never uploads.
The upload step (n8n or YouTube API) only reads rows with "approved": true and a youtube_video_id.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLACEHOLDER = re.compile(r"^(obs_programout|c\d{3,}|set \d+|unknown|unlabell?ed|part \d+)", re.I)
VIDEO_EXT = {".mp4", ".mkv", ".mov"}
PARTIAL_EXT = {".crdownload", ".part"}
DEFAULT_REPO = Path(r"C:\Users\Suite E Studios\Documents\_dev\roboborealis-platform")
DEFAULT_OUT = Path(r"E:\_processed\thumbnails")
MANIFEST = "thumbnails_manifest.json"


def is_placeholder(label: str | None) -> bool:
    return not label or bool(PLACEHOLDER.match(label.strip()))


def short_date(event_date: str) -> str | None:
    """'01.30.2026' -> '01.30'. None when the date is missing or malformed."""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.\d{4}$", (event_date or "").strip())
    return f"{int(m.group(1)):02d}.{int(m.group(2)):02d}" if m else None


def slug(text: str) -> str:
    return re.sub(r"[^\w\- ]+", "", text).strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def derive_event_title(folder_name: str, labels: list[str]) -> str | None:
    """Only trusts a clean folder pattern; otherwise returns None so Matt names the event.
    '1.30 - FF Jan - Little Giver, Seems' -> 'FF Jan'  (event sits before a ' - ' separator)
    '2.13 - Love Burn Decompression'      -> whole name (no commas, mentions no band)
    '2.28 - Katara and Roger Thomas ...'  -> None (it contains band names)"""
    name = re.sub(r"^\s*\d{1,2}\.\d{1,2}(\.\d{2,4})?\s*[-–]?\s*", "", folder_name).strip()
    parts = [p.strip() for p in re.split(r"\s+[-–]\s+", name) if p.strip()]
    if not parts:
        return None
    candidate = parts[0]
    if len(parts) == 1 and "," in candidate:
        return None
    if any(_norm(label) and _norm(label) in _norm(candidate) for label in labels):
        return None
    return candidate


def find_full_set(workspace: Path, label: str) -> Path | None:
    for p in sorted(workspace.glob("*Full Set*.mp4")):
        if label.lower() in p.stem.lower():
            return p
    return None


def usable_video(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXT and p.suffix.lower() not in PARTIAL_EXT and p.is_file()


def hhmmss_to_sec(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parts = [float(x) for x in str(value).split(":")]
    sec = 0.0
    for part in parts:
        sec = sec * 60 + part
    return sec


def plan_event(workspace: Path) -> list[dict]:
    """One row per set, plus one event row when 2+ bands play. Rows with `issues` need Matt."""
    scan_file = workspace / "scan_result.json"
    if not scan_file.exists():
        return []
    scan = json.loads(scan_file.read_text(encoding="utf-8"))
    date = short_date(scan.get("event_date", ""))
    rows: list[dict] = []
    labels: list[str] = []
    for seg_file in sorted(workspace.glob("*_segments.json")):
        data = json.loads(seg_file.read_text(encoding="utf-8"))
        source = Path(data.get("source_file", ""))
        for seg in data.get("segments", []):
            if not seg.get("approved", True):
                continue
            label = (seg.get("label") or "").strip()
            issues = []
            if is_placeholder(label):
                issues.append("band name unknown (placeholder label)")
            if not date:
                issues.append("event date missing")
            full_set = None if is_placeholder(label) else find_full_set(workspace, label)
            video = full_set or (source if usable_video(source) else None)
            if video is None:
                issues.append("no usable video file found")
            if not is_placeholder(label):
                labels.append(label)
            rows.append(
                {
                    "id": f"{workspace.name}::{seg_file.stem}::{seg.get('id')}",
                    "kind": "set",
                    "event": scan.get("folder_name", workspace.name),
                    "title": None if is_placeholder(label) else label,
                    "date": date,
                    "template": "single",
                    "bands": [],
                    "video": str(video) if video else None,
                    "is_full_set": bool(full_set),
                    "window": None if full_set else [seg.get("start"), seg.get("end")],
                    "at": hhmmss_to_sec(data.get("thumbnail_time")),
                    "issues": issues,
                }
            )
    rows = dedupe_rows(rows)
    uniq = list(dict.fromkeys(labels))
    if len(uniq) >= 2:
        override = workspace / "event_title.txt"  # Matt's answer, when the folder name isn't clean
        title = (override.read_text(encoding="utf-8").strip() if override.exists() else None) or             derive_event_title(scan.get("folder_name", ""), uniq)
        issues = [] if title else ["event name could not be derived from the folder name"]
        if not date:
            issues.append("event date missing")
        first = next((r for r in rows if r["video"]), None)
        rows.append(
            {
                "id": f"{workspace.name}::event",
                "kind": "event",
                "event": scan.get("folder_name", workspace.name),
                "title": title,
                "date": date,
                "template": "event",
                "bands": uniq[:3],
                "video": first["video"] if first else None,
                "is_full_set": bool(first and first["is_full_set"]),
                "window": first["window"] if first else None,
                "at": None,
                "issues": issues + ([] if first else ["no usable video file found"]),
            }
        )
    return rows


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """The same set can sit in several *_segments.json (4k and 1080p copies). Keep one per
    (date, title): a Full Set export wins, then the first seen. Rows without a title are kept."""
    best: dict[tuple, dict] = {}
    out: list[dict] = []
    for r in rows:
        if r["title"] is None:
            out.append(r)
            continue
        key = (r["date"], _norm(r["title"]))
        if key not in best:
            best[key] = r
            out.append(r)
        elif r["is_full_set"] and not best[key]["is_full_set"]:
            out[out.index(best[key])] = r
            best[key] = r
    return out


def make_proxy(src: Path, window: list, work: Path) -> Path:
    """Low-res clip of just one set's window, so frame picking never looks at another band."""
    start, end = float(window[0]), float(window[1])
    out = work / f"proxy_{abs(hash((str(src), start, end)))}.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-to", str(end), "-i", str(src),
         "-vf", "scale=-2:720", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", str(out)],
        check=True, stdin=subprocess.DEVNULL,
    )
    return out


def render_row(row: dict, repo: Path, out_dir: Path, work: Path, top: int | None = None) -> Path:
    src = Path(row["video"])
    at = row.get("at")
    if row.get("window") and not row["is_full_set"]:
        start = float(row["window"][0])
        src = make_proxy(src, row["window"], work)
        at = at - start if at is not None and at >= start else None
    name = slug(f"{row['date']} - {row['title']}") + ".jpg"
    out = out_dir / name
    cmd = ["node", str(repo / "tools/youtube/brand/render-thumbnails.ts"), "--video", str(src),
           "--template", row["template"], "--title", row["title"], "--out", str(out)]
    if row["date"]:
        cmd += ["--date", row["date"]]
    if row["bands"]:
        cmd += ["--bands", "|".join(row["bands"])]
    if top:
        cmd += ["--top", str(top)]
    elif at is not None:
        m, s = divmod(at, 60)
        cmd += ["--at", f"{int(m):02d}:{s:04.1f}"]
    subprocess.run(cmd, cwd=repo, check=True, stdin=subprocess.DEVNULL)
    return out


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"items": {}}


def save_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def event_workspaces(root: Path, only: str | None) -> list[Path]:
    base = root / "_processed" if (root / "_processed").is_dir() else root
    return [p for p in sorted(base.iterdir()) if p.is_dir() and (only is None or only.lower() in p.name.lower())]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "render", "approve"])
    ap.add_argument("--root", type=Path, default=Path(r"E:\2026"))
    ap.add_argument("--event", help="only events whose folder name contains this text")
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--top", type=int, help="render N options plus a contact sheet instead of one")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--id", help="comma-separated row ids to approve")
    args = ap.parse_args(argv)
    manifest_path = args.manifest or args.out / MANIFEST

    if args.command == "approve":
        data = load_manifest(manifest_path)
        for rid in (args.id or "").split(","):
            if rid in data["items"]:
                data["items"][rid]["approved"] = True
        save_manifest(manifest_path, data)
        return 0

    rows = [r for ws in event_workspaces(args.root, args.event) for r in plan_event(ws)]
    ready = [r for r in rows if not r["issues"]]
    for r in rows:
        flag = "OK  " if not r["issues"] else "ASK "
        print(f"{flag}{r['date'] or '??.??'}  {r['template']:6}  {r['title'] or '?'}  <- {r['video']}")
        for issue in r["issues"]:
            print(f"      ! {issue}")
    print(f"\n{len(ready)} ready, {len(rows) - len(ready)} need an answer from Matt")
    if args.command == "plan":
        return 0

    if not (args.repo / "tools/youtube/brand/render-thumbnails.ts").exists():
        print(f"roboborealis-platform not found at {args.repo}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    data = load_manifest(manifest_path)
    work = Path(tempfile.mkdtemp(prefix="spm-thumbs-"))
    try:
        for r in ready:
            try:
                jpg = render_row(r, args.repo, args.out, work, args.top)
            except subprocess.CalledProcessError as e:
                print(f"FAILED {r['id']}: {e}", file=sys.stderr)
                continue
            prev = data["items"].get(r["id"], {})
            data["items"][r["id"]] = {**r, "thumbnail": str(jpg), "quality": prev.get("quality", "unreviewed"),
                                      "approved": False, "youtube_video_id": prev.get("youtube_video_id")}
            save_manifest(manifest_path, data)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
