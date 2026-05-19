import json
import os
from pathlib import Path

import anthropic

from config import load_config, get_workspace


SYSTEM_PROMPT = '''You generate YouTube video metadata for live music clips from Suite E Studios in St. Pete, FL.
Keep titles concise and searchable. Descriptions should be warm, community-oriented, and mention the venue.
Tags should be relevant to the local St. Pete music scene, the band genre, and live music generally.
Return only valid JSON — no markdown, no explanation.'''


def generate_metadata(band_name: str, event_name: str, event_date: str, notes: str, client: anthropic.Anthropic) -> dict:
    prompt = f'''Generate YouTube metadata for this live music clip:

Band: {band_name}
Event: {event_name}
Date: {event_date}
Context: {notes or "Live performance at Suite E Studios, St. Pete, FL."}

Return a JSON object with:
- "title": YouTube video title (max 70 chars)
- "description": Video description (3-5 sentences, include date, venue, city)
- "tags": array of 10-15 relevant tags (strings)
- "thumbnail_time": suggested timestamp for thumbnail (HH:MM:SS format, a good mid-performance moment)'''

    config = load_config()
    message = client.messages.create(
        model=config.get('claude_model', 'claude-sonnet-4-6'),
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': prompt}],
    )

    try:
        return json.loads(message.content[0].text.strip())
    except (json.JSONDecodeError, IndexError):
        return {
            'title': f'{band_name} | Live at Suite E Studios',
            'description': f'{band_name} performing live at Suite E Studios in St. Pete, FL on {event_date}.',
            'tags': ['live music', 'st pete', 'suite e studios', 'st petersburg florida', band_name.lower()],
            'thumbnail_time': '00:05:00',
        }


def run_metadata(folder: Path, output_dir: Path | None = None) -> None:
    folder = folder.resolve()
    workspace = get_workspace(folder, output_dir)

    scan_file = workspace / 'scan_result.json'
    scan_data = {}
    if scan_file.exists():
        with open(scan_file) as f:
            scan_data = json.load(f)

    clips = sorted(workspace.glob('*.mp4'))
    if not clips:
        print('No exported clips found in workspace. Run export first.')
        return

    client = anthropic.Anthropic(api_key=''.join(os.environ.get('ANTHROPIC_API_KEY', '').split()))
    event_date = scan_data.get('event_date', 'unknown date')
    event_name = scan_data.get('event_name', folder.name)
    notes = scan_data.get('notes', '')

    print(f'\nGenerating metadata for {len(clips)} clip(s)...')

    for clip in clips:
        meta_file = clip.with_name(clip.stem + '_metadata.json')
        if meta_file.exists():
            print(f'  Skipping (exists): {meta_file.name}')
            continue

        # Extract band name from: "MM.DD.YYYY - Band Name - Full Set.mp4"
        parts = clip.stem.split(' - ')
        band_name = parts[1].strip() if len(parts) >= 2 else clip.stem

        print(f'  Generating: {clip.name}...')
        meta = generate_metadata(band_name, event_name, event_date, notes, client)
        meta['source_clip'] = clip.name

        with open(meta_file, 'w') as f:
            json.dump(meta, f, indent=2)

        print(f'    Title: {meta["title"]}')

    print('\nMetadata generation complete.')
    print('Phase 2 (future): upload clips + metadata to S3 → n8n → YouTube review queue.')
