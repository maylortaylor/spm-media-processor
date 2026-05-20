import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
PRESETS_FILE = PROJECT_ROOT / 'audio_presets.json'


def load_presets() -> dict:
    if PRESETS_FILE.exists():
        with open(PRESETS_FILE) as f:
            return json.load(f)
    return {}


def save_preset(name: str, mode: str, notch_freq: int | None, strength: int, notch_harmonics: bool) -> None:
    presets = load_presets()
    presets[name] = {
        'mode': mode,
        'notch_freq': notch_freq,
        'strength': strength,
        'notch_harmonics': notch_harmonics,
    }
    with open(PRESETS_FILE, 'w') as f:
        json.dump(presets, f, indent=2)
    print(f"Preset saved: '{name}'")


def delete_preset(name: str) -> None:
    presets = load_presets()
    if name not in presets:
        print(f"No preset named '{name}'")
        return
    del presets[name]
    with open(PRESETS_FILE, 'w') as f:
        json.dump(presets, f, indent=2)
    print(f"Preset deleted: '{name}'")


def list_presets() -> None:
    presets = load_presets()
    if not presets:
        print("No presets saved yet.")
        return
    for name, p in presets.items():
        parts = [f"mode={p['mode']}"]
        if p.get('notch_freq'):
            parts.append(f"freq={p['notch_freq']}Hz")
        if p.get('notch_harmonics'):
            parts.append("harmonics=yes")
        if p['mode'] == 'auto':
            parts.append(f"strength={p.get('strength', 20)}")
        print(f"  {name!r:40s} {', '.join(parts)}")


def build_audio_filter_chain(
    mode: str,
    frequency: int | None = None,
    strength: int = 20,
    notch_harmonics: bool = False,
) -> str:
    """Return an FFmpeg -af filter string for the given noise-removal mode.

    Modes:
      auto    — FFT denoiser (afftdn); no frequency knowledge needed; best for steady whines
      notch   — Narrow notch at a specific Hz; requires frequency; optionally hits 2x harmonic
      highcut — Lowpass at 14 kHz; blunt fallback when auto leaves residual
    """
    if mode == 'auto':
        return f"afftdn=nr={strength}:nf=-40"
    if mode == 'notch':
        if frequency is None:
            raise ValueError("--notch-freq HZ is required for --mode notch")
        chain = f"equalizer=f={frequency}:t=h:width=200:g=-30"
        if notch_harmonics:
            chain += f",equalizer=f={frequency * 2}:t=h:width=200:g=-30"
        return chain
    if mode == 'highcut':
        return "lowpass=f=14000"
    raise ValueError(f"Unknown mode: {mode!r}. Choose auto, notch, or highcut.")


def clean_video_audio(
    input_file: Path,
    output_file: Path,
    filter_chain: str,
    ffmpeg_path: str = 'ffmpeg',
) -> None:
    """Re-encode only the audio stream with the given filter; video is stream-copied."""
    cmd = [
        ffmpeg_path,
        '-i', str(input_file),
        '-af', filter_chain,
        '-c:v', 'copy',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-movflags', '+faststart',
        '-y',
        str(output_file),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )


def run_clean_audio(
    target: Path,
    output_dir: Path | None = None,
    mode: str = 'auto',
    notch_freq: int | None = None,
    strength: int = 20,
    notch_harmonics: bool = False,
    preset: str | None = None,
    save_preset_name: str | None = None,
    config: dict | None = None,
) -> None:
    """Clean audio on a single .mp4 file or all .mp4 files in a folder.

    Cleaned files are written as {stem}_cleaned.mp4 — originals are never touched.
    """
    if config is None:
        from config import load_config
        config = load_config()

    # Load preset first, then let explicit CLI args override
    if preset is not None:
        presets = load_presets()
        if preset not in presets:
            print(f"Error: no preset named '{preset}'. Run with --list-presets to see available.")
            return
        p = presets[preset]
        mode = p.get('mode', mode)
        notch_freq = p.get('notch_freq', notch_freq)
        strength = p.get('strength', strength)
        notch_harmonics = p.get('notch_harmonics', notch_harmonics)
        print(f"Using preset: '{preset}'")
    else:
        mode = mode or config.get('clean_audio_mode', 'auto')
        strength = strength if strength != 20 else config.get('clean_audio_strength', 20)
        notch_freq = notch_freq or config.get('clean_audio_notch_freq', None)

    if save_preset_name is not None:
        save_preset(save_preset_name, mode, notch_freq, strength, notch_harmonics)

    ffmpeg_path = config.get('ffmpeg_path', 'ffmpeg')

    try:
        filter_chain = build_audio_filter_chain(mode, notch_freq, strength, notch_harmonics)
    except ValueError as e:
        print(f"Error: {e}")
        return

    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = sorted(target.glob('*.mp4'))
        if not files:
            print(f"No .mp4 files found in {target}")
            return
    else:
        print(f"Error: {target} is not a file or directory")
        return

    print(f"Filter: {filter_chain}")
    print(f"Processing {len(files)} file(s)...\n")

    for src in files:
        dest_dir = output_dir if output_dir else src.parent
        dest = dest_dir / f"{src.stem}_cleaned.mp4"

        if dest.exists():
            print(f"  Skip (exists): {dest.name}")
            continue

        print(f"  Cleaning: {src.name}")
        try:
            clean_video_audio(src, dest, filter_chain, ffmpeg_path)
            print(f"  Done → {dest.name}\n")
        except subprocess.CalledProcessError as e:
            print(f"  Error cleaning {src.name}:")
            print(f"  {e.stderr.decode()[:300]}\n")
