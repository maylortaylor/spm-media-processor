import shutil
import sys
from pathlib import Path
from config import load_config, save_config, PROJECT_ROOT


def run_configure() -> None:
    print('=== Music Processor Setup ===\n')
    config = load_config()

    # FFmpeg
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        print(f'FFmpeg found: {ffmpeg}')
        config['ffmpeg_path'] = 'ffmpeg'
    else:
        path = input('FFmpeg not in PATH. Enter full path to ffmpeg binary: ').strip()
        if not path:
            print('ERROR: FFmpeg is required. Install it and re-run configure.')
            sys.exit(1)
        config['ffmpeg_path'] = path

    ffprobe = shutil.which('ffprobe')
    if ffprobe:
        config['ffprobe_path'] = 'ffprobe'
    else:
        path = input('Enter full path to ffprobe binary: ').strip()
        if not path:
            print('ERROR: ffprobe is required (comes with FFmpeg).')
            sys.exit(1)
        config['ffprobe_path'] = path

    # Google Calendar
    print('\n--- Google Calendar (optional, press Enter to skip) ---')
    cal_id = input('Google Calendar ID: ').strip()
    if cal_id:
        config['google_calendar_id'] = cal_id
        creds_file = PROJECT_ROOT / 'credentials.json'
        if not creds_file.exists():
            print('\n  To enable calendar integration:')
            print('  1. Go to https://console.cloud.google.com/')
            print('  2. Create a project, enable Google Calendar API')
            print('  3. Create OAuth2 credentials (Desktop application)')
            print(f'  4. Download as credentials.json and place in: {PROJECT_ROOT}')
            print('  5. Re-run configure to authenticate')
        else:
            print('  credentials.json found. Testing authentication...')
            try:
                from calendar_client import authenticate
                authenticate()
                print('  Google Calendar: OK')
            except Exception as e:
                print(f'  Auth failed: {e}')

    # Default output dir
    print('\n--- Output Directory ---')
    print('Where should processed clips go? (Enter to use _processed/ inside each event folder)')
    out_dir = input('Default output dir: ').strip()
    if out_dir:
        config['default_output_dir'] = out_dir

    # Thresholds
    print('\n--- Detection Thresholds (press Enter for defaults) ---')
    threshold = input(f'Single-band threshold in minutes [{config["single_band_threshold_min"]}]: ').strip()
    if threshold:
        config['single_band_threshold_min'] = float(threshold)

    gap_db = input(f'Volume drop threshold in dB [{config["gap_db"]}]: ').strip()
    if gap_db:
        config['gap_db'] = float(gap_db)

    gap_sec = input(f'Minimum gap duration in seconds [{config["gap_sec"]}]: ').strip()
    if gap_sec:
        config['gap_sec'] = float(gap_sec)

    save_config(config)
    print(f'\nConfig saved to {PROJECT_ROOT / "config.json"}')
    print(f'Setup complete. Run "{sys.executable} process.py scan <folder>" to get started.')
