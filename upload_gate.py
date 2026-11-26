"""Human gate between 'rendered' and 'uploaded'.

    python process.py approve-upload E:\\2026 list                     # what's rendered and what blocks it
    python process.py approve-upload E:\\2026 sheet  "Seems"           # contact sheet to look at (frames over the intro)
    python process.py approve-upload E:\\2026 approve "Seems"          # after you've watched it
    python process.py approve-upload E:\\2026 revoke  "Seems"
    python process.py approve-upload E:\\2026 export-queue             # writes upload_queue.json for n8n / the API

Nothing here uploads. A video is only ever queued when ALL of these hold: a person approved it, it is a
full video (not a Short), the file exists, it has a real band name, its metadata sidecar exists, and it
was rendered with the current intro settings. Re-rendering resets the approval (publish.py writes False).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from config import load_config
from publish import _event_folders, _load_manifest, _save_manifest, get_publish_dir, is_placeholder_label

SHEET_TIMES = (0, 3, 8, 13, 18, 25)


def blockers(key: str, entry: dict, publish_dir: Path, config: dict) -> list[str]:
    """Reasons this rendered output can't be approved or queued. Empty list means eligible."""
    out = []
    path = publish_dir / key
    if entry.get("kind") != "video":
        out.append("not a full video (Shorts wait for Phase 2)")
    if not path.exists():
        out.append("rendered file is missing")
    if is_placeholder_label(entry.get("label")):
        out.append("band name is a placeholder")
    if not path.with_suffix(".json").exists():
        out.append("metadata sidecar is missing")
    if float(entry.get("intro_fade", 0)) != float(config.get("publish_intro_fade_sec", 0)):
        out.append("rendered with different intro settings (re-render)")
    return out


def rows(publish_dir: Path, config: dict) -> list[dict]:
    manifest = _load_manifest(publish_dir)
    result = []
    for key, entry in sorted(manifest.items()):
        problems = blockers(key, entry, publish_dir, config)
        result.append(
            {
                "key": key,
                "label": entry.get("label"),
                "event": entry.get("event"),
                "approved": bool(entry.get("upload_approved")) and not problems,
                "blockers": problems,
            }
        )
    return result


def matches(all_rows: list[dict], names: list[str]) -> list[dict]:
    wanted = [n.lower() for n in names]
    return [r for r in all_rows if any(n in r["key"].lower() for n in wanted)]


def set_approval(publish_dir: Path, config: dict, names: list[str], approve: bool) -> list[str]:
    """Flip upload_approved for outputs whose path contains any name. Refuses blocked ones when approving."""
    manifest = _load_manifest(publish_dir)
    changed = []
    for r in matches(rows(publish_dir, config), names):
        if approve and r["blockers"]:
            print(f"  REFUSED {r['key']}: {'; '.join(r['blockers'])}")
            continue
        manifest[r["key"]]["upload_approved"] = approve
        changed.append(r["key"])
    _save_manifest(publish_dir, manifest)
    return changed


def build_queue(publish_dir: Path, config: dict) -> list[dict]:
    manifest = _load_manifest(publish_dir)
    queue = []
    for r in rows(publish_dir, config):
        if not r["approved"]:
            continue
        video = publish_dir / r["key"]
        meta = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))
        queue.append(
            {
                "video": str(video),
                "title": meta.get("title"),
                "description": meta.get("description"),
                "tags": meta.get("tags", []),
                "band": r["label"],
                "event": r["event"],
                "event_date": meta.get("event_date"),
                "rendered_at": manifest[r["key"]].get("rendered_at"),
                "youtube_video_id": manifest[r["key"]].get("youtube_video_id"),
            }
        )
    return queue


def contact_sheet(publish_dir: Path, key: str, config: dict) -> Path:
    """One JPEG of frames across the opening so a person can check the intro and the start of the set."""
    from PIL import Image, ImageDraw

    video = publish_dir / key
    review = publish_dir / "_review"
    review.mkdir(exist_ok=True)
    frames = []
    for t in SHEET_TIMES:
        f = review / f"_f{t}.png"
        subprocess.run(
            [config.get("ffmpeg_path", "ffmpeg"), "-y", "-v", "error", "-ss", str(t), "-i", str(video)]
            + ["-frames:v", "1", "-vf", "scale=640:360", str(f)],
            check=True,
        )
        frames.append((t, Image.open(f)))
    sheet = Image.new("RGB", (1280, 360 * 3), "black")
    for i, (t, im) in enumerate(frames):
        x, y = (i % 2) * 640, (i // 2) * 360
        sheet.paste(im, (x, y))
        ImageDraw.Draw(sheet).text((x + 10, y + 10), f"{t}s", fill="yellow")
    out = review / (Path(key).stem + " - review.jpg")
    sheet.save(out, quality=88)
    for t, _ in frames:
        (review / f"_f{t}.png").unlink(missing_ok=True)
    return out


def run_gate(year_folder: Path, action: str, names: list[str]) -> None:
    config = load_config()
    year_dir = Path(year_folder).resolve()
    publish_dir = get_publish_dir(next(iter(_event_folders(year_dir)), year_dir), config)
    if action == "list":
        for r in rows(publish_dir, config):
            state = "APPROVED" if r["approved"] else ("blocked " if r["blockers"] else "waiting ")
            print(f"  {state} {r['key']}" + (f"   [{'; '.join(r['blockers'])}]" if r["blockers"] else ""))
        return
    if action == "export-queue":
        queue = build_queue(publish_dir, config)
        out = publish_dir / "upload_queue.json"
        out.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        print(f"{len(queue)} approved video(s) -> {out}")
        return
    if not names:
        raise SystemExit(f"'{action}' needs at least one name (part of the output file name)")
    if action == "sheet":
        for r in matches(rows(publish_dir, config), names):
            print(contact_sheet(publish_dir, r["key"], config))
        return
    changed = set_approval(publish_dir, config, names, approve=(action == "approve"))
    print(f"{action}d {len(changed)} video(s)")
    for key in changed:
        print(f"  {key}")
