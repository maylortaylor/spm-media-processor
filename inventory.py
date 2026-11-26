"""Read-only footage inventory per event folder.

Walks each event folder under a year dir, ffprobes every video/audio file
(cached by path+size+mtime) and writes:
  {publish_dir}/_inventory_<year>.json   full detail
  {publish_dir}/_inventory_<year>.csv    one row per event

Never modifies source folders.
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
from pathlib import Path

from config import load_config

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".mts", ".m2ts", ".avi", ".mxf"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aif", ".aiff"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".cr2", ".cr3", ".arw", ".dng"}
CAMERA_RE = re.compile(r"(canon|dji|gopro|sony|iphone|phone|obs|action)", re.I)
FULL_RE = re.compile(r"\bfull\b", re.I)


def _probe(path: Path, ffprobe: str) -> dict:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120)
        data = json.loads(out.stdout or "{}")
    except Exception:
        return {"duration": 0.0, "error": True}
    fmt = data.get("format", {})
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "duration": float(fmt.get("duration") or 0),
        "codec": v.get("codec_name", ""),
        "width": v.get("width", 0),
        "height": v.get("height", 0),
    }


def _camera(name: str) -> str:
    m = CAMERA_RE.search(name)
    if m:
        return m.group(1).lower()
    if re.match(r"^C\d{4}", name):
        return "sony"
    return "unknown"


def run_inventory(year_dir: Path, output_dir: Path | None = None) -> list[dict]:
    cfg = load_config()
    ffprobe = cfg.get("ffprobe_path", "ffprobe")
    publish_dir = output_dir or (year_dir.parent / "_processed")
    publish_dir.mkdir(parents=True, exist_ok=True)
    cache_path = publish_dir / f"_inventory_{year_dir.name}.json"
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8")).get("files", {})
        except Exception:
            cache = {}

    files: dict[str, dict] = {}
    rows: list[dict] = []
    events = sorted(p for p in year_dir.iterdir() if p.is_dir() and not p.name.startswith("_"))
    for ev in events:
        vids, auds, imgs = [], [], 0
        for f in ev.rglob("*"):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in IMAGE_EXT:
                imgs += 1
            elif ext in VIDEO_EXT or ext in AUDIO_EXT:
                st = f.stat()
                key = str(f)
                sig = f"{st.st_size}:{int(st.st_mtime)}"
                hit = cache.get(key)
                if hit and hit.get("sig") == sig:
                    info = hit
                else:
                    info = {"sig": sig, "size": st.st_size, **_probe(f, ffprobe)}
                files[key] = info
                (vids if ext in VIDEO_EXT else auds).append((f, info))

        vid_min = sum(i["duration"] for _, i in vids) / 60
        aud_min = sum(i["duration"] for _, i in auds) / 60
        cams = sorted({_camera(f.name) for f, _ in vids})
        mixer_audio = any(
            p.parent != ev and p.parent.name.lower().startswith("audio") for p, _ in auds
        ) or bool(auds)
        longest = max((i["duration"] for _, i in vids), default=0) / 60
        has_full = any(FULL_RE.search(f.name) or f.parent.name.lower().startswith("full")
                       for f, _ in vids + auds)
        no_video = not vids
        upgrade = bool(
            (len([c for c in cams if c != "unknown"]) > 1) or (mixer_audio and vids)
        )
        row = {
            "event": ev.name,
            "videos": len(vids),
            "video_minutes": round(vid_min, 1),
            "longest_video_min": round(longest, 1),
            "audio_files": len(auds),
            "audio_minutes": round(aud_min, 1),
            "photos": imgs,
            "cameras": "/".join(cams),
            "has_full_named_file": has_full,
            "no_video": no_video,
            "upgrade_candidate": upgrade,
            "probe_errors": sum(1 for _, i in vids + auds if i.get("error")),
        }
        rows.append(row)
        print(f"{ev.name}: {len(vids)} vid / {vid_min:.0f} min, {imgs} photos, cams={row['cameras']}")

    cache_path.write_text(json.dumps({"files": files, "events": rows}, indent=1), encoding="utf-8")
    csv_path = publish_dir / f"_inventory_{year_dir.name}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["event"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {csv_path}")
    return rows
