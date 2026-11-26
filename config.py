from pathlib import Path
import json

PROJECT_ROOT = Path(__file__).parent
CONFIG_FILE = PROJECT_ROOT / "config.json"
KNOWN_BANDS_FILE = PROJECT_ROOT / "known_bands.json"

DEFAULTS = {
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "google_calendar_id": "",
    "default_output_dir": None,
    "claude_model": "claude-sonnet-4-6",
    "gemini_model": "gemini-flash-latest",
    "single_band_threshold_min": 75,
    "gap_db": 12.0,
    "gap_sec": 30.0,
    "min_segment_min": 5,
    "scan_use_ai": True,
    "clean_audio_mode": "auto",  # 'auto' | 'notch' | 'highcut'
    "clean_audio_strength": 20,  # afftdn noise_reduction (1–97)
    "clean_audio_notch_freq": None,  # Hz; only used in 'notch' mode
    # publish — YouTube-ready renders with intro/outro cards (see publish.py)
    "publish_dir": None,  # null = _processed/ next to the year folders (e.g. E:\_processed)
    # 'bumper'  = OBS 'LOGO WIDE blur' look: blurred footage + big logo + text, crossfade to/from the set
    # 'overlay' = band-name text over the clear set | 'cards' = separate title cards
    "publish_style": "bumper",
    "publish_bumper_crossfade_sec": 1.0,
    "publish_bumper_lead_sec": 0.75,  # blur + logo alone before the title text fades in
    "publish_bumper_blur": 6,  # blur radius at 1080p (OBS look was ~25)
    "publish_bumper_logo_opacity": 0.62,
    "publish_overlay_fade_sec": 1.25,  # text fade in / fade out
    "publish_overlay_hold_sec": 7.5,  # text fully visible, at the start and again before the end
    # Opening card: the set's audio plays from 0:00 under the title card (blurred footage + logo + band,
    # date, event, venue), which holds for publish_intro_hold_sec and then dissolves into the clear video
    # over publish_intro_fade_sec. Set publish_intro_fade_sec to 0 for the old short bumper.
    "publish_intro_fade_sec": 15,
    "publish_intro_hold_sec": 3,
    "publish_intro_show_date": True,
    "publish_overlay_font": None,  # null = Roboto SemiCondensed Light (same as OBS)
    "publish_overlay_logo": "auto",  # auto = add SUITE E logo only when the source isn't an OBS recording
    "publish_logo": None,  # null = {publish_dir}/_assets/logo.png
    "publish_url": "www.StPeteMusic.live/Live",
    "publish_intro_image": None,  # null = {publish_dir}/_assets/intro.png (gradient if missing)
    "publish_outro_image": None,  # null = {publish_dir}/_assets/outro.png (falls back to intro)
    "publish_font": None,  # .ttf path; null = Arial Bold / system bold
    "publish_darken": 0.45,  # 0–1 black overlay on background art so text stays readable
    "publish_text_y": 0.5,  # vertical center of the title text (0 = top, 1 = bottom)
    "publish_venue": "Suite E Studios",
    "publish_city": "St. Pete, FL",
    "publish_channel": "St Pete Music",
    "publish_intro_sec": 4,
    "publish_outro_sec": 6,
    "publish_resolution": "source",  # 'source' (4K stays 4K) | '1080p' | '4k'
    "publish_encoder": "auto",  # 'auto' probes nvenc → amf → qsv → libx264
    "shorts_intro_sec": 1.5,
    "shorts_outro_sec": 2.5,
    "shorts_max_sec": 180,  # YouTube Shorts limit, cards included
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return {**DEFAULTS, **json.load(f)}
    return dict(DEFAULTS)


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def load_known_bands() -> list[str]:
    if KNOWN_BANDS_FILE.exists():
        with open(KNOWN_BANDS_FILE) as f:
            return json.load(f).get("bands", [])
    return []


def save_known_band(name: str) -> None:
    bands = load_known_bands()
    if name and name not in bands and name.lower() not in ("set 1", "set 2", "set 3", "unknown artist"):
        bands.append(name)
        with open(KNOWN_BANDS_FILE, "w") as f:
            json.dump({"bands": sorted(bands)}, f, indent=2)


def get_workspace(event_folder: Path, output_dir: Path | None = None) -> Path:
    """Return (and create) the workspace dir for an event folder.

    All intermediate files (scan_result.json, audio MP3, segments JSON)
    and final clips land here — the original event folder is never written to.
    """
    cfg = load_config()
    if output_dir is None:
        default = cfg.get("default_output_dir")
        base = Path(default) if default else event_folder.parent / "_processed"
    else:
        base = Path(output_dir)
    workspace = base / event_folder.name
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace
