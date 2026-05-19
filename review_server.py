import json
import struct
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

from config import load_known_bands, save_known_band, get_workspace, load_config

PORT = 8765
NUM_PEAKS = 4000


def _build_peaks(audio_file: Path) -> dict:
    config = load_config()
    ffmpeg = config.get('ffmpeg_path', 'ffmpeg')
    result = subprocess.run(
        [ffmpeg, '-i', str(audio_file), '-ac', '1', '-ar', '100', '-f', 'f32le', '-'],
        capture_output=True, check=True,
    )
    count = len(result.stdout) // 4
    if count == 0:
        return {'peaks': [], 'duration': 0}
    samples = struct.unpack(f'{count}f', result.stdout)
    chunk = max(1, count // NUM_PEAKS)
    peaks = [max(abs(s) for s in samples[i:i + chunk]) for i in range(0, count, chunk)]
    max_val = max(peaks) if peaks else 1.0
    return {
        'peaks': [round(p / max_val, 4) for p in peaks],
        'duration': count / 100,
    }


class ReviewHandler(BaseHTTPRequestHandler):
    workspace: Path = None
    current_segments_file: Path = None
    server_instance: HTTPServer = None

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/':
            self._serve_html()
        elif path == '/data':
            self._serve_json(self.current_segments_file)
        elif path == '/bands':
            self._send_json({'bands': load_known_bands()})
        elif path == '/scan':
            self._serve_json(self.workspace / 'scan_result.json')
        elif path.startswith('/audio/'):
            filename = unquote(path[len('/audio/'):])
            self._serve_audio(self.workspace / filename)
        elif path.startswith('/peaks/'):
            filename = unquote(path[len('/peaks/'):])
            self._serve_peaks(self.workspace / filename)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/save':
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length))

            with open(self.current_segments_file, 'w') as f:
                json.dump(data, f, indent=2)

            for seg in data.get('segments', []):
                save_known_band(seg.get('label', ''))

            self._send_json({'status': 'saved'})
            threading.Thread(target=self.server_instance.shutdown, daemon=True).start()
        else:
            self.send_error(404)

    def _serve_html(self):
        html_file = Path(__file__).parent / 'templates' / 'review.html'
        content = html_file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self, filepath: Path):
        if not filepath or not filepath.exists():
            self._send_json({})
            return
        with open(filepath) as f:
            data = json.load(f)
        self._send_json(data)

    def _send_json(self, data: dict):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_peaks(self, audio_file: Path):
        peaks_file = audio_file.with_name(audio_file.stem + '_peaks.json')
        if not peaks_file.exists():
            print(f'  Generating peaks for {audio_file.name} (one-time, ~30s)...')
            data = _build_peaks(audio_file)
            with open(peaks_file, 'w') as f:
                json.dump(data, f)
            print('  Peaks saved.')
        with open(peaks_file) as f:
            self._send_json(json.load(f))

    def _serve_audio(self, filepath: Path):
        if not filepath.exists():
            self.send_error(404)
            return

        file_size = filepath.stat().st_size
        range_header = self.headers.get('Range')

        if range_header:
            ranges = range_header.replace('bytes=', '').split('-')
            start = int(ranges[0]) if ranges[0] else 0
            end = int(ranges[1]) if len(ranges) > 1 and ranges[1] else file_size - 1
            length = end - start + 1

            self.send_response(206)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.send_header('Content-Length', length)
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            with open(filepath, 'rb') as f:
                f.seek(start)
                self.wfile.write(f.read(length))
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Content-Length', file_size)
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            with open(filepath, 'rb') as f:
                self.wfile.write(f.read())


def review_segments_file(workspace: Path, segments_file: Path) -> None:
    ReviewHandler.workspace = workspace
    ReviewHandler.current_segments_file = segments_file

    server = HTTPServer(('127.0.0.1', PORT), ReviewHandler)
    ReviewHandler.server_instance = server

    url = f'http://127.0.0.1:{PORT}'
    print(f'\nOpening review UI: {url}')
    print(f'Reviewing: {segments_file.name}')
    print('Save & Close in the browser when done.')
    webbrowser.open(url)

    server.serve_forever()
    print('  Review saved.')


def run_review(folder: Path, output_dir: Path | None = None) -> None:
    folder = folder.resolve()
    workspace = get_workspace(folder, output_dir)

    segment_files = sorted(workspace.glob('*_segments.json'))
    if not segment_files:
        print('No segments found in workspace. Run analyze first.')
        return

    pending = []
    for sf in segment_files:
        with open(sf) as f:
            data = json.load(f)
        if data.get('auto_approved'):
            print(f'  Skipping (auto-approved): {sf.name}')
            continue
        all_approved = all(s.get('approved') for s in data.get('segments', []))
        if all_approved:
            print(f'  Already approved: {sf.name}')
            continue
        pending.append(sf)

    if not pending:
        print('All segments already approved. Run export.')
        return

    for sf in pending:
        review_segments_file(workspace, sf)

    print(f'\nAll reviews done. Run: {sys.executable} process.py export "{folder}"')
