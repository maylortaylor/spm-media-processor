#!/usr/bin/env python3
'''
SPM Media Processor CLI.

Usage:
  python process.py configure                                     First-time machine setup
  python process.py scan       <folder> [--output-dir D]          Classify files, look up calendar
  python process.py analyze    <folder> [--output-dir D] [opts]   Detect band segments from audio
  python process.py review     <folder> [--output-dir D]          Browser waveform review (long videos)
  python process.py export     <folder> [--output-dir D]          Cut approved clips
  python process.py metadata   <folder> [--output-dir D]          Generate YouTube metadata
  python process.py batch      <year-folder> [--output-dir D]     Process all event folders in a year dir
  python process.py rename     <folder> [--output-dir D] [--dry-run]
                                                                   Copy-rename images and short clips
  python process.py deep-batch <root-dir> [--output-dir D] [opts] Process all years under a root dir

All commands read default_output_dir from config.json when --output-dir is not given.
All intermediate files and output clips go to output_dir/event_folder_name/ — originals untouched.

Options for analyze / batch / deep-batch:
  --gap-db FLOAT              Volume drop in dB that signals a set change (default: 12)
  --gap-sec FLOAT             Minimum gap duration in seconds (default: 30)
  --single-band-threshold INT Max minutes for auto-approval as single band (default: 75)

Options for deep-batch:
  --dry-run                   Preview folders without processing
  --year YYYY                 Only process this year (e.g. --year 2024)
  --no-rename                 Skip image/short-video renaming step
'''
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()


def _add_output_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--output-dir', type=Path, default=None,
                        help='Where to write all outputs (overrides config.json default_output_dir)')


def main() -> None:
    parser = argparse.ArgumentParser(
        description='SPM Media Processor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('configure', help='Set up this machine (FFmpeg, Google Calendar, defaults)')

    scan_p = sub.add_parser('scan', help='Classify files in a folder')
    scan_p.add_argument('folder', type=Path)
    _add_output_dir(scan_p)

    analyze_p = sub.add_parser('analyze', help='Detect band segments from audio volume')
    analyze_p.add_argument('folder', type=Path)
    _add_output_dir(analyze_p)
    analyze_p.add_argument('--gap-db', type=float, default=None)
    analyze_p.add_argument('--gap-sec', type=float, default=None)
    analyze_p.add_argument('--single-band-threshold', type=float, default=None)

    review_p = sub.add_parser('review', help='Open browser review UI for long videos')
    review_p.add_argument('folder', type=Path)
    _add_output_dir(review_p)

    export_p = sub.add_parser('export', help='Cut approved clips to output folder')
    export_p.add_argument('folder', type=Path)
    _add_output_dir(export_p)

    meta_p = sub.add_parser('metadata', help='Generate YouTube metadata for exported clips')
    meta_p.add_argument('folder', type=Path)
    _add_output_dir(meta_p)

    batch_p = sub.add_parser('batch', help='Process all event folders inside a year/root folder')
    batch_p.add_argument('year_folder', type=Path,
                         help='Folder containing event subfolders (e.g. EVENTS/2026/)')
    _add_output_dir(batch_p)
    batch_p.add_argument('--gap-db', type=float, default=None)
    batch_p.add_argument('--gap-sec', type=float, default=None)
    batch_p.add_argument('--single-band-threshold', type=float, default=None)

    rename_p = sub.add_parser('rename', help='Copy-rename images and short clips by EXIF source/date')
    rename_p.add_argument('folder', type=Path)
    _add_output_dir(rename_p)
    rename_p.add_argument('--dry-run', action='store_true',
                          help='Preview renames without copying files')

    deep_p = sub.add_parser('deep-batch', help='Process all years/events under a root directory')
    deep_p.add_argument('root_dir', type=Path,
                        help='Root containing year subfolders (e.g. EVENTS/ with 2024/, 2025/, ...)')
    _add_output_dir(deep_p)
    deep_p.add_argument('--dry-run', action='store_true',
                        help='Preview folders without processing')
    deep_p.add_argument('--year', type=str, default=None, metavar='YYYY',
                        help='Only process this year (e.g. --year 2024)')
    deep_p.add_argument('--no-rename', action='store_true',
                        help='Skip image/short-video renaming step')
    deep_p.add_argument('--gap-db', type=float, default=None)
    deep_p.add_argument('--gap-sec', type=float, default=None)
    deep_p.add_argument('--single-band-threshold', type=float, default=None)

    args = parser.parse_args()

    if args.command == 'configure':
        from configure import run_configure
        run_configure()

    elif args.command == 'scan':
        from classify import run_scan
        run_scan(args.folder, output_dir=args.output_dir)

    elif args.command == 'analyze':
        from config import load_config
        cfg = load_config()
        gap_db = args.gap_db if args.gap_db is not None else cfg['gap_db']
        gap_sec = args.gap_sec if args.gap_sec is not None else cfg['gap_sec']
        threshold = args.single_band_threshold if args.single_band_threshold is not None else cfg['single_band_threshold_min']
        from analyze import run_analyze
        run_analyze(args.folder, output_dir=args.output_dir, gap_db=gap_db, gap_sec=gap_sec, single_band_threshold=threshold)

    elif args.command == 'review':
        from review_server import run_review
        run_review(args.folder, output_dir=args.output_dir)

    elif args.command == 'export':
        from export import run_export
        run_export(args.folder, output_dir=args.output_dir)

    elif args.command == 'metadata':
        from metadata import run_metadata
        run_metadata(args.folder, output_dir=args.output_dir)

    elif args.command == 'batch':
        from batch import run_batch
        from config import load_config
        cfg = load_config()
        gap_db = args.gap_db if args.gap_db is not None else cfg['gap_db']
        gap_sec = args.gap_sec if args.gap_sec is not None else cfg['gap_sec']
        threshold = args.single_band_threshold if args.single_band_threshold is not None else cfg['single_band_threshold_min']
        run_batch(
            args.year_folder,
            output_dir=args.output_dir,
            gap_db=gap_db,
            gap_sec=gap_sec,
            single_band_threshold=threshold,
        )

    elif args.command == 'rename':
        from rename_media import run_rename
        run_rename(args.folder, output_dir=args.output_dir, dry_run=args.dry_run)

    elif args.command == 'deep-batch':
        from deep_batch import run_deep_batch
        from config import load_config
        cfg = load_config()
        gap_db = args.gap_db if args.gap_db is not None else cfg['gap_db']
        gap_sec = args.gap_sec if args.gap_sec is not None else cfg['gap_sec']
        threshold = args.single_band_threshold if args.single_band_threshold is not None else cfg['single_band_threshold_min']
        run_deep_batch(
            args.root_dir,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
            year_filter=args.year,
            no_rename=args.no_rename,
            gap_db=gap_db,
            gap_sec=gap_sec,
            single_band_threshold=threshold,
        )


if __name__ == '__main__':
    main()
