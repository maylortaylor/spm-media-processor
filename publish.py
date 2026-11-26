"""Publish reviewed band sets as YouTube-ready videos and Shorts.

For every approved + reviewed segment this renders:
    intro card (band name)  →  trimmed set  →  outro card (band name)
and for every Shorts range marked in the review UI, a vertical 1080x1920 clip
with short intro/outro cards and the band name on screen.

Output layout (publish_dir, default: `_processed/` next to the year folders, e.g. E:\\_processed):

    _processed/
      _assets/intro.png, outro.png         ← background art (optional; drop your own in)
      _assets/cards/                       ← generated title cards (cache)
      YouTube/MM.DD.YYYY - Band - Live at Suite E Studios.mp4  (+ .json metadata)
      Shorts/MM.DD.YYYY - Band - Short 1.mp4                    (+ .json metadata)
      publish_manifest.json                ← what was rendered from where
      _status_YYYY.csv                     ← written by `process.py status`

Originals and the per-event workspace are never modified.
"""

import csv
import json
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from analyze import guess_artist_from_filename
from config import get_workspace, load_config, load_known_bands

ANALYZABLE = ("short_set", "medium_stream", "full_stream")

# Labels that are recorder/file names or placeholders, not band names
_PLACEHOLDER_RE = re.compile(
    r"^(|set \d+|unknown artist|c\d{4}|p\d+( 4k)?|\d+k)$|obs_programout|sonywide|vlc-record|\d{2}-\d{2}-\d{4}",
    re.IGNORECASE,
)

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


# ═══════════════════════════════════════════════════════════════════════════
# Paths, state, readiness
# ═══════════════════════════════════════════════════════════════════════════


def get_publish_dir(event_folder: Path, config: dict) -> Path:
    configured = config.get("publish_dir")
    # Event folders live in a year folder (E:\2026\3.27 - ...) → publish to E:\_processed
    base = Path(configured) if configured else event_folder.resolve().parent.parent / "_processed"
    base.mkdir(parents=True, exist_ok=True)
    return base


def is_placeholder_label(label: str | None) -> bool:
    return bool(_PLACEHOLDER_RE.search((label or "").strip()))


def is_reviewed(data: dict) -> bool:
    """A segments file is reviewed once saved from the review UI.

    Files that went through the waveform review before the `reviewed` flag existed
    (not auto-approved, every segment approved) also count. Auto-approved files are
    the whole raw recording, so they need a trim pass in review first.
    """
    if data.get("reviewed"):
        return True
    segs = data.get("segments", [])
    return not data.get("auto_approved") and bool(segs) and all(s.get("approved") for s in segs)


def segment_problem(data: dict, seg: dict) -> str | None:
    """Return why a segment can't be published yet, or None if it's ready."""
    if not seg.get("approved"):
        return "needs_review: segment not approved"
    if not is_reviewed(data):
        return "needs_review: untrimmed full recording — open review, set start/end, save"
    if is_placeholder_label(seg.get("label")):
        return f"needs_label: '{seg.get('label', '')}' is not a band name"
    return None


def short_label(data: dict, short: dict) -> str:
    if short.get("label"):
        return short["label"]
    mid = (short["start"] + short["end"]) / 2
    for seg in data.get("segments", []):
        if seg["start"] <= mid <= seg["end"]:
            return seg.get("label", "")
    return ""


def _sanitize(name: str) -> str:
    for char in r'\/:*?"<>|':
        name = name.replace(char, "")
    return re.sub(r"\s+", " ", name).strip()


def _load_manifest(publish_dir: Path) -> dict:
    mf = publish_dir / "publish_manifest.json"
    if mf.exists():
        with open(mf) as f:
            return json.load(f)
    return {}


def _save_manifest(publish_dir: Path, manifest: dict) -> None:
    with open(publish_dir / "publish_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)


def _resolve_output(out_dir: Path, stem: str, key: str, manifest: dict, publish_dir: Path) -> Path:
    """Pick the output path for a render key, adding ' (2)' etc. when another source owns the name."""
    for n in range(1, 50):
        name = stem if n == 1 else f"{stem} ({n})"
        path = out_dir / f"{name}.mp4"
        rel = path.relative_to(publish_dir).as_posix()
        entry = manifest.get(rel)
        if entry and entry.get("key") == key:
            return path
        if entry is None and not path.exists():
            return path
    raise RuntimeError(f"Too many name collisions for {stem}")


# ═══════════════════════════════════════════════════════════════════════════
# Planning — what would be rendered for an event
# ═══════════════════════════════════════════════════════════════════════════


def _quality(source: Path, config: dict) -> tuple:
    """Sort key for 'highest quality copy': resolution, then bitrate, then file size."""
    try:
        r = subprocess.run(
            [config.get("ffprobe_path", "ffprobe"), "-v", "error", "-select_streams", "v:0"]
            + ["-show_entries", "stream=width,height:format=bit_rate", "-of", "json", str(source)],
            capture_output=True,
            text=True,
        )
        j = json.loads(r.stdout or "{}")
        st = (j.get("streams") or [{}])[0]
        pixels = int(st.get("width", 0)) * int(st.get("height", 0))
        bitrate = int((j.get("format") or {}).get("bit_rate") or 0)
    except (ValueError, OSError):
        pixels = bitrate = 0
    return (pixels, bitrate, source.stat().st_size if source.exists() else 0)


def dedupe_event(folder: Path, output_dir: Path | None, config: dict) -> int:
    """Recordings of the same set (same length within 2s) → keep only the highest-quality copy.

    The others are marked `"skip": "duplicate of …"` in their segments file and never published.
    Delete that key to bring one back.
    """
    files = []
    for sf in sorted(_workspace_path(folder, output_dir, config).glob("*_segments.json")):
        with open(sf) as f:
            data = json.load(f)
        if data.get("skip") and not str(data["skip"]).startswith("duplicate of"):
            continue  # skipped by hand for another reason
        files.append((sf, data))
    def labels(data: dict) -> frozenset:
        return frozenset(re.sub(r"[^a-z0-9]", "", (s.get("label") or "").lower()) for s in data.get("segments", []))

    # Same set = same length (±2s) AND same band labels. Files with no sets marked never count —
    # e.g. a raw 4K camera file is not a duplicate of the trimmed, reviewed program recording.
    groups: list[list] = []
    for item in sorted(files, key=lambda x: x[1].get("duration", 0)):
        if not item[1].get("segments"):
            groups.append([item])
            continue
        for group in groups:
            ref = group[0][1]
            if ref.get("segments") and abs(ref.get("duration", 0) - item[1].get("duration", 0)) <= 2:
                if labels(ref) == labels(item[1]):
                    group.append(item)
                    break
        else:
            groups.append([item])

    marked = 0
    for group in groups:
        ranked = sorted(group, key=lambda x: _quality(Path(x[1]["source_file"]), config), reverse=True)
        best = Path(ranked[0][1]["source_file"]).name
        for sf, data in ranked:
            want = None if Path(data["source_file"]).name == best else f"duplicate of {best}"
            if data.get("skip") == want:
                continue
            if want:
                data["skip"] = want
                marked += 1
                print(f"  {folder.name}: skipping {Path(data['source_file']).name} (lower-quality {want})")
            else:
                data.pop("skip", None)
                print(f"  {folder.name}: keeping {Path(data['source_file']).name}")
            with open(sf, "w") as f:
                json.dump(data, f, indent=2)
    return marked


def _recorded_at(source: Path) -> tuple:
    """Recording start time from OBS/Sony-style names ('02-27-2026_20-04-18', '2026-01-17 21-48-15'),
    else the file's modified time — so 'Part 1, Part 2' follow the order things happened."""
    name = source.name
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})[ _](\d{2})-(\d{2})-(\d{2})", name)
    if m:
        mo, d, y, hh, mm, ss = m.groups()
        return (0, f"{y}{mo}{d}{hh}{mm}{ss}", name)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})", name)
    if m:
        return (0, "".join(m.groups()), name)
    try:
        return (1, f"{source.stat().st_mtime:015.0f}", name)
    except OSError:
        return (2, "", name)


def plan_event(folder: Path, output_dir: Path | None = None, config: dict | None = None) -> list[dict]:
    """Return one row per segment / Short with its state and target output path."""
    config = config or load_config()
    folder = folder.resolve()
    workspace = get_workspace(folder, output_dir)
    publish_dir = get_publish_dir(folder, config)
    manifest = _load_manifest(publish_dir)
    venue = config.get("publish_venue", "Suite E Studios")

    scan = {}
    scan_file = workspace / "scan_result.json"
    if scan_file.exists():
        with open(scan_file) as f:
            scan = json.load(f)
    event_date = scan.get("event_date") or "unknown-date"

    loaded = []
    for sf in sorted(workspace.glob("*_segments.json")):
        with open(sf) as f:
            data = json.load(f)
        if not data.get("skip"):  # skip = duplicate copy or skipped by hand
            loaded.append((sf, data))
    part_tags = [re.search(r"\bp(\d+)\b", Path(d["source_file"]).name, re.IGNORECASE) for _, d in loaded]
    if loaded and all(part_tags):  # files named p0/p1/p2 → that's the running order
        order = {id(item): int(m.group(1)) for item, m in zip(loaded, part_tags)}
        loaded.sort(key=lambda x: order[id(x)])
    else:
        loaded.sort(key=lambda x: _recorded_at(Path(x[1]["source_file"])))

    # A band with several recordings in one event gets '- Part 1', '- Part 2', ... in file order
    video_counts: dict[str, int] = {}
    for _, data in loaded:
        for seg in data.get("segments", []):
            key = capitalize_words(seg.get("label", "")).lower()
            video_counts[key] = video_counts.get(key, 0) + 1
    part_no: dict[str, int] = {}
    short_no: dict[str, int] = {}

    rows = []
    claimed: set[Path] = set()
    for sf, data in loaded:
        source = Path(data["source_file"])

        items = [("video", seg, capitalize_words(seg.get("label", ""))) for seg in data.get("segments", [])]
        items += [("short", sh, capitalize_words(short_label(data, sh))) for sh in data.get("shorts", [])]
        for kind, item, label in items:
            lk = label.lower()
            if kind == "video":
                problem = segment_problem(data, item)
                stem = f"{event_date} - {_sanitize(label)} - Live at {venue}"
                if video_counts.get(lk, 0) > 1:
                    part_no[lk] = part_no.get(lk, 0) + 1
                    stem += f" - Part {part_no[lk]}"
                out_dir = publish_dir / "YouTube"
            else:
                problem = None if not is_placeholder_label(label) else f"needs_label: Short has no band name ('{label}')"
                short_no[lk] = short_no.get(lk, 0) + 1
                stem = f"{event_date} - {_sanitize(label)} - Short {short_no[lk]}"
                out_dir = publish_dir / "Shorts"
            if not source.exists():
                problem = f"missing_source: {source}"

            key = f"{source}|{kind}|{item['id']}"
            output = _resolve_output(out_dir, stem, key, manifest, publish_dir)
            while output in claimed:  # two items in this same run want the same name
                stem += " (alt)"
                output = _resolve_output(out_dir, stem, key, manifest, publish_dir)
            claimed.add(output)

            entry = manifest.get(output.relative_to(publish_dir).as_posix())
            state = "ready"
            if problem:
                state = problem.split(":")[0]
            elif output.exists():
                trim_changed = entry and (
                    abs(entry["start"] - item["start"]) > 0.5 or abs(entry["end"] - item["end"]) > 0.5
                )
                intro_changed = (
                    entry
                    and kind == "video"
                    and float(entry.get("intro_fade", 0)) != float(config.get("publish_intro_fade_sec", 0))
                )
                stale = trim_changed or intro_changed
                state = "stale" if stale else "published"
                if stale:
                    why = "trim changed" if trim_changed else "intro settings changed"
                    problem = f"stale: {why} since publish — delete the output file to re-render"

            rows.append(
                {
                    "event": folder.name,
                    "event_date": event_date,
                    "event_name": scan.get("event_name", folder.name),
                    "notes": scan.get("notes", ""),
                    "segments_file": sf,
                    "source": source,
                    "kind": kind,
                    "id": item["id"],
                    "label": label,
                    "title_mode": item.get("title_mode", "band"),
                    "event_title": item.get("event_title") or scan.get("event_name", ""),
                    "other_bands": scan.get("bands", []) + load_known_bands() + _folder_band_names(folder.name),
                    "parts": (
                        item.get("join_before", [])
                        + [{"source_file": str(source), "start": item["start"], "end": item["end"]}]
                        + item.get("join_after", [])
                    )
                    if kind == "video" and (item.get("join_before") or item.get("join_after"))
                    else None,
                    "start": item["start"],
                    "end": item["end"],
                    "state": state,
                    "reason": problem or "",
                    "output": output,
                    "key": key,
                    "publish_dir": publish_dir,
                }
            )
    return rows


# ═══════════════════════════════════════════════════════════════════════════
# Title cards (Pillow)
# ═══════════════════════════════════════════════════════════════════════════


def _load_font(size: int, config: dict):
    from PIL import ImageFont

    for path in [config.get("publish_font")] + _FONT_CANDIDATES:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _fit_lines(draw, text: str, max_w: int, start_size: int, min_size: int, config: dict):
    """Largest font size that fits text on one line, falling back to two lines."""
    for lines in ([text], _split_two(text)):
        size = start_size
        while size >= min_size:
            font = _load_font(size, config)
            if all(draw.textlength(line, font=font) <= max_w for line in lines):
                return lines, font
            size = int(size * 0.92)
    return _split_two(text), _load_font(min_size, config)


def _split_two(text: str) -> list[str]:
    words = text.split()
    if len(words) < 2:
        return [text]
    best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    return [" ".join(words[:best]), " ".join(words[best:])]


def _background(width: int, height: int, image_path: Path | None, darken: float):
    from PIL import Image, ImageOps

    if image_path and image_path.exists():
        img = ImageOps.fit(Image.open(image_path).convert("RGB"), (width, height), Image.LANCZOS)
        if darken > 0:
            img = Image.blend(img, Image.new("RGB", (width, height), (0, 0, 0)), darken)
        return img
    # Fallback: dark navy gradient matching the review UI theme
    img = Image.new("RGB", (width, height))
    top, bottom = (26, 26, 46), (8, 8, 14)
    for y in range(height):
        t = y / max(height - 1, 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        img.paste(color, (0, y, width, y + 1))
    return img


def _draw_block(img, blocks: list[tuple[str, float, tuple]], center_y: float, config: dict) -> None:
    """Draw stacked text blocks [(text, size_fraction_of_height, rgb)] centered on center_y."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    w, h = img.size
    base = min(w, h * 16 / 9) if w >= h else w * 1.6  # keep type proportional in vertical frames
    laid = []
    for i, (text, frac, color) in enumerate(blocks):
        start = int(base * frac)
        if i == 0:
            lines, font = _fit_lines(draw, text, int(w * 0.86), start, max(18, start // 3), config)
        else:
            lines, font = _fit_lines(draw, text, int(w * 0.86), start, max(14, start // 2), config)
        line_h = int(font.size * 1.15)
        laid.append((lines, font, color, line_h))
    gap = int(h * 0.025)
    total = sum(len(lines) * lh for lines, _, _, lh in laid) + gap * (len(laid) - 1)
    y = int(h * center_y - total / 2)
    for lines, font, color, line_h in laid:
        for line in lines:
            tw = draw.textlength(line, font=font)
            stroke = max(2, font.size // 22)
            draw.text(((w - tw) / 2, y), line, font=font, fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0))
            y += line_h
        y += gap


def _pretty_date(event_date: str) -> str:
    for fmt in ("%m.%d.%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(event_date, fmt)
            return f"{d.strftime('%B')} {d.day}, {d.year}"
        except ValueError:
            continue
    return event_date


def render_card(kind: str, label: str, event_date: str, width: int, height: int, out: Path, config: dict) -> Path:
    """Render an intro/outro PNG for a band."""
    assets = Path(config["_publish_dir"]) / "_assets"
    venue = config.get("publish_venue", "Suite E Studios")
    city = config.get("publish_city", "St. Pete, FL")
    channel = config.get("publish_channel", "St Pete Music")

    intro_img = Path(config.get("publish_intro_image") or assets / "intro.png")
    outro_img = Path(config.get("publish_outro_image") or assets / "outro.png")
    bg_path = outro_img if kind == "outro" and outro_img.exists() else intro_img

    img = _background(width, height, bg_path, float(config.get("publish_darken", 0.45)))
    accent = (126, 184, 255)
    if kind == "intro":
        blocks = [
            (label, 0.11, (255, 255, 255)),
            (f"Live at {venue}", 0.045, accent),
            (f"{_pretty_date(event_date)}  ·  {city}", 0.032, (220, 220, 220)),
        ]
    else:
        blocks = [
            (label, 0.09, (255, 255, 255)),
            ("Thanks for watching", 0.045, accent),
            (f"Subscribe to {channel} for more live sets", 0.032, (220, 220, 220)),
        ]
    _draw_block(img, blocks, float(config.get("publish_text_y", 0.5)), config)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def render_shorts_overlay(label: str, out: Path, config: dict) -> Path:
    """Transparent 1080x1920 overlay with the band name above the video area."""
    from PIL import Image

    img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    venue = config.get("publish_venue", "Suite E Studios")
    _draw_block(img, [(label, 0.075, (255, 255, 255)), (f"Live at {venue}", 0.032, (126, 184, 255))], 0.2, config)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# FFmpeg rendering
# ═══════════════════════════════════════════════════════════════════════════

# ── OBS-style lower-third overlay (matches the "YouTube Main Page" OBS scene) ──

_LIGHT_FONT_CANDIDATES = [
    str(Path.home() / "AppData/Local/Microsoft/Windows/Fonts/Roboto_SemiCondensed-Light.ttf"),
    "C:/Windows/Fonts/Roboto_SemiCondensed-Light.ttf",
    "C:/Windows/Fonts/RobotoCondensed-Light.ttf",
    "/Library/Fonts/Roboto_SemiCondensed-Light.ttf",
    "C:/Windows/Fonts/segoeuisl.ttf",
    "C:/Windows/Fonts/segoeuil.ttf",
]


def _light_font(size: int, config: dict):
    from PIL import ImageFont

    for path in [config.get("publish_overlay_font")] + _LIGHT_FONT_CANDIDATES:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size)
    return _load_font(size, config)


def _light_text(layer, xy, text, size, max_w, anchor, opacity, config) -> None:
    """Thin white text with a soft shadow, shrunk to fit max_w."""
    from PIL import ImageDraw, ImageFilter, Image

    font = _light_font(size, config)
    draw = ImageDraw.Draw(layer)
    while draw.textlength(text, font=font) > max_w and size > 12:
        size = int(size * 0.94)
        font = _light_font(size, config)
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    off = max(2, size // 40)
    ImageDraw.Draw(shadow).text((xy[0] + off, xy[1] + off), text, font=font, anchor=anchor, fill=(0, 0, 0, 150))
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(2, size // 25))))
    draw.text(xy, text, font=font, anchor=anchor, fill=(255, 255, 255, int(255 * opacity)))


def _source_has_logo(source: Path, config: dict) -> bool:
    """OBS program recordings already have the SUITE E bug burned in."""
    setting = config.get("publish_overlay_logo", "auto")
    if setting != "auto":
        return not bool(setting)
    return "obs" in source.name.lower()


def capitalize_words(name: str) -> str:
    """'then theres me' → 'Then Theres Me'; keeps existing caps ('MoonShow', 'JRose').

    Small joining words stay lowercase after the first word: 'Grape Culture with Toni Nagy'.
    """
    small = {"with", "and", "of", "the", "at", "a", "an", "in", "on", "for", "feat.", "ft.", "&", "x"}
    words = (name or "").split(" ")
    return " ".join(w if i and w in small else w[:1].upper() + w[1:] for i, w in enumerate(words))


def _folder_band_names(folder_name: str) -> list[str]:
    """'2.27 - FF beach terror, harlow gold, then theres me' → ['beach terror', 'harlow gold', 'then theres me']."""
    rest = re.sub(r"^\d{1,2}\.\d{1,2}\s*-?\s*", "", folder_name)
    parts = re.split(r",|&| - | and ", rest)
    return [re.sub(r"^(FF|Final Friday)\s+", "", p.strip(), flags=re.IGNORECASE) for p in parts if p.strip()]


def event_mentions_other_band(event: str, band: str, bands: list[str]) -> bool:
    """True when an event title names a different act, e.g. 'FF beach terror' on Harlow Gold's set."""
    ev, me = event.lower(), band.lower()
    for b in bands:
        bl = b.strip().lower()
        if len(bl) < 3 or bl == me or bl == ev or bl in me or me in bl or is_placeholder_label(b):
            continue
        if re.search(r"\b" + re.escape(bl) + r"\b", ev):
            return True
    return False


def title_lines(row: dict) -> tuple[str | None, str]:
    """(event line or None, main name) for the set's title_mode: 'band' | 'event' | 'both'.

    The event is left off when it's empty or names another band, so only the artist shows.
    """
    band, event = row["label"], capitalize_words((row.get("event_title") or "").strip())
    mode = row.get("title_mode", "band")
    if not event or mode == "band" or event_mentions_other_band(event, band, row.get("other_bands", [])):
        return None, band
    if mode == "event":
        return event, event
    return event, band


def render_overlay(
    label: str,
    width: int,
    height: int,
    with_logo: bool,
    out: Path,
    config: dict,
    top_label: str | None = None,
    bumper: bool = False,
    date_text: str | None = None,
) -> Path:
    """Transparent overlay: '{top} at / Suite E Studios' top-right, label + URL bottom-center, logo bottom-left.

    Positions/sizes are taken from the OBS scene at 1920x1080 and scaled for 4K.
    """
    from PIL import Image

    s = height / 1080
    venue = config.get("publish_venue", "Suite E Studios")
    url = config.get("publish_url", "www.StPeteMusic.live/Live")
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    # Measured from the OBS 'LOGO  WIDE blur' recording at 1920x1080
    if top_label:
        _light_text(img, (1870 * s, 64 * s), f"{top_label} at", int(64 * s), 1000 * s, "ra", 0.72, config)
        _light_text(img, (1870 * s, 140 * s), venue, int(64 * s), 1000 * s, "ra", 0.72, config)
    if bumper:  # logo sits mid-screen, so: name under it, URL at the very bottom
        _light_text(img, (960 * s, 860 * s if date_text else 880 * s), label, int(100 * s), 1500 * s, "mm", 0.88, config)
        if date_text:
            _light_text(img, (960 * s, 950 * s), date_text, int(58 * s), 1500 * s, "mm", 0.8, config)
        _light_text(img, (960 * s, 1032 * s), url, int(54 * s), 1500 * s, "mm", 0.72, config)
    else:
        _light_text(img, (960 * s, 838 * s), label, int(100 * s), 1500 * s, "mm", 0.86, config)
        _light_text(img, (960 * s, 930 * s), url, int(70 * s), 1500 * s, "mm", 0.75, config)

    logo_path = Path(config.get("publish_logo") or Path(config["_publish_dir"]) / "_assets" / "logo.png")
    if with_logo and logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        lw = int(99 * s)
        logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
        logo.putalpha(logo.getchannel("A").point(lambda a: int(a * 0.85)))
        img.alpha_composite(logo, (int(50 * s), int(915 * s)))

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def render_bumper_logo(width: int, height: int, out: Path, config: dict) -> Path:
    """Full-frame transparent PNG with the big translucent SUITE E logo, top-center (OBS bumper scene)."""
    from PIL import Image

    s = height / 1080
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    logo_path = Path(config.get("publish_logo") or Path(config["_publish_dir"]) / "_assets" / "logo.png")
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        lw = int(440 * s)
        logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
        opacity = float(config.get("publish_bumper_logo_opacity", 0.62))
        logo.putalpha(logo.getchannel("A").point(lambda a: int(a * opacity)))
        top = int(440 * s - logo.height / 2)  # centered a little above mid-screen, clear of the name
        img.alpha_composite(logo, ((width - lw) // 2, max(0, top)))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def render_shorts_overlay_light(label: str, out: Path, config: dict) -> Path:
    """Vertical-Short version of the overlay look: band name + venue above the video area."""
    from PIL import Image

    img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    venue = config.get("publish_venue", "Suite E Studios")
    _light_text(img, (540, 470), label, 110, 980, "mm", 0.92, config)
    _light_text(img, (540, 570), f"Live at {venue}", 56, 980, "mm", 0.8, config)
    _light_text(img, (540, 1420), config.get("publish_url", "www.StPeteMusic.live/Live"), 50, 980, "mm", 0.7, config)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


_encoder_cache: dict[str, str] = {}


def pick_encoder(config: dict) -> str:
    pref = config.get("publish_encoder", "auto")
    if pref in _encoder_cache:
        return _encoder_cache[pref]
    candidates = [pref] if pref != "auto" else ["h264_nvenc", "h264_amf", "h264_qsv", "libx264"]
    for enc in candidates:
        test = subprocess.run(
            [config.get("ffmpeg_path", "ffmpeg"), "-hide_banner", "-loglevel", "error"]
            + ["-f", "lavfi", "-i", "color=black:s=1920x1080:d=0.5", "-c:v", enc, "-f", "null", "-"],
            capture_output=True,
        )
        if test.returncode == 0:
            _encoder_cache[pref] = enc
            return enc
    _encoder_cache[pref] = "libx264"
    return "libx264"


def _encoder_args(enc: str, mbps: int) -> list[str]:
    b, peak, buf = f"{mbps}M", f"{int(mbps * 1.5)}M", f"{mbps * 3}M"
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", b, "-maxrate", peak, "-bufsize", buf]
    if enc == "h264_amf":
        return ["-c:v", enc, "-quality", "quality", "-rc", "vbr_peak", "-b:v", b, "-maxrate", peak, "-bufsize", buf]
    if enc == "h264_qsv":
        return ["-c:v", enc, "-preset", "medium", "-b:v", b, "-maxrate", peak, "-bufsize", buf]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", peak, "-bufsize", buf]


def probe(source: Path, config: dict) -> dict:
    result = subprocess.run(
        [config.get("ffprobe_path", "ffprobe"), "-v", "error", "-show_entries"]
        + ["stream=codec_type,width,height,r_frame_rate", "-of", "json", str(source)],
        capture_output=True,
        text=True,
        check=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    num, _, den = video.get("r_frame_rate", "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    if not 10 <= fps <= 61:
        fps = 30.0
    return {
        "width": int(video.get("width", 1920)),
        "height": int(video.get("height", 1080)),
        "fps": round(fps, 3),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def _run_ffmpeg(args: list[str], total_sec: float, output: Path, config: dict) -> None:
    """Run ffmpeg writing to a .partial file, printing coarse progress, then rename into place."""
    partial = output.with_name(output.stem + ".partial.mp4")
    cmd = [config.get("ffmpeg_path", "ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostats", "-y"]
    cmd += args + ["-progress", "pipe:1", "-f", "mp4", str(partial)]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        next_pct = 10
        try:
            for line in proc.stdout:
                if line.startswith("out_time_us=") and total_sec > 0:
                    try:
                        pct = int(line.split("=")[1]) / 1e6 / total_sec * 100
                    except ValueError:
                        continue
                    if pct >= next_pct:
                        print(f"      {int(pct)}%")
                        next_pct += 10
            proc.wait()
        except BaseException:
            proc.kill()
            partial.unlink(missing_ok=True)
            raise
        if proc.returncode != 0:
            err.seek(0)
            partial.unlink(missing_ok=True)
            raise RuntimeError(err.read().decode(errors="replace")[-800:])
    partial.replace(output)


def render_video(row: dict, config: dict) -> None:
    """Intro card + trimmed set + outro card → 16:9 MP4."""
    info = probe(row["source"], config)
    mode = config.get("publish_resolution", "source")
    four_k = mode == "4k" or (mode == "source" and min(info["width"], info["height"]) >= 2160)
    W, H = (3840, 2160) if four_k else (1920, 1080)
    fps = info["fps"]
    style = config.get("publish_style", "bumper")
    if row.get("parts") or (style == "bumper" and row["end"] - row["start"] >= 60):
        return _render_video_bumper(row, config, info, W, H, fps)
    if style in ("bumper", "overlay"):
        return _render_video_overlay(row, config, info, W, H, fps)
    intro_s = float(config.get("publish_intro_sec", 4))
    outro_s = float(config.get("publish_outro_sec", 6))
    dur = row["end"] - row["start"]

    cards = Path(config["_publish_dir"]) / "_assets" / "cards"
    tag = _sanitize(f"{row['event_date']} {row['label']} {W}x{H}")
    intro = render_card("intro", row["label"], row["event_date"], W, H, cards / f"{tag} intro.png", config)
    outro = render_card("outro", row["label"], row["event_date"], W, H, cards / f"{tag} outro.png", config)

    norm = f"setsar=1,fps={fps},format=yuv420p"
    fit = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"
    aformat = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    set_audio = "[1:a]" if info["has_audio"] else "[5:a]"
    graph = ";".join(
        [
            f"[0:v]scale={W}:{H},{norm},fade=t=in:st=0:d=0.6,fade=t=out:st={intro_s - 0.6}:d=0.6[v0]",
            f"[1:v]{fit},{norm},fade=t=in:st=0:d=0.5,fade=t=out:st={max(dur - 1, 0)}:d=1[v1]",
            f"[2:v]scale={W}:{H},{norm},fade=t=in:st=0:d=0.6,fade=t=out:st={outro_s - 1}:d=1[v2]",
            f"[3:a]{aformat}[a0]",
            f"{set_audio}{aformat},afade=t=in:st=0:d=0.5,afade=t=out:st={max(dur - 1.5, 0)}:d=1.5[a1]",
            f"[4:a]{aformat}[a2]",
            "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]",
        ]
    )
    silence = "anullsrc=r=48000:cl=stereo"
    args = ["-loop", "1", "-framerate", str(fps), "-t", str(intro_s), "-i", str(intro)]
    args += ["-ss", str(row["start"]), "-t", str(dur), "-i", str(row["source"])]
    args += ["-loop", "1", "-framerate", str(fps), "-t", str(outro_s), "-i", str(outro)]
    args += ["-f", "lavfi", "-t", str(intro_s), "-i", silence]
    args += ["-f", "lavfi", "-t", str(outro_s), "-i", silence]
    args += ["-f", "lavfi", "-t", str(dur), "-i", silence]
    args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]"]
    args += _encoder_args(pick_encoder(config), 45 if four_k else (16 if fps > 31 else 12))
    args += ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"]
    _run_ffmpeg(args, intro_s + dur + outro_s, row["output"], config)


def _render_video_overlay(row: dict, config: dict, info: dict, W: int, H: int, fps: float) -> None:
    """The set itself, with the band-name overlay fading in at the start and again before the end."""
    dur = row["end"] - row["start"]
    fade = float(config.get("publish_overlay_fade_sec", 2.5))
    hold = float(config.get("publish_overlay_hold_sec", 15))
    span = min(2 * fade + hold, dur)
    fade = min(fade, span / 3)

    cards = Path(config["_publish_dir"]) / "_assets" / "cards"
    with_logo = not _source_has_logo(row["source"], config)
    tag = _sanitize(f"{row['event_date']} {row['label']} {W}x{H}{' logo' if with_logo else ''}")
    top, bottom = title_lines(row)
    tag += f" {row.get('title_mode', 'band')}"
    png = render_overlay(bottom, W, H, with_logo, cards / f"{tag} overlay.png", config, top_label=top or bottom)

    def ov(idx: int, offset: float) -> str:
        shift = f",setpts=PTS+{offset}/TB" if offset else ""
        return (
            f"[{idx}:v]format=rgba,fade=t=in:st=0:d={fade}:alpha=1,"
            f"fade=t=out:st={span - fade}:d={fade}:alpha=1{shift}[o{idx}]"
        )

    fit = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"
    aformat = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    end_overlay = dur >= 2 * span + 5  # very short sets only get the opening overlay
    parts = [
        f"[0:v]{fit},setsar=1,fps={fps},fade=t=in:st=0:d=1,fade=t=out:st={max(dur - 1.5, 0)}:d=1.5[base]",
        ov(1, 0),
        "[base][o1]overlay=0:0:eof_action=pass" + ("[mid]" if end_overlay else ",format=yuv420p[v]"),
    ]
    if end_overlay:
        parts += [ov(2, round(dur - span, 3)), "[mid][o2]overlay=0:0:eof_action=pass,format=yuv420p[v]"]
    audio_in = "[0:a]" if info["has_audio"] else "[3:a]"
    parts.append(f"{audio_in}{aformat},afade=t=in:st=0:d=1,afade=t=out:st={max(dur - 2, 0)}:d=2[a]")

    args = ["-ss", str(row["start"]), "-t", str(dur), "-i", str(row["source"])]
    args += ["-loop", "1", "-framerate", str(fps), "-t", str(span), "-i", str(png)]
    args += ["-loop", "1", "-framerate", str(fps), "-t", str(span), "-i", str(png)]
    args += ["-f", "lavfi", "-t", str(dur), "-i", "anullsrc=r=48000:cl=stereo"]
    args += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]"]
    four_k = H >= 2160
    args += _encoder_args(pick_encoder(config), 45 if four_k else (16 if fps > 31 else 12))
    args += ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"]
    _run_ffmpeg(args, dur, row["output"], config)


def _render_video_bumper(row: dict, config: dict, info: dict, W: int, H: int, fps: float) -> None:
    """Recreates the OBS 'LOGO  WIDE blur' bumper at the start and end of the set.

    Start: blurred footage + big SUITE E logo, band text fades in / holds / fades out,
    then a crossfade to the clear camera. The end mirrors it. Audio plays straight through.

    A set can be several source ranges joined into one video (row["parts"], e.g. a performance
    split across two recordings); joins get a short dip through black.

    Rendered in pieces for speed: the two ~12s bumper clips get the heavy filters, the middle
    ranges are plain encodes, the audio is encoded once over everything, and the pieces are
    joined with a lossless concat (same encoder settings for every piece, so no re-encode).
    """
    ranges = [(Path(p["source_file"]), float(p["start"]), float(p["end"])) for p in row.get("parts") or []]
    ranges = ranges or [(row["source"], row["start"], row["end"])]
    dur = sum(e - s for _, s, e in ranges)
    fade = float(config.get("publish_overlay_fade_sec", 2.5))
    hold = float(config.get("publish_overlay_hold_sec", 15))
    xf = float(config.get("publish_bumper_crossfade_sec", 1.5))
    lead = float(config.get("publish_bumper_lead_sec", 1.5))  # blur + logo alone before the text appears
    join_fade = 0.4  # dip through black where two recordings meet
    text_span = 2 * fade + hold
    intro_fade = float(config.get("publish_intro_fade_sec", 0))
    intro_hold = float(config.get("publish_intro_hold_sec", 3))
    card_intro = intro_fade > 0  # opening title card that dissolves over intro_fade seconds
    if card_intro:
        intro = round(intro_hold + intro_fade, 3)  # card holds, then everything dissolves together
    else:
        intro = round(lead + text_span + 0.5 + xf, 3)  # blur → text in/hold/out → crossfade to clean
    outro = round(xf + lead + text_span, 3)  # crossfade to blur → text in/hold/out to the end
    first_len = ranges[0][2] - ranges[0][1]
    last_len = ranges[-1][2] - ranges[-1][1]
    intro = min(intro, first_len)
    has_end = dur >= intro + outro + 5 and last_len >= outro + (0 if len(ranges) > 1 else intro)
    s = H / 1080

    publish_dir = Path(config["_publish_dir"])
    cards = publish_dir / "_assets" / "cards"
    top, bottom = title_lines(row)
    date_text = _pretty_date(row["event_date"]) if card_intro and config.get("publish_intro_show_date", True) else None
    tag = _sanitize(f"{row['event_date']} {row['label']} {row.get('title_mode', 'band')} {W}x{H} d{int(bool(date_text))}")
    text_png = render_overlay(
        bottom, W, H, False, cards / f"{tag} text.png", config, top_label=top, bumper=True, date_text=date_text
    )
    logo_png = render_bumper_logo(W, H, cards / f"bumper logo {W}x{H}.png", config)

    fit = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"
    base = f"{fit},setsar=1,fps={fps},format=yuv420p"
    venc = _encoder_args(pick_encoder(config), 45 if H >= 2160 else (16 if fps > 31 else 12))
    blur = f"boxblur={max(2, int(float(config.get('publish_bumper_blur', 12)) * s))}:3,eq=brightness=-0.04:saturation=1.15"

    def bumper_clip(source: Path, start: float, length: float, kind: str, out: Path) -> None:
        if kind == "intro" and card_intro:
            # Title card is on screen from the first frame (audio plays under it); card, logo and text
            # all hold for intro_hold seconds, then dissolve together into the clear video over intro_fade.
            ramp = f"fade=t=out:st={intro_hold}:d={intro_fade}:alpha=1"
            graph = ";".join(
                [
                    f"[0:v]{base},split=2[c][b]",
                    f"[b]{blur}[bb]",
                    f"[bb][1:v]overlay=0:0:shortest=1,format=yuva420p,{ramp}[ba]",
                    "[c][ba]overlay=0:0[x]",
                    f"[2:v]format=rgba,fade=t=in:st=0:d=0.4:alpha=1,{ramp}[t]",
                    "[x][t]overlay=0:0:eof_action=pass,format=yuv420p[v]",
                ]
            )
            text_len = length
        else:
            if kind == "intro":
                crossfade = f"fade=t=out:st={length - xf}:d={xf}:alpha=1"
                text_at, edge = round(lead, 3), "fade=t=in:st=0:d=1"
            else:
                crossfade = f"fade=t=in:st=0:d={xf}:alpha=1"
                text_at, edge = round(xf + lead, 3), f"fade=t=out:st={length - 1.5}:d=1.5"
            shift = f",setpts=PTS+{text_at}/TB" if text_at else ""
            graph = ";".join(
                [
                    f"[0:v]{base},split=2[c][b]",
                    f"[b]{blur}[bb]",
                    "[bb][1:v]overlay=0:0:shortest=1,format=yuva420p," + crossfade + "[ba]",
                    "[c][ba]overlay=0:0[x]",
                    f"[2:v]format=rgba,fade=t=in:st=0:d={fade}:alpha=1,"
                    f"fade=t=out:st={text_span - fade}:d={fade}:alpha=1{shift}[t]",
                    f"[x][t]overlay=0:0:eof_action=pass,{edge},format=yuv420p[v]",
                ]
            )
            text_len = text_span
        args = ["-ss", str(start), "-t", str(length), "-i", str(source)]
        args += ["-loop", "1", "-framerate", str(fps), "-t", str(length), "-i", str(logo_png)]
        args += ["-loop", "1", "-framerate", str(fps), "-t", str(text_len), "-i", str(text_png)]
        args += ["-filter_complex", graph, "-map", "[v]", "-an"] + venc
        _run_ffmpeg(args, length, out, config)

    tmp = Path(tempfile.mkdtemp(prefix="publish_", dir=publish_dir))
    try:
        pieces = []
        print("      opening bumper...")
        pieces.append(tmp / "000_intro.mp4")
        bumper_clip(ranges[0][0], ranges[0][1], intro, "intro", pieces[-1])

        last = len(ranges) - 1
        for i, (source, start, end) in enumerate(ranges):
            mid_start = start + (intro if i == 0 else 0)
            mid_end = end - (outro if i == last and has_end else 0)
            length = round(mid_end - mid_start, 3)
            if length <= 0.1:
                continue
            print(f"      main set{f' (part {i + 1} of {last + 1})' if last else ''}...")
            fades = []
            if i > 0:
                fades.append(f"fade=t=in:st=0:d={join_fade}")
            if i < last:
                fades.append(f"fade=t=out:st={max(length - join_fade, 0)}:d={join_fade}")
            pieces.append(tmp / f"{i + 1:03d}_set.mp4")
            mid = ["-ss", str(mid_start), "-t", str(length), "-i", str(source)]
            mid += ["-vf", ",".join([base] + fades), "-an"] + venc
            _run_ffmpeg(mid, length, pieces[-1], config)

        if has_end:
            print("      closing bumper...")
            pieces.append(tmp / "999_outro.mp4")
            bumper_clip(ranges[-1][0], ranges[-1][2] - outro, outro, "outro", pieces[-1])

        print("      audio...")
        audio = tmp / "audio.m4a"
        aformat = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        a_args, chains = [], []
        for i, (source, start, end) in enumerate(ranges):
            length = round(end - start, 3)
            if info["has_audio"]:
                a_args += ["-ss", str(start), "-t", str(length), "-i", str(source)]
            else:
                a_args += ["-f", "lavfi", "-t", str(length), "-i", "anullsrc=r=48000:cl=stereo"]
            joins = []
            if i > 0:
                joins.append(f"afade=t=in:st=0:d={join_fade}")
            if i < last:
                joins.append(f"afade=t=out:st={max(length - join_fade, 0)}:d={join_fade}")
            chains.append(f"[{i}:a]{','.join([aformat] + joins)}[a{i}]")
        # With the opening card, audio starts at full level at 0:00 (no fade-in); the outro still fades out.
        lead_in = "" if card_intro else "afade=t=in:st=0:d=1,"
        ends = f"{lead_in}afade=t=out:st={max(dur - 2, 0)}:d=2"
        concat = "".join(f"[a{i}]" for i in range(len(ranges))) + f"concat=n={len(ranges)}:v=0:a=1,{ends}[a]"
        a_args += ["-filter_complex", ";".join(chains + [concat]), "-map", "[a]", "-c:a", "aac", "-b:a", "256k"]
        _run_ffmpeg(a_args, dur, audio, config)

        print("      joining...")
        concat_list = tmp / "pieces.txt"
        concat_list.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
        join = ["-f", "concat", "-safe", "0", "-i", str(concat_list), "-i", str(audio)]
        join += ["-map", "0:v", "-map", "1:a", "-c", "copy", "-shortest", "-movflags", "+faststart"]
        _run_ffmpeg(join, 0, row["output"], config)
    finally:
        for f in tmp.glob("*"):
            f.unlink(missing_ok=True)
        tmp.rmdir()


def _render_short_overlay(row: dict, config: dict, info: dict) -> None:
    """Vertical Short in the overlay look: no title cards, band name on screen the whole time."""
    W, H, fps = 1080, 1920, info["fps"]
    dur = min(row["end"] - row["start"], float(config.get("shorts_max_sec", 180)))
    cards = Path(config["_publish_dir"]) / "_assets" / "cards"
    overlay = render_shorts_overlay_light(row["label"], cards / _sanitize(f"{row['label']} short overlay.png"), config)
    aformat = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    audio_in = "[0:a]" if info["has_audio"] else "[2:a]"
    graph = ";".join(
        [
            "[0:v]split=2[s1][s2]",
            f"[s1]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=40:2,eq=brightness=-0.1[bg]",
            f"[s2]scale={W}:{H}:force_original_aspect_ratio=decrease[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[base]",
            f"[1:v]format=rgba,fade=t=in:st=0:d=1:alpha=1[ov]",
            f"[base][ov]overlay=0:0:shortest=1,setsar=1,fps={fps},format=yuv420p,"
            f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(dur - 0.8, 0)}:d=0.8[v]",
            f"{audio_in}{aformat},afade=t=in:st=0:d=0.3,afade=t=out:st={max(dur - 0.8, 0)}:d=0.8[a]",
        ]
    )
    args = ["-ss", str(row["start"]), "-t", str(dur), "-i", str(row["source"])]
    args += ["-loop", "1", "-framerate", str(fps), "-t", str(dur), "-i", str(overlay)]
    args += ["-f", "lavfi", "-t", str(dur), "-i", "anullsrc=r=48000:cl=stereo"]
    args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]"]
    args += _encoder_args(pick_encoder(config), 12)
    args += ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"]
    _run_ffmpeg(args, dur, row["output"], config)


def render_short(row: dict, config: dict) -> None:
    """Vertical 1080x1920 Short: blurred fill + centered video + band name overlay, quick intro/outro."""
    info = probe(row["source"], config)
    if config.get("publish_style", "bumper") != "cards":
        return _render_short_overlay(row, config, info)
    W, H = 1080, 1920
    fps = info["fps"]
    intro_s = float(config.get("shorts_intro_sec", 1.5))
    outro_s = float(config.get("shorts_outro_sec", 2.5))
    max_total = float(config.get("shorts_max_sec", 180))
    dur = min(row["end"] - row["start"], max_total - intro_s - outro_s)
    if dur < row["end"] - row["start"]:
        print(f"      Short trimmed to {dur:.0f}s to stay under the {max_total:.0f}s Shorts limit")

    cards = Path(config["_publish_dir"]) / "_assets" / "cards"
    tag = _sanitize(f"{row['event_date']} {row['label']} vertical")
    intro = render_card("intro", row["label"], row["event_date"], W, H, cards / f"{tag} intro.png", config)
    outro = render_card("outro", row["label"], row["event_date"], W, H, cards / f"{tag} outro.png", config)
    overlay = render_shorts_overlay(row["label"], cards / f"{tag} overlay.png", config)

    norm = f"setsar=1,fps={fps},format=yuv420p"
    aformat = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    set_audio = "[1:a]" if info["has_audio"] else "[6:a]"
    graph = ";".join(
        [
            f"[0:v]scale={W}:{H},{norm},fade=t=in:st=0:d=0.3,fade=t=out:st={intro_s - 0.3}:d=0.3[v0]",
            "[1:v]split=2[s1][s2]",
            f"[s1]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=40:2,eq=brightness=-0.1[bg]",
            f"[s2]scale={W}:{H}:force_original_aspect_ratio=decrease[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[base]",
            f"[base][3:v]overlay=0:0:shortest=1,{norm},fade=t=out:st={max(dur - 0.5, 0)}:d=0.5[v1]",
            f"[2:v]scale={W}:{H},{norm},fade=t=in:st=0:d=0.3,fade=t=out:st={outro_s - 0.6}:d=0.6[v2]",
            f"[4:a]{aformat}[a0]",
            f"{set_audio}{aformat},afade=t=in:st=0:d=0.3,afade=t=out:st={max(dur - 0.8, 0)}:d=0.8[a1]",
            f"[5:a]{aformat}[a2]",
            "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]",
        ]
    )
    silence = "anullsrc=r=48000:cl=stereo"
    args = ["-loop", "1", "-framerate", str(fps), "-t", str(intro_s), "-i", str(intro)]
    args += ["-ss", str(row["start"]), "-t", str(dur), "-i", str(row["source"])]
    args += ["-loop", "1", "-framerate", str(fps), "-t", str(outro_s), "-i", str(outro)]
    args += ["-loop", "1", "-framerate", str(fps), "-t", str(dur), "-i", str(overlay)]
    args += ["-f", "lavfi", "-t", str(intro_s), "-i", silence]
    args += ["-f", "lavfi", "-t", str(outro_s), "-i", silence]
    args += ["-f", "lavfi", "-t", str(dur), "-i", silence]
    args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]"]
    args += _encoder_args(pick_encoder(config), 12)
    args += ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"]
    _run_ffmpeg(args, intro_s + dur + outro_s, row["output"], config)


def _write_sidecar(row: dict, workspace: Path, config: dict) -> None:
    """Metadata JSON next to the video. Reuses Gemini metadata from the workspace when it exists."""
    venue = config.get("publish_venue", "Suite E Studios")
    city = config.get("publish_city", "St. Pete, FL")
    label, date = row["label"], _pretty_date(row["event_date"])
    meta = {}
    existing = workspace / f"{row['event_date']} - {_sanitize(label)} - Full Set_metadata.json"
    if row["kind"] == "video" and existing.exists():
        with open(existing) as f:
            meta = json.load(f)
    if not meta:
        title = f"{label} | Live at {venue}" + (" #shorts" if row["kind"] == "short" else "")
        meta = {
            "title": title[:100],
            "description": f"{label} performing live at {venue} in {city} on {date}.\n\nEvent: {row['event_name']}",
            "tags": ["live music", "st pete", "st petersburg florida", venue.lower(), label.lower()],
        }
    meta.update({"band": label, "event": row["event_name"], "event_date": row["event_date"], "kind": row["kind"]})
    with open(row["output"].with_suffix(".json"), "w") as f:
        json.dump(meta, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# Entry points
# ═══════════════════════════════════════════════════════════════════════════


def _event_folders(path: Path) -> list[Path]:
    """A year folder (contains event subfolders) expands to its events; an event folder is itself."""
    path = path.resolve()
    if re.match(r"^\d{1,2}\.\d{1,2}", path.name):  # event folders are named 'M.DD - ...'
        return [path]
    subs = [p for p in path.iterdir() if p.is_dir() and not p.name.startswith(("_", ".", "$"))]
    return sorted(subs, key=_date_sort_key) or [path]


def _date_sort_key(p: Path) -> tuple:
    """Calendar order for 'M.DD - ...' folders (so 2.13 sorts before 10.03)."""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})", p.name)
    return (int(m.group(1)), int(m.group(2)), p.name) if m else (99, 99, p.name)


def _workspace_path(folder: Path, output_dir: Path | None, config: dict) -> Path:
    """Same location as config.get_workspace() but without creating it."""
    base = output_dir or (Path(config["default_output_dir"]) if config.get("default_output_dir") else None)
    return (base or folder.parent / "_processed") / folder.name


def run_publish(
    target: Path,
    output_dir: Path | None = None,
    dry_run: bool = False,
    shorts: bool = True,
    videos: bool = True,
) -> dict:
    config = load_config()
    counts = {"rendered": 0, "published": 0, "blocked": 0, "failed": 0}
    for folder in _event_folders(target):
        apply_bands_file(folder, output_dir, config)
        dedupe_event(folder, output_dir, config)
        rows = plan_event(folder, output_dir, config)
        rows = [r for r in rows if (r["kind"] == "video" and videos) or (r["kind"] == "short" and shorts)]
        if not rows:
            continue
        print(f"\n[{folder.name}]")
        publish_dir = rows[0]["publish_dir"]
        config["_publish_dir"] = str(publish_dir)
        manifest = _load_manifest(publish_dir)
        workspace = get_workspace(folder, output_dir)

        for row in rows:
            name = row["output"].name
            mins = (row["end"] - row["start"]) / 60
            if row["state"] == "published":
                print(f"  [done] already published: {name}")
                counts["published"] += 1
                continue
            if row["state"] != "ready":
                print(f"  [skip] {row['kind']} '{row['label']}' ({row['source'].name}): {row['reason']}")
                counts["blocked"] += 1
                continue
            if dry_run:
                print(f"  [would render] {row['kind']}: {name} ({mins:.1f} min)")
                continue

            print(f"  [render] {row['kind']}: {name} ({mins:.1f} min) with {pick_encoder(config)}")
            row["output"].parent.mkdir(parents=True, exist_ok=True)
            try:
                (render_video if row["kind"] == "video" else render_short)(row, config)
            except (RuntimeError, subprocess.CalledProcessError) as e:
                print(f"    ERROR: {e}")
                counts["failed"] += 1
                continue
            _write_sidecar(row, workspace, config)
            manifest[row["output"].relative_to(publish_dir).as_posix()] = {
                "key": row["key"],
                "kind": row["kind"],
                "label": row["label"],
                "event": row["event"],
                "source": str(row["source"]),
                "start": row["start"],
                "end": row["end"],
                "intro_fade": float(config.get("publish_intro_fade_sec", 0)),
                "upload_approved": False,  # a person flips this after watching the render (approve-upload)
                "rendered_at": datetime.now().isoformat(timespec="seconds"),
            }
            _save_manifest(publish_dir, manifest)
            print(f"    Done -> {row['output']}")
            counts["rendered"] += 1

    print(
        f"\nRendered {counts['rendered']}, already published {counts['published']}, "
        f"waiting on review/labels {counts['blocked']}, failed {counts['failed']}."
    )
    if counts["blocked"] and not dry_run:
        run_needs_info(target, output_dir)
    return counts


def run_status(year_dir: Path, output_dir: Path | None = None) -> list[dict]:
    """Print + write a CSV of every event's progress toward YouTube publishing."""
    config = load_config()
    year_dir = year_dir.resolve()
    report = []
    for folder in _event_folders(year_dir):
        workspace = _workspace_path(folder, output_dir, config)
        scan_file = workspace / "scan_result.json"
        base = {"event": folder.name, "source": "", "kind": "", "label": "", "minutes": "", "output": ""}
        if not scan_file.exists():
            report.append({**base, "state": "needs_scan", "reason": "run scan (or Scan in the dashboard)"})
            continue
        with open(scan_file) as f:
            scan = json.load(f)
        if scan.get("skipped"):
            report.append({**base, "state": "skipped", "reason": "skipped in dashboard"})
            continue

        long_videos = [f for f in scan.get("files", []) if f.get("category") in ANALYZABLE]
        analyzed = set()
        for sf in workspace.glob("*_segments.json"):
            with open(sf) as fh:
                analyzed.add(json.load(fh).get("source_file"))
        for f in long_videos:
            if f["path"] not in analyzed:
                report.append(
                    {**base, "source": f["name"], "state": "needs_analyze", "reason": f"{f['size_gb']} GB not analyzed"}
                )
        if not long_videos and not analyzed:
            report.append({**base, "state": "no_long_videos", "reason": "only phone clips/images — Shorts candidates"})

        for row in plan_event(folder, output_dir, config):
            report.append(
                {
                    "event": row["event"],
                    "source": row["source"].name,
                    "kind": row["kind"],
                    "label": row["label"],
                    "minutes": round((row["end"] - row["start"]) / 60, 1),
                    "state": row["state"],
                    "reason": row["reason"],
                    "output": row["output"].name,
                }
            )

    order = ["needs_scan", "needs_analyze", "needs_review", "needs_label", "stale", "missing_source", "ready"]
    current = None
    for r in report:
        if r["event"] != current:
            current = r["event"]
            print(f"\n{current}")
        what = f"{r['kind'] or 'event'} '{r['label']}'" if r["label"] else (r["source"] or "event")
        mins = f" ({r['minutes']} min)" if r["minutes"] != "" else ""
        print(f"  [{r['state']:<14}] {what}{mins}  {r['reason']}")

    summary: dict[str, int] = {}
    for r in report:
        summary[r["state"]] = summary.get(r["state"], 0) + 1
    print("\nSummary:")
    for state in order + sorted(set(summary) - set(order)):
        if state in summary:
            print(f"  {state:<15} {summary[state]}")

    publish_dir = get_publish_dir(next(iter(_event_folders(year_dir)), year_dir), config)
    csv_path = publish_dir / f"_status_{year_dir.name}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["event", "state", "kind", "label", "minutes", "source", "reason", "output"])
        writer.writeheader()
        writer.writerows(report)
    print(f"\nWrote {csv_path}")
    run_needs_info(year_dir, output_dir)
    return report


# ═══════════════════════════════════════════════════════════════════════════
# Band-info review queue — sets whose band we don't know yet
# ═══════════════════════════════════════════════════════════════════════════

INFO_FIELDS = [
    "band",
    "suggestions",
    "event_name",
    "title_mode",
    "start",
    "end",
    "times_ok",
    "event",
    "date",
    "current_label",
    "minutes",
    "preview",
    "source",
    "segments_file",
    "seg_id",
]
_YES = {"yes", "y", "x", "true", "1", "ok"}


def _hms(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _parse_hms(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def _preview_sheet(source: Path, start: float, end: float, out: Path, config: dict) -> None:
    """2x2 grid of frames from across the set, to identify who's playing."""
    times = [start + (end - start) * f for f in (0.1, 0.35, 0.6, 0.85)]
    args = [config.get("ffmpeg_path", "ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"]
    for t in times:
        args += ["-ss", f"{t:.2f}", "-i", str(source)]
    cell = "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1"
    graph = ";".join(f"[{i}:v]{cell}[c{i}]" for i in range(4))
    graph += ";[c0][c1][c2][c3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[v]"
    args += ["-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-q:v", "4", str(out)]
    subprocess.run(args, capture_output=True)


BANDS_FILE = "bands.txt"


def read_bands_file(folder: Path) -> dict:
    """Parse an optional bands.txt in the event folder.

        event: Final Friday      → event name
        21-36-29 = Chriss        → any video whose filename contains '21-36-29' is Chriss
        Chriss                   → a listed band (if it's the only one, it names every unnamed set)
    """
    info = {"event": None, "mappings": [], "listed": []}
    path = folder / BANDS_FILE
    if not path.exists():
        return info
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^event\s*:", line, re.IGNORECASE):
            info["event"] = line.split(":", 1)[1].strip() or None
        elif "=" in line:
            pattern, band = (s.strip() for s in line.split("=", 1))
            if pattern and band:
                info["mappings"].append((pattern.lower(), band))
        else:
            info["listed"].append(line)
    return info


def band_suggestions(folder: Path, scan: dict) -> list[str]:
    """Likely band names for an event: bands.txt, then the folder name, then the scan's guesses."""
    bf = read_bands_file(folder)
    names = bf["listed"] + [b for _, b in bf["mappings"]] + _folder_band_names(folder.name) + scan.get("bands", [])
    seen, out = set(), []
    for n in names:
        n = capitalize_words(n.strip())
        if n and n.lower() not in seen and not is_placeholder_label(n):
            seen.add(n.lower())
            out.append(n)
    return out


def apply_bands_file(folder: Path, output_dir: Path | None, config: dict) -> int:
    """Name unnamed sets from the event folder's bands.txt. Only touches placeholder labels."""
    from config import save_known_band

    bf = read_bands_file(folder)
    if not (bf["mappings"] or bf["listed"] or bf["event"]):
        return 0
    single = bf["listed"][0] if len(bf["listed"]) == 1 and not bf["mappings"] else None
    named = 0
    for sf in sorted(_workspace_path(folder, output_dir, config).glob("*_segments.json")):
        with open(sf) as f:
            data = json.load(f)
        src = Path(data.get("source_file", "")).name.lower()
        changed = False
        for seg in data.get("segments", []):
            if not is_placeholder_label(seg.get("label")):
                continue
            band = next((b for pat, b in bf["mappings"] if pat in src), None) or single
            if band:
                seg["label"] = band
                named += 1
                changed = True
                save_known_band(band)
            if bf["event"] and not seg.get("event_title"):
                seg["event_title"] = bf["event"]
                changed = True
        if changed:
            with open(sf, "w") as f:
                json.dump(data, f, indent=2)
    if named:
        print(f"  {folder.name}: named {named} set(s) from {BANDS_FILE}")
    return named


_vertical_cache: dict[str, bool] = {}


def is_vertical(source: Path, config: dict) -> bool:
    """Portrait video (taller than wide) — these get their own band-info sheet."""
    key = str(source)
    if key not in _vertical_cache:
        if "vertical" in source.name.lower():
            _vertical_cache[key] = True
        else:
            try:
                info = probe(source, config)
                _vertical_cache[key] = info["height"] > info["width"]
            except (subprocess.CalledProcessError, OSError, ValueError):
                _vertical_cache[key] = False
    return _vertical_cache[key]


def run_needs_info(target: Path, output_dir: Path | None = None) -> Path | None:
    """Write _needs_band_info/band_info.csv + preview images for every unpublished set with no band name.

    Answers already typed into an existing sheet are kept when it's regenerated.
    """
    config = load_config()
    folders = _event_folders(target)
    if not folders:
        return None
    review_dir = get_publish_dir(folders[0], config) / "_needs_band_info"
    sheet = review_dir / "band_info.csv"
    vsheet = review_dir / "band_info_vertical.csv"

    kept: dict[tuple[str, str], dict] = {}
    for existing in (sheet, vsheet):
        if existing.exists():
            with open(existing, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    kept[(r.get("segments_file", ""), r.get("seg_id", ""))] = r

    rows, vrows = [], []  # landscape sets, vertical sets
    for folder in folders:
        scan_file = _workspace_path(folder, output_dir, config) / "scan_result.json"
        if not scan_file.exists():
            continue
        apply_bands_file(folder, output_dir, config)
        dedupe_event(folder, output_dir, config)
        with open(scan_file) as f:
            hints = " / ".join(band_suggestions(folder, json.load(f)))
        for row in plan_event(folder, output_dir, config):
            if row["kind"] != "video" or row["state"] in ("published", "missing_source"):
                continue
            if not is_placeholder_label(row["label"]):
                continue
            review_dir.mkdir(parents=True, exist_ok=True)
            preview = review_dir / f"{_sanitize(row['event'])} - {_sanitize(row['source'].stem)} - set {row['id']}.jpg"
            if not preview.exists():
                print(f"  preview: {preview.name}")
                _preview_sheet(row["source"], row["start"], row["end"], preview, config)
            key = (str(row["segments_file"]), str(row["id"]))
            old = kept.get(key, {})
            from_name = guess_artist_from_filename(row["source"].stem)
            if from_name and re.fullmatch(r"(p|part)\s*\d+.*", from_name, re.IGNORECASE):
                from_name = None  # 'P1', 'Part 2 Comedy' are recorder/part names, not bands
            row_hints = " / ".join(h for h in [f"{from_name} (filename)" if from_name else "", hints] if h)
            (vrows if is_vertical(row["source"], config) else rows).append(
                {
                    "band": old.get("band", ""),
                    "suggestions": row_hints,
                    "event_name": old.get("event_name") or row["event_name"],
                    "title_mode": old.get("title_mode") or row.get("title_mode", "band"),
                    "start": old.get("start") or _hms(row["start"]),
                    "end": old.get("end") or _hms(row["end"]),
                    "times_ok": old.get("times_ok", ""),
                    "event": row["event"],
                    "date": row["event_date"],
                    "current_label": row["label"],
                    "minutes": round((row["end"] - row["start"]) / 60, 1),
                    "preview": preview.name,
                    "source": row["source"].name,
                    "segments_file": key[0],
                    "seg_id": key[1],
                }
            )

    if not rows and not vrows:
        print("\nBand info: every set has a band name.")
        return None
    for path, data in ((sheet, rows), (vsheet, vrows)):
        if not data:
            path.unlink(missing_ok=True)
            continue
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=INFO_FIELDS)
            writer.writeheader()
            writer.writerows(data)
    (review_dir / "README.txt").write_text(
        "Sets we don't know the band for\n"
        "===============================\n\n"
        "1. Open each preview .jpg (4 frames from across the set) to see who is playing.\n"
        "2. In band_info.csv fill in:\n"
        "     band        the artist name (required)\n"
        "     event_name  the event, if it should be shown (prefilled from the dashboard)\n"
        "     title_mode  band | event | both  (what the intro/outro says)\n"
        "     start/end   H:MM:SS - trim to just this band's set\n"
        "     times_ok    yes = start/end are right, publish without opening Review\n"
        f'3. Run:  python process.py apply-info "{sheet}"   (or ask Claude to apply it)\n'
        "\nTip: drop a bands.txt in an event folder and those sets get named automatically:\n"
        "     event: Final Friday        (optional - the event name)\n"
        "     21-36-29 = Chriss          (part of a video's filename = band)\n"
        "     Chriss                     (a lone band name = every unnamed set in that folder)\n"
        "Rows you leave blank are skipped and kept for next time.\n"
        "\nVertical (portrait) recordings are listed separately in band_info_vertical.csv.\n",
        encoding="utf-8",
    )
    print(f"\nBand info needed for {len(rows)} landscape set(s): {sheet}")
    if vrows:
        print(f"Band info needed for {len(vrows)} vertical set(s): {vsheet}")
    return sheet


def run_apply_info(sheet: Path | None = None) -> int:
    """Write answers from band_info.csv back into the segments JSON files."""
    from config import save_known_band

    config = load_config()
    if sheet is None:
        base = Path(config["publish_dir"]) if config.get("publish_dir") else None
        if base is None:
            raise SystemExit("Pass the path to band_info.csv (publish_dir isn't set in config).")
        sheet = base / "_needs_band_info" / "band_info.csv"
    # A folder (or band_info.csv) means both sheets; band_info_vertical.csv alone means just that one
    folder = sheet if sheet.is_dir() else sheet.parent
    sheets = [sheet] if sheet.is_file() and sheet.name != "band_info.csv" else []
    sheets = sheets or [s for s in (folder / "band_info.csv", folder / "band_info_vertical.csv") if s.exists()]
    answers = []
    for s in sheets:
        with open(s, newline="", encoding="utf-8-sig") as f:
            answers += [r for r in csv.DictReader(f) if (r.get("band") or "").strip()]

    applied = 0
    for r in answers:
        sf = Path(r["segments_file"])
        if not sf.exists():
            print(f"  missing segments file, skipped: {sf}")
            continue
        with open(sf) as f:
            data = json.load(f)
        seg = next((s for s in data.get("segments", []) if str(s["id"]) == r["seg_id"]), None)
        if not seg:
            print(f"  set {r['seg_id']} not found in {sf.name}, skipped")
            continue
        seg["label"] = r["band"].strip()
        seg["title_mode"] = (r.get("title_mode") or "band").strip().lower()
        if (r.get("event_name") or "").strip():
            seg["event_title"] = r["event_name"].strip()
        start, end = _parse_hms(r.get("start", "")), _parse_hms(r.get("end", ""))
        if start is not None and end is not None and end > start:
            seg["start"], seg["end"] = start, min(end, data.get("duration", end))
            seg["start_hhmmss"], seg["end_hhmmss"] = _hms(seg["start"]), _hms(seg["end"])
        if (r.get("times_ok") or "").strip().lower() in _YES:
            seg["approved"] = True
            data["reviewed"] = True
        with open(sf, "w") as f:
            json.dump(data, f, indent=2)
        save_known_band(seg["label"])
        ready = "ready to publish" if data.get("reviewed") else "still needs a trim in Review"
        print(f"  {r['event']}: {r['current_label']} -> {seg['label']} ({ready})")
        applied += 1
    print(f"\nApplied band info to {applied} set(s).")
    return applied
