from pathlib import Path

from classify import scan_folder
from analyze import run_analyze
from review_server import run_review
from export import run_export
from config import get_workspace


def run_batch(
    year_folder: Path,
    output_dir: Path | None = None,
    gap_db: float = 12.0,
    gap_sec: float = 30.0,
    single_band_threshold: float = 75.0,
) -> None:
    year_folder = year_folder.resolve()

    event_folders = sorted(d for d in year_folder.iterdir() if d.is_dir() and not d.name.startswith("."))

    if not event_folders:
        print(f"No subfolders found in: {year_folder}")
        return

    print(f"\nBatch processing {len(event_folders)} folder(s) in: {year_folder}")
    for i, folder in enumerate(event_folders, 1):
        print(f"\n[{i}/{len(event_folders)}] {folder.name}")

    input("\nProceed? (Enter to continue, Ctrl+C to abort): ")

    for i, folder in enumerate(event_folders, 1):
        print(f"\n{'=' * 70}")
        print(f"[{i}/{len(event_folders)}] {folder.name}")
        print("=" * 70)

        # Scan
        scan_folder(folder, output_dir)

        # Analyze
        run_analyze(
            folder,
            output_dir=output_dir,
            gap_db=gap_db,
            gap_sec=gap_sec,
            single_band_threshold=single_band_threshold,
        )

        # Review if needed
        workspace = get_workspace(folder, output_dir)
        segment_files = list(workspace.glob("*_segments.json"))
        needs_review = []
        for sf in segment_files:
            import json

            with open(sf) as f:
                data = json.load(f)
            if not data.get("auto_approved"):
                all_approved = all(s.get("approved") for s in data.get("segments", []))
                if not all_approved:
                    needs_review.append(sf.name)

        if needs_review:
            print(f"\n  {len(needs_review)} file(s) need review before export.")
            choice = input("  Open review UI now? (y/n, Enter = yes): ").strip().lower()
            if choice != "n":
                run_review(folder, output_dir=output_dir)

        # Export
        run_export(folder, output_dir=output_dir)

    print(f"\n{'=' * 70}")
    print(f"Batch complete. {len(event_folders)} folder(s) processed.")
    if output_dir:
        print(f"Output: {output_dir}")
