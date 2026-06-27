# ============================================================
# relay.py — Railway Download Relay
# ============================================================
from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import tempfile
import threading
import uuid
import time
import shutil
import json
import logging
import subprocess
from functools import wraps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
jobs = {}

RELAY_TOKEN = os.environ.get('RELAY_TOKEN', '')
COOKIE_HEADER = None

def _init_cookies():
    global COOKIE_HEADER

    cookies_txt = os.environ.get('YOUTUBE_COOKIES_TXT', '').strip()
    if cookies_txt:
        cookie_pairs = []
        for line in cookies_txt.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) >= 7:
                name = parts[5]
                value = parts[6]
                if name:
                    cookie_pairs.append(f'{name}={value}')
        if cookie_pairs:
            COOKIE_HEADER = '; '.join(cookie_pairs)
            logger.info(f'[cookies] Cookie-заголовок готов ({len(cookie_pairs)} куки)')
        return

    cookies_json = os.environ.get('YOUTUBE_COOKIES_JSON', '').strip()
    if cookies_json:
        try:
            data = json.loads(cookies_json)
            cookies = data.get('youtube', data) if isinstance(data, dict) else data
            if isinstance(cookies, list) and cookies:
                cookie_pairs = []
                for c in cookies:
                    name = c.get('name', '')
                    value = c.get('value', '')
                    if name:
                        cookie_pairs.append(f'{name}={value}')
                if cookie_pairs:
                    COOKIE_HEADER = '; '.join(cookie_pairs)
                    logger.info(f'[cookies] Cookie-заголовок готов из JSON ({len(cookie_pairs)} куки)')
        except Exception as e:
            logger.error(f'[cookies] Ошибка парсинга: {e}')

_init_cookies()

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if RELAY_TOKEN:
            token = (request.headers.get('X-Relay-Token') or
                     request.args.get('token') or '')
            if token != RELAY_TOKEN:
                return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper

def _cleanup_loop():
    while True:
        time.sleep(300)
        cutoff = time.time() - 3600
        for jid in list(jobs):
            j = jobs.get(jid)
            if not j or j.get('created_at', 0) >= cutoff:
                continue
            tmp = j.get('tmp_dir')
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            jobs.pop(jid, None)

threading.Thread(target=_cleanup_loop, daemon=True).start()

def _spotify_worker(job_id, url, fmt, tmp_dir):
    """Скачивание Spotify через spotdl с обложкой и метаданными."""
    audio_fmt = fmt if fmt in ('mp3', 'm4a', 'opus', 'ogg', 'flac') else 'mp3'

    try:
        jobs[job_id]['status'] = 'downloading'
        jobs[job_id]['progress'] = 5

        cmd = [
            'spotdl',
            url,
            '--output', tmp_dir,
            '--format', audio_fmt,
            '--bitrate', '192k',
            '--threads', '1',
        ]
        logger.info(f'[spotify] job {job_id}: {" ".join(cmd)}')

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        progress = 10
        for line in proc.stdout:
            line = line.strip()
            if line:
                logger.info(f'[spotdl] {line}')
            if 'Downloading' in line or 'Found' in line:
                progress = min(progress + 10, 80)
                jobs[job_id]['progress'] = progress
            elif 'Converting' in line or 'Embedding' in line:
                jobs[job_id]['progress'] = 90
                jobs[job_id]['status'] = 'processing'

        proc.wait()
        jobs[job_id]['progress'] = 95

        MEDIA = {'.mp3', '.m4a', '.opus', '.ogg', '.flac', '.wav'}
        found = next(
            (os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
             if os.path.splitext(f)[1].lower() in MEDIA),
            None
        )

        if not found:
            raise RuntimeError('spotdl не нашёл файл после скачивания')

        jobs[job_id].update({
            'file': found,
            'filename': os.path.basename(found),
            'status': 'done',
            'progress': 100,
        })
        logger.info(f'[spotify] job {job_id} done: {found}')

    except Exception as exc:
        err = str(exc)[:500]
        logger.error(f'[spotify] job {job_id} ОШИБКА: {err}')
        jobs[job_id].update({'status': 'error', 'error': err})
        shutil.rmtree(tmp_dir, ignore_errors=True)
        jobs[job_id]['tmp_dir'] = None

def _download_worker(job_id, url, dl_type, quality, fmt, tmp_dir):

    def hook(d):
        if d['status'] == 'downloading':
            raw = d.get('_percent_str', '0').strip().replace('%', '').replace('~', '')
            try:
                jobs[job_id]['progress'] = float(raw)
                jobs[job_id]['status'] = 'downloading'
            except Exception:
                pass
        elif d['status'] == 'finished':
            jobs[job_id]['status'] = 'processing'
            jobs[job_id]['progress'] = 99

    http_headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
    }
    if COOKIE_HEADER:
        http_headers['Cookie'] = COOKIE_HEADER
        logger.info('[worker] Куки переданы через заголовок')

    opts = {
        'outtmpl': os.path.join(tmp_dir, '%(title).100s.%(ext)s'),
        'progress_hooks': [hook],
        'noplaylist': True,
        'nocheckcertificate': True,
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 5,
        'socket_timeout': 30,
        'http_headers': http_headers,
        'extractor_args': {
            'youtube': {
                'player_client': ['android_vr', 'android'],
            }
        },
        'quiet': False,
        'no_warnings': False,
    }

    if dl_type == 'audio':
        af = fmt if fmt in ('mp3', 'm4a', 'opus', 'ogg', 'wav') else 'mp3'
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': af, 'preferredquality': '192'},
            {'key': 'FFmpegMetadata', 'add_metadata': True},
        ]
    else:
        opts['format'] = quality
        opts['merge_output_format'] = fmt if fmt in ('mp4', 'mkv', 'webm') else 'mp4'

    try:
        jobs[job_id]['status'] = 'downloading'
        logger.info(f'[worker] job {job_id}: {url}')

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        MEDIA = {'.mp4', '.mkv', '.webm', '.mp3', '.m4a',
                 '.opus', '.ogg', '.flac', '.wav'}
        found = next(
            (os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
             if os.path.splitext(f)[1].lower() in MEDIA),
            None
        )

        if not found:
            raise RuntimeError('Медиафайл не найден после скачивания')

        jobs[job_id].update({
            'file': found,
            'filename': os.path.basename(found),
            'status': 'done',
            'progress': 100,
        })
        logger.info(f'[worker] job {job_id} done: {found}')

    except Exception as exc:
        err = str(exc)[:500]
        logger.error(f'[worker] job {job_id} ОШИБКА: {err}')
        jobs[job_id].update({'status': 'error', 'error': err})
        shutil.rmtree(tmp_dir, ignore_errors=True)
        jobs[job_id]['tmp_dir'] = None

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'active_jobs': len(jobs), 'cookies': bool(COOKIE_HEADER)})

@app.route('/start', methods=['POST'])
@require_auth
def start():
    data = request.json or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL обязателен'}), 400

    job_id = str(uuid.uuid4())
    tmp_dir = tempfile.mkdtemp()

    jobs[job_id] = {
        'status': 'pending', 'progress': 0,
        'file': None, 'filename': None, 'error': None,
        'tmp_dir': tmp_dir, 'created_at': time.time(),
    }

    # Spotify — отдельный воркер через spotdl
    if 'open.spotify.com' in url.lower():
        fmt = data.get('format', 'mp3')
        threading.Thread(
            target=_spotify_worker,
            args=(job_id, url, fmt, tmp_dir),
            daemon=True,
        ).start()
    else:
        threading.Thread(
            target=_download_worker,
            args=(job_id, url,
                  data.get('type', 'video'),
                  data.get('quality', 'bestvideo+bestaudio/best'),
                  data.get('format', 'mp4'),
                  tmp_dir),
            daemon=True,
        ).start()

    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
@require_auth
def job_status(job_id):
    j = jobs.get(job_id)
    if not j:
        return jsonify({'error': 'Задача не найдена'}), 404
    return jsonify({
        'status': j['status'],
        'progress': int(j.get('progress', 0)),
        'error': j.get('error'),
        'filename': j.get('filename'),
    })

@app.route('/file/<job_id>')
@require_auth
def get_file(job_id):
    j = jobs.get(job_id)
    if not j:
        return jsonify({'error': 'Задача не найдена'}), 404
    if j['status'] != 'done':
        return jsonify({'error': f'Файл не готов: {j["status"]}'}), 425
    fp = j.get('file')
    if not fp or not os.path.exists(fp):
        return jsonify({'error': 'Файл не найден на диске'}), 404

    fn = j.get('filename') or os.path.basename(fp)
    return send_file(fp, as_attachment=True, download_name=fn)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, threaded=True)
