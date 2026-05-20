from pathlib import Path
import json

PROJECT_ROOT = Path(__file__).parent
CONFIG_FILE = PROJECT_ROOT / 'config.json'
KNOWN_BANDS_FILE = PROJECT_ROOT / 'known_bands.json'

DEFAULTS = {
    'ffmpeg_path': 'ffmpeg',
    'ffprobe_path': 'ffprobe',
    'google_calendar_id': '',
    'default_output_dir': None,
    'claude_model': 'claude-sonnet-4-6',
    'single_band_threshold_min': 75,
    'gap_db': 12.0,
    'gap_sec': 30.0,
    'min_segment_min': 5,
    'clean_audio_mode': 'auto',      # 'auto' | 'notch' | 'highcut'
    'clean_audio_strength': 20,      # afftdn noise_reduction (1–97)
    'clean_audio_notch_freq': None,  # Hz; only used in 'notch' mode
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return {**DEFAULTS, **json.load(f)}
    return dict(DEFAULTS)


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def load_known_bands() -> list[str]:
    if KNOWN_BANDS_FILE.exists():
        with open(KNOWN_BANDS_FILE) as f:
            return json.load(f).get('bands', [])
    return []


def save_known_band(name: str) -> None:
    bands = load_known_bands()
    if name and name not in bands and name.lower() not in ('set 1', 'set 2', 'set 3', 'unknown artist'):
        bands.append(name)
        with open(KNOWN_BANDS_FILE, 'w') as f:
            json.dump({'bands': sorted(bands)}, f, indent=2)


def get_workspace(event_folder: Path, output_dir: Path | None = None) -> Path:
    """Return (and create) the workspace dir for an event folder.

    All intermediate files (scan_result.json, audio MP3, segments JSON)
    and final clips land here — the original event folder is never written to.
    """
    cfg = load_config()
    if output_dir is None:
        default = cfg.get('default_output_dir')
        base = Path(default) if default else event_folder.parent / '_processed'
    else:
        base = Path(output_dir)
    workspace = base / event_folder.name
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace
