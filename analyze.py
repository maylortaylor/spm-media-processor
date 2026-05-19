import json
import re
import subprocess
import sys
from pathlib import Path

from config import load_config, get_workspace

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.mts', '.m2ts', '.wmv', '.flv', '.webm'}
PROCESSABLE = {'short_set', 'medium_stream', 'full_stream'}


def ffmpeg_cmd(config: dict) -> str:
    return config.get('ffmpeg_path', 'ffmpeg')


def ffprobe_cmd(config: dict) -> str:
    return config.get('ffprobe_path', 'ffprobe')


def get_duration(video_file: Path, config: dict) -> float:
    result = subprocess.run(
        [ffprobe_cmd(config), '-v', 'quiet', '-print_format', 'json', '-show_format', str(video_file)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(result.stdout)['format']['duration'])


def extract_audio(video_file: Path, audio_file: Path, config: dict) -> None:
    print(f'  Extracting audio → {audio_file.name}')
    subprocess.run(
        [ffmpeg_cmd(config), '-i', str(video_file), '-vn', '-acodec', 'libmp3lame', '-ab', '64k', '-y', str(audio_file)],
        check=True, capture_output=True,
    )


def get_max_volume(audio_file: Path, config: dict) -> float:
    result = subprocess.run(
        [ffmpeg_cmd(config), '-i', str(audio_file), '-af', 'volumedetect', '-f', 'null', '-'],
        capture_output=True, text=True,
    )
    match = re.search(r'max_volume: ([-\d.]+) dB', result.stderr)
    if not match:
        return -3.0
    return float(match.group(1))


def detect_quiet_regions(audio_file: Path, threshold_db: float, min_duration: float, config: dict) -> list[dict]:
    """Return list of {start, end} dicts for regions below threshold_db for at least min_duration seconds."""
    result = subprocess.run(
        [ffmpeg_cmd(config), '-i', str(audio_file),
         '-af', f'silencedetect=noise={threshold_db:.1f}dB:d={min_duration}',
         '-f', 'null', '-'],
        capture_output=True, text=True,
    )

    starts = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', result.stderr)]
    ends_raw = re.findall(r'silence_end: ([\d.]+)', result.stderr)
    ends = [float(x) for x in ends_raw]

    gaps = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else None
        gaps.append({'start': start, 'end': end})
    return gaps


def gaps_to_segments(gaps: list[dict], total_duration: float, min_segment_sec: float) -> list[dict]:
    """Convert quiet gaps into active performance segments."""
    segments = []
    cursor = 0.0

    for gap in gaps:
        seg_end = gap['start']
        if seg_end - cursor >= min_segment_sec:
            segments.append({'start': cursor, 'end': seg_end})
        cursor = gap['end'] if gap['end'] is not None else total_duration

    if total_duration - cursor >= min_segment_sec:
        segments.append({'start': cursor, 'end': total_duration})

    return segments


def seconds_to_hhmmss(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    return f'{h:02d}:{m:02d}:{sec:02d}'


def guess_artist_from_filename(stem: str) -> str | None:
    """Extract artist name from common video filename patterns.

    Handles:
      DATE - VENUE - ARTIST-NNN  (e.g. '1.30 - FF - Seems-011')
      DATE - ARTIST-NNN          (e.g. '2.20 - Mountain Holler-002')
      DATE ARTIST-NNN            (e.g. '2.20 wyatt norton-005')
    Returns None when the pattern isn't recognizable.
    """
    name = re.sub(r'-\d{3}$', '', stem).strip()

    if ' - ' in name:
        parts = [p.strip() for p in name.split(' - ') if p.strip()]
        candidate = parts[-1] if parts else None
    elif ' -' in name:
        parts = [p.strip() for p in re.split(r'\s+-', name) if p.strip()]
        candidate = parts[-1] if parts else None
    else:
        candidate = re.sub(r'^\d+\.\d+\s+', '', name).strip() or None

    if not candidate or re.match(r'^\d', candidate):
        return None

    # Title-case only if all-lower or all-upper; preserve existing mixed case (e.g. CamelCase)
    if candidate == candidate.lower() or candidate == candidate.upper():
        return candidate.title()
    return candidate


def analyze_file(
    video_file: Path,
    workspace: Path,
    config: dict,
    gap_db: float,
    gap_sec: float,
    non_interactive: bool = False,
) -> dict | None:
    stem = video_file.stem
    audio_file = workspace / f'{stem}_audio.mp3'
    segments_file = workspace / f'{stem}_segments.json'

    if segments_file.exists():
        print(f'  Segments already exist for {video_file.name} — delete {segments_file.name} to re-analyze')
        with open(segments_file) as f:
            return json.load(f)

    print(f'\nAnalyzing: {video_file.name}')

    duration = get_duration(video_file, config)
    duration_min = duration / 60
    print(f'  Duration: {seconds_to_hhmmss(duration)} ({duration_min:.1f} min)')

    single_band = duration_min <= config.get('single_band_threshold_min', 75)

    if single_band:
        from config import load_known_bands, save_known_band

        detected = guess_artist_from_filename(video_file.stem)
        if detected:
            known = {b.lower(): b for b in load_known_bands()}
            band_name = known.get(detected.lower(), detected)
            print(f'  Band name: {band_name} (auto-detected)')
        elif non_interactive:
            band_name = 'Unknown Artist'
            print('  Band name: Unknown Artist (skipped — non-interactive mode)')
        else:
            raw = input('  Band name (Enter to skip): ').strip()
            band_name = raw if raw else 'Unknown Artist'

        save_known_band(band_name)

        segments = [{
            'id': 1,
            'start': 0.0,
            'end': duration,
            'start_hhmmss': '00:00:00',
            'end_hhmmss': seconds_to_hhmmss(duration),
            'label': band_name,
            'approved': True,
        }]
        result = {
            'source_file': str(video_file),
            'audio_file': None,
            'duration': duration,
            'auto_approved': True,
            'segments': segments,
        }
    else:
        if not audio_file.exists():
            extract_audio(video_file, audio_file, config)
        else:
            print(f'  Audio already extracted: {audio_file.name}')

        print('  Detecting volume level...')
        max_vol = get_max_volume(audio_file, config)
        threshold = max(max_vol - gap_db, -60.0)
        print(f'  Max volume: {max_vol:.1f} dB  |  Gap threshold: {threshold:.1f} dB  |  Min gap: {gap_sec}s')

        print('  Finding performance segments...')
        gaps = detect_quiet_regions(audio_file, threshold, gap_sec, config)
        min_segment_sec = config.get('min_segment_min', 5) * 60
        raw_segments = gaps_to_segments(gaps, duration, min_segment_sec)

        segments = []
        for i, seg in enumerate(raw_segments, 1):
            segments.append({
                'id': i,
                'start': seg['start'],
                'end': seg['end'],
                'start_hhmmss': seconds_to_hhmmss(seg['start']),
                'end_hhmmss': seconds_to_hhmmss(seg['end']),
                'label': f'Set {i}',
                'approved': False,
            })

        print(f'  Found {len(segments)} segment(s)')

        result = {
            'source_file': str(video_file),
            'audio_file': str(audio_file),
            'duration': duration,
            'auto_approved': False,
            'segments': segments,
        }

    with open(segments_file, 'w') as f:
        json.dump(result, f, indent=2)

    return result


def run_analyze(
    folder: Path,
    output_dir: Path | None = None,
    gap_db: float = 12.0,
    gap_sec: float = 30.0,
    single_band_threshold: float = 75.0,
    non_interactive: bool = False,
) -> None:
    folder = folder.resolve()
    workspace = get_workspace(folder, output_dir)
    config = load_config()
    config['gap_db'] = gap_db
    config['gap_sec'] = gap_sec
    config['single_band_threshold_min'] = single_band_threshold

    scan_file = workspace / 'scan_result.json'
    if scan_file.exists():
        with open(scan_file) as f:
            scan_result = json.load(f)
    else:
        print('No scan_result.json found in workspace. Running scan first...')
        from classify import scan_folder
        scan_result = scan_folder(folder, output_dir)

    processable = [
        f for f in scan_result['files']
        if f['category'] in PROCESSABLE and f['type'] == 'video'
    ]

    if not processable:
        print('No processable video files found in this folder.')
        return

    needs_review = []
    for file_info in processable:
        video_file = Path(file_info['path'])
        result = analyze_file(video_file, workspace, config, gap_db, gap_sec, non_interactive)
        if result and not result.get('auto_approved'):
            needs_review.append(file_info['name'])

    print(f'\n{"="*60}')
    if needs_review:
        print(f'{len(needs_review)} file(s) need human review:')
        for name in needs_review:
            print(f'  - {name}')
        print(f'\nRun: {sys.executable} process.py review "{folder}"')
    else:
        print('All files auto-approved (single-band videos).')
        print(f'\nRun: {sys.executable} process.py export "{folder}"')
