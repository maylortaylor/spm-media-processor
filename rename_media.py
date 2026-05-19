import json
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

from config import get_workspace, load_config
from classify import parse_folder_date, format_date

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.dng', '.cr2', '.arw', '.raw'}
PHONE_CLIP_MAX_BYTES = 2 * 1024 ** 3

# (substring_in_make_or_model, label) — checked in order, first match wins
_DEVICE_PATTERNS = [
    ('apple',     'phone'),
    ('iphone',    'phone'),
    ('samsung',   'phone'),
    ('google',    'phone'),
    ('pixel',     'phone'),
    ('oneplus',   'phone'),
    ('android',   'phone'),
    ('huawei',    'phone'),
    ('xiaomi',    'phone'),
    ('motorola',  'phone'),
    ('canon',     'canon'),
    ('nikon',     'nikon'),
    ('sony',      'sony'),
    ('fuji',      'fuji'),
    ('olympus',   'olympus'),
    ('panasonic', 'panasonic'),
    ('gopro',     'gopro'),
    ('dji',       'dji'),
]

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def normalize_device_label(make: str | None, model: str | None) -> str:
    """Map EXIF make/model strings to a short normalized label."""
    combined = f'{make or ""} {model or ""}'.lower()
    for pattern, label in _DEVICE_PATTERNS:
        if pattern in combined:
            return label
    return 'camera'


def get_image_exif(filepath: Path) -> dict:
    """Extract date/make/model from image EXIF. Never raises — returns Nones on failure."""
    result: dict = {'date': None, 'make': None, 'model': None}

    # exifread handles all formats including RAW, HEIC, JPEG
    try:
        import exifread
        with open(filepath, 'rb') as f:
            tags = exifread.process_file(f, details=False)
        for dt_tag in ('EXIF DateTimeOriginal', 'EXIF DateTimeDigitized', 'Image DateTime'):
            if dt_tag in tags:
                try:
                    result['date'] = datetime.strptime(str(tags[dt_tag]), '%Y:%m:%d %H:%M:%S').date()
                    break
                except ValueError:
                    pass
        result['make'] = str(tags.get('Image Make') or '').strip() or None
        result['model'] = str(tags.get('Image Model') or '').strip() or None
        if result['date'] or result['make'] or result['model']:
            return result
    except Exception:
        pass

    # Fallback: Pillow for JPEG/PNG
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            exif_raw = getattr(img, '_getexif', lambda: None)()
            if exif_raw:
                # 36867=DateTimeOriginal, 36868=DateTimeDigitized, 306=DateTime
                for tag_id in (36867, 36868, 306):
                    if tag_id in exif_raw:
                        try:
                            result['date'] = datetime.strptime(
                                exif_raw[tag_id], '%Y:%m:%d %H:%M:%S',
                            ).date()
                            break
                        except (ValueError, TypeError):
                            pass
                result['make'] = exif_raw.get(271) or None   # 271 = Make
                result['model'] = exif_raw.get(272) or None  # 272 = Model
    except Exception:
        pass

    return result


def get_video_metadata(filepath: Path, config: dict) -> dict:
    """Extract date/make/model from video container tags via ffprobe. Never raises."""
    result: dict = {'date': None, 'make': None, 'model': None}
    ffprobe = config.get('ffprobe_path', 'ffprobe')
    try:
        proc = subprocess.run(
            [ffprobe, '-v', 'quiet', '-print_format', 'json', '-show_format', str(filepath)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            tags = json.loads(proc.stdout).get('format', {}).get('tags', {})
            ct = tags.get('creation_time') or tags.get('date')
            if ct:
                for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d'):
                    try:
                        result['date'] = datetime.strptime(ct[:26], fmt).date()
                        break
                    except ValueError:
                        pass
            make = tags.get('com.apple.quicktime.make') or tags.get('make')
            model = tags.get('com.apple.quicktime.model') or tags.get('model')
            result['make'] = make or None
            result['model'] = model or None
    except Exception:
        pass
    return result


def resolve_context_label(event_date: date | None, scan_result: dict | None) -> str:
    """Return context string: event name → band names → day-of-week."""
    if scan_result:
        name = (scan_result.get('event_name') or '').strip()
        folder_name = (scan_result.get('folder_name') or '').strip()
        # Only use event_name if it's more descriptive than the raw folder name
        if name and name != folder_name:
            return name
        bands = [b.strip() for b in scan_result.get('bands', []) if b.strip()]
        if bands:
            return ', '.join(bands)
    if event_date:
        return event_date.strftime('%A')  # e.g. "Friday"
    return 'Unknown'


def _load_scan_result(workspace: Path) -> dict | None:
    scan_file = workspace / 'scan_result.json'
    if scan_file.exists():
        try:
            with open(scan_file) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def build_rename_plan(
    folder: Path,
    output_dir: Path | None = None,
    config: dict | None = None,
) -> list[dict]:
    """Return list of {source, dest, reason} — no files are written."""
    if config is None:
        config = load_config()

    workspace = get_workspace(folder, output_dir)
    scan_result = _load_scan_result(workspace)

    event_date: date | None = None
    if scan_result and scan_result.get('event_date'):
        try:
            event_date = datetime.strptime(scan_result['event_date'], '%m.%d.%Y').date()
        except (ValueError, TypeError):
            pass
    if event_date is None:
        event_date = parse_folder_date(folder.name)

    context = resolve_context_label(event_date, scan_result)
    date_str = format_date(event_date) if event_date else 'unknown-date'

    video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.mts', '.m2ts', '.wmv', '.flv', '.webm'}

    image_files = []
    short_video_files = []
    for f in sorted(folder.rglob('*')):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            image_files.append(f)
        elif ext in video_exts and f.stat().st_size < PHONE_CLIP_MAX_BYTES:
            short_video_files.append(f)

    renamed_dir = workspace / 'renamed'
    counters: dict[tuple[str, str], int] = {}

    def _make_entry(filepath: Path, meta: dict, file_type: str) -> dict:
        label = normalize_device_label(meta['make'], meta['model'])
        key = (label, file_type)
        counters[key] = counters.get(key, 0) + 1
        n = counters[key]
        ext = filepath.suffix.lower()
        new_name = f'{date_str} - {context} - {label} {file_type} {n:02d}{ext}'
        return {
            'source': filepath,
            'dest': renamed_dir / new_name,
            'reason': f'make={meta["make"]!r} model={meta["model"]!r}',
            'is_image': file_type == 'image',
        }

    plan = []
    for f in image_files:
        plan.append(_make_entry(f, get_image_exif(f), 'image'))
    for f in short_video_files:
        plan.append(_make_entry(f, get_video_metadata(f, config), 'video'))

    return plan


def run_rename(
    folder: Path,
    output_dir: Path | None = None,
    dry_run: bool = False,
) -> None:
    folder = folder.resolve()
    config = load_config()
    plan = build_rename_plan(folder, output_dir, config)

    if not plan:
        print(f'  No images or short videos found in: {folder.name}')
        return

    prefix = '[DRY RUN] ' if dry_run else ''
    print(f'\n{prefix}Renaming {len(plan)} file(s) in: {folder.name}')

    workspace = get_workspace(folder, output_dir)
    renamed_dir = workspace / 'renamed'

    if not dry_run:
        renamed_dir.mkdir(parents=True, exist_ok=True)

    col = 52
    print(f'  {"Source":<{col}}  Destination')
    print('  ' + '-' * 110)
    for entry in plan:
        src = entry['source'].name
        dst = entry['dest'].name
        print(f'  {src:<{col}}  →  {dst}')
        if not dry_run:
            shutil.copy2(entry['source'], entry['dest'])

    if not dry_run:
        manifest = {
            'folder': str(folder),
            'total': len(plan),
            'files': [
                {'source': str(e['source']), 'dest': str(e['dest']), 'reason': e['reason']}
                for e in plan
            ],
        }
        manifest_file = workspace / 'rename_manifest.json'
        with open(manifest_file, 'w') as f:
            json.dump(manifest, f, indent=2)
        print(f'  Copied {len(plan)} file(s) → {renamed_dir}')
        print('  Originals untouched.')
