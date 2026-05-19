import json
import re
import traceback
from datetime import datetime
from pathlib import Path

from classify import scan_folder
from analyze import run_analyze
from export import run_export
from config import get_workspace, load_config


def find_year_folders(root_dir: Path, year_filter: str | None = None) -> list[Path]:
    """Find subdirectories matching a 4-digit year pattern."""
    folders = sorted(
        d for d in root_dir.iterdir()
        if d.is_dir() and not d.name.startswith('.') and re.match(r'^\d{4}$', d.name)
    )
    if year_filter:
        folders = [f for f in folders if f.name == year_filter]
    return folders


def find_event_folders(year_dir: Path) -> list[Path]:
    """Find event subfolders inside a year directory."""
    return sorted(
        d for d in year_dir.iterdir()
        if d.is_dir() and not d.name.startswith('.')
    )


def run_deep_batch(
    root_dir: Path,
    output_dir: Path | None = None,
    dry_run: bool = False,
    year_filter: str | None = None,
    no_rename: bool = False,
    gap_db: float = 12.0,
    gap_sec: float = 30.0,
    single_band_threshold: float = 75.0,
) -> None:
    root_dir = root_dir.resolve()

    year_folders = find_year_folders(root_dir, year_filter)
    if not year_folders:
        msg = f'No year folder matching {year_filter!r}' if year_filter else 'No year folders (e.g. 2024/, 2025/)'
        print(f'{msg} found in: {root_dir}')
        return

    # Build the full work list up front so we can show totals
    work: list[tuple[str, Path]] = []
    for year_dir in year_folders:
        for folder in find_event_folders(year_dir):
            work.append((year_dir.name, folder))

    if not work:
        print('No event folders found.')
        return

    year_counts: dict[str, int] = {}
    for year_name, _ in work:
        year_counts[year_name] = year_counts.get(year_name, 0) + 1

    print(f'\nDeep batch: {len(work)} event folder(s) across {len(year_folders)} year(s)')
    for y, count in sorted(year_counts.items()):
        print(f'  {y}: {count} folder(s)')

    if dry_run:
        print('\n[DRY RUN] Folders that would be processed:')
        for year_name, folder in work:
            print(f'  {year_name}/{folder.name}')
        print(f'\n[DRY RUN] {len(work)} folder(s) total. Run without --dry-run to process.')
        return

    manifest: dict = {
        'root': str(root_dir),
        'run_timestamp': datetime.now().isoformat(timespec='seconds'),
        'years': {},
        'summary': {'processed': 0, 'skipped': 0, 'errored': 0},
    }

    year_progress: dict[str, int] = {y: 0 for y in year_counts}

    for year_name, folder in work:
        year_progress[year_name] += 1
        idx = year_progress[year_name]
        total_in_year = year_counts[year_name]

        print(f'\n{"="*70}')
        print(f'[{year_name}: {idx}/{total_in_year}] {folder.name}')
        print('=' * 70)

        if year_name not in manifest['years']:
            manifest['years'][year_name] = {
                'total': total_in_year,
                'processed': 0,
                'skipped': 0,
                'errored': 0,
                'folders': [],
            }

        try:
            if not no_rename:
                from rename_media import run_rename
                run_rename(folder, output_dir=output_dir, dry_run=False)

            scan_folder(folder, output_dir)

            run_analyze(
                folder,
                output_dir=output_dir,
                gap_db=gap_db,
                gap_sec=gap_sec,
                single_band_threshold=single_band_threshold,
                non_interactive=True,
            )

            run_export(folder, output_dir=output_dir)

            manifest['years'][year_name]['processed'] += 1
            manifest['years'][year_name]['folders'].append(
                {'name': folder.name, 'status': 'processed'},
            )
            manifest['summary']['processed'] += 1

        except Exception as e:
            error_msg = f'{type(e).__name__}: {e}'
            print(f'\n  ERROR processing {folder.name}: {error_msg}')
            traceback.print_exc()
            manifest['years'][year_name]['errored'] += 1
            manifest['years'][year_name]['folders'].append(
                {'name': folder.name, 'status': 'error', 'error': error_msg},
            )
            manifest['summary']['errored'] += 1

    # Write manifest next to the _processed dir
    cfg = load_config()
    if output_dir:
        manifest_base = Path(output_dir)
    elif cfg.get('default_output_dir'):
        manifest_base = Path(cfg['default_output_dir'])
    else:
        manifest_base = root_dir / '_processed'

    manifest_base.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    manifest_file = manifest_base / f'deep_batch_manifest_{ts}.json'
    with open(manifest_file, 'w') as f:
        json.dump(manifest, f, indent=2)

    s = manifest['summary']
    print(f'\n{"="*70}')
    print(f'Deep batch complete.')
    print(f'  Processed: {s["processed"]}  Skipped: {s["skipped"]}  Errored: {s["errored"]}')
    print(f'  Manifest:  {manifest_file}')
    if s['errored']:
        print(f'\n  {s["errored"]} folder(s) had errors — see manifest for details.')
        print(f'  Folders needing manual review still have unapproved segments.')
        print(f'  Run: python process.py review "<folder>" for each one, then re-export.')
