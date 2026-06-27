# relay.py — Railway Download Relay
from flask import Flask, request, jsonify, send_file
import yt_dlp, os, tempfile, threading, uuid, time, shutil, json, logging, subprocess
from functools import wraps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)
jobs = {}

RELAY_TOKEN = os.environ.get('RELAY_TOKEN', '')
COOKIE_FILE = None

def _init_cookies():
    global COOKIE_FILE
    txt = os.environ.get('YOUTUBE_COOKIES_TXT', '').strip()
    if txt:
        COOKIE_FILE = '/tmp/yt_cookies.txt'
        with open(COOKIE_FILE, 'w') as f: f.write(txt)
        count = sum(1 for l in txt.splitlines() if l.strip() and not l.startswith('#'))
        logger.info(f'[cookies] Записано {count} строк в cookiefile')
        return
    raw = os.environ.get('YOUTUBE_COOKIES_JSON', '').strip()
    if raw:
        try:
            data = json.loads(raw)
            lst = data.get('youtube', data) if isinstance(data, dict) else data
            if isinstance(lst, list):
                COOKIE_FILE = '/tmp/yt_cookies.txt'
                with open(COOKIE_FILE, 'w') as f:
                    f.write('# Netscape HTTP Cookie File\n')
                    for c in lst:
                        dom = c.get('domain', '.youtube.com')
                        if not dom.startswith('.'): dom = '.' + dom
                        sec = 'TRUE' if c.get('secure') else 'FALSE'
                        exp = str(int(c.get('expirationDate', 2147483647)))
                        n, v = c.get('name',''), c.get('value','')
                        f.write(f'{dom}\tTRUE\t{c.get("path","/")}\t{sec}\t{exp}\t{n}\t{v}\n')
                logger.info(f'[cookies] JSON → {len(lst)} куки записано')
        except Exception as e:
            logger.error(f'[cookies] {e}')

_init_cookies()

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if RELAY_TOKEN:
            tok = request.headers.get('X-Relay-Token') or request.args.get('token','')
            if tok != RELAY_TOKEN:
                return jsonify({'error':'Unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper

def _cleanup():
    while True:
        time.sleep(300)
        cut = time.time() - 3600
        for jid in list(jobs):
            j = jobs.get(jid)
            if j and j.get('created_at',0) < cut:
                shutil.rmtree(j.get('tmp_dir',''), ignore_errors=True)
                jobs.pop(jid, None)

threading.Thread(target=_cleanup, daemon=True).start()

BASE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

STRATEGIES = [
    (['web'],         True,  'web+cookie'),
    (['android_vr'],  False, 'android_vr'),
    (['android'],     False, 'android'),
    (['web'],         False, 'web_nocookie'),
    (['mweb'],        True,  'mweb+cookie'),
    (['tv_embedded'], False, 'tv_embedded'),
]

def _make_opts(strategy, extra=None):
    clients, use_cookie, name = strategy
    opts = {
        'noplaylist': True,
        'nocheckcertificate': True,
        'retries': 5,
        'fragment_retries': 5,
        'socket_timeout': 30,
        'http_headers': BASE_HEADERS.copy(),
        'extractor_args': {'youtube': {'player_client': clients}},
        'quiet': True,
        'no_warnings': True,
    }
    if use_cookie and COOKIE_FILE and os.path.exists(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
    if extra: opts.update(extra)
    return opts, name

# ── /info — получить аудиодорожки и форматы ──────────────────
@app.route('/info', methods=['POST'])
@require_auth
def video_info():
    """
    Возвращает список аудиодорожек видео.
    Используется для выбора языка аудио перед скачиванием.
    """
    data = request.json or {}
    url  = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL обязателен'}), 400

    last_err = 'Не удалось получить информацию'
    for strategy in STRATEGIES:
        opts, name = _make_opts(strategy, {'quiet': True, 'no_warnings': True})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                continue

            fmts = info.get('formats', [])

            # Собираем уникальные аудиодорожки
            # У видео может быть несколько языков (dubbed)
            audio_tracks = {}
            for f in fmts:
                if f.get('vcodec','none') != 'none':
                    continue  # только аудио форматы
                if not f.get('url'):
                    continue
                lang = f.get('language') or f.get('format_note') or 'default'
                abr  = f.get('abr') or 0
                acodec = f.get('acodec','unknown')
                fid  = f.get('format_id','')

                # Оставляем лучший bitrate для каждого языка
                if lang not in audio_tracks or abr > audio_tracks[lang]['abr']:
                    audio_tracks[lang] = {
                        'language':    lang,
                        'format_id':   fid,
                        'abr':         round(abr) if abr else 0,
                        'acodec':      acodec,
                        'label':       _lang_label(lang, abr),
                    }

            tracks = sorted(audio_tracks.values(), key=lambda x: x['abr'], reverse=True)

            logger.info(f'[info] {url} → {len(tracks)} аудиодорожек ({name})')
            return jsonify({
                'title':        info.get('title',''),
                'uploader':     info.get('uploader',''),
                'duration':     info.get('duration'),
                'audio_tracks': tracks,
                'has_multiple': len(tracks) > 1,
            })

        except Exception as e:
            last_err = str(e)[:300]
            logger.warning(f'[info] {name} провалился: {last_err}')
            continue

    return jsonify({'error': last_err}), 500

def _lang_label(lang: str, abr) -> str:
    """Человекочитаемое название языка."""
    LANG_NAMES = {
        'ru': 'Русский', 'en': 'English', 'de': 'Deutsch',
        'fr': 'Français', 'es': 'Español', 'it': 'Italiano',
        'pt': 'Português', 'ja': '日本語', 'ko': '한국어',
        'zh': '中文', 'ar': 'العربية', 'hi': 'हिन्दी',
        'tr': 'Türkçe', 'pl': 'Polski', 'nl': 'Nederlands',
        'uk': 'Українська', 'cs': 'Čeština', 'sv': 'Svenska',
        'default': 'По умолчанию', 'und': 'Оригинал',
    }
    base = lang.split('-')[0].lower() if lang else 'default'
    name = LANG_NAMES.get(base) or LANG_NAMES.get(lang) or lang.upper()
    bitrate = f' ~{round(abr)}kbps' if abr else ''
    return f'{name}{bitrate}'

# ── Перебор стратегий для скачивания ─────────────────────────
def _try_download(job_id, url, dl_type, quality, fmt, tmp_dir, audio_lang=None):
    def hook(d):
        if d['status'] == 'downloading':
            raw = d.get('_percent_str','0').strip().replace('%','').replace('~','')
            try:
                jobs[job_id]['progress'] = float(raw)
                jobs[job_id]['status']   = 'downloading'
            except: pass
        elif d['status'] == 'finished':
            jobs[job_id].update({'status':'processing','progress':99})

    last_err = 'Неизвестная ошибка'

    for strategy in STRATEGIES:
        opts, name = _make_opts(strategy, {
            'outtmpl': os.path.join(tmp_dir, '%(title).100s.%(ext)s'),
            'progress_hooks': [hook],
            'quiet': False,
            'no_warnings': False,
        })

        # Формат с учётом выбранной аудиодорожки
        if dl_type == 'audio':
            af = fmt if fmt in ('mp3','m4a','opus','ogg','wav') else 'mp3'
            if audio_lang and audio_lang != 'default':
                # Конкретный язык: берём аудио с нужным language тегом
                opts['format'] = f'bestaudio[language={audio_lang}]/bestaudio/best'
            else:
                opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [
                {'key':'FFmpegExtractAudio','preferredcodec':af,'preferredquality':'192'},
                {'key':'FFmpegMetadata','add_metadata':True},
            ]
        else:
            if audio_lang and audio_lang != 'default':
                # Видео с конкретной аудиодорожкой
                opts['format'] = (
                    f'bestvideo+bestaudio[language={audio_lang}]/'
                    f'{quality}+bestaudio[language={audio_lang}]/'
                    f'{quality}'
                )
            else:
                opts['format'] = quality
            opts['merge_output_format'] = fmt if fmt in ('mp4','mkv','webm') else 'mp4'

        logger.info(f'[worker] Стратегия {name} (lang={audio_lang}): {url}')
        try:
            for f in os.listdir(tmp_dir):
                try: os.remove(os.path.join(tmp_dir, f))
                except: pass

            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            MEDIA = {'.mp4','.mkv','.webm','.mp3','.m4a','.opus','.ogg','.flac','.wav'}
            found = next(
                (os.path.join(tmp_dir,f) for f in os.listdir(tmp_dir)
                 if os.path.splitext(f)[1].lower() in MEDIA), None)

            if found:
                logger.info(f'[worker] Успех ({name}): {found}')
                return found, None
            else:
                last_err = f'Файл не найден ({name})'
        except Exception as e:
            last_err = str(e)[:300]
            logger.warning(f'[worker] {name} провалился: {last_err}')

    return None, last_err

def _download_worker(job_id, url, dl_type, quality, fmt, tmp_dir, audio_lang=None):
    try:
        jobs[job_id]['status'] = 'downloading'
        logger.info(f'[worker] job {job_id}: {url} (audio_lang={audio_lang})')

        found, err = _try_download(job_id, url, dl_type, quality, fmt, tmp_dir, audio_lang)

        if found:
            jobs[job_id].update({
                'file': found, 'filename': os.path.basename(found),
                'status': 'done', 'progress': 100,
            })
        else:
            jobs[job_id].update({'status':'error','error':err or 'Все стратегии провалились'})
            shutil.rmtree(tmp_dir, ignore_errors=True)
            jobs[job_id]['tmp_dir'] = None
    except Exception as exc:
        err = str(exc)[:500]
        logger.error(f'[worker] {err}')
        jobs[job_id].update({'status':'error','error':err})
        shutil.rmtree(tmp_dir, ignore_errors=True)
        jobs[job_id]['tmp_dir'] = None

def _spotify_worker(job_id, url, fmt, tmp_dir):
    audio_fmt = fmt if fmt in ('mp3','m4a','opus','ogg','flac') else 'mp3'
    try:
        jobs[job_id].update({'status':'downloading','progress':5})
        cmd = ['spotdl', url, '--output', tmp_dir, '--format', audio_fmt, '--bitrate','192k','--threads','1']
        logger.info(f'[spotify] {" ".join(cmd)}')
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        progress = 10
        for line in proc.stdout:
            line = line.strip()
            if line: logger.info(f'[spotdl] {line}')
            if 'Downloading' in line or 'Found' in line:
                progress = min(progress+10, 80)
                jobs[job_id]['progress'] = progress
            elif 'Converting' in line or 'Embedding' in line:
                jobs[job_id].update({'progress':90,'status':'processing'})
        proc.wait()
        MEDIA = {'.mp3','.m4a','.opus','.ogg','.flac','.wav'}
        found = next((os.path.join(tmp_dir,f) for f in os.listdir(tmp_dir)
                      if os.path.splitext(f)[1].lower() in MEDIA), None)
        if not found: raise RuntimeError('spotdl не нашёл файл')
        jobs[job_id].update({'file':found,'filename':os.path.basename(found),'status':'done','progress':100})
    except Exception as exc:
        err = str(exc)[:500]
        logger.error(f'[spotify] {err}')
        jobs[job_id].update({'status':'error','error':err})
        shutil.rmtree(tmp_dir, ignore_errors=True)
        jobs[job_id]['tmp_dir'] = None

@app.route('/health')
def health():
    return jsonify({'status':'ok','jobs':len(jobs),'cookies':bool(COOKIE_FILE)})

@app.route('/start', methods=['POST'])
@require_auth
def start():
    data    = request.json or {}
    url     = (data.get('url') or '').strip()
    if not url: return jsonify({'error':'URL обязателен'}), 400
    job_id  = str(uuid.uuid4())
    tmp_dir = tempfile.mkdtemp()
    jobs[job_id] = {'status':'pending','progress':0,'file':None,'filename':None,
                    'error':None,'tmp_dir':tmp_dir,'created_at':time.time()}
    if 'open.spotify.com' in url.lower():
        threading.Thread(target=_spotify_worker,
            args=(job_id,url,data.get('format','mp3'),tmp_dir),daemon=True).start()
    else:
        threading.Thread(target=_download_worker,
            args=(job_id, url,
                  data.get('type','video'),
                  data.get('quality','bestvideo+bestaudio/best'),
                  data.get('format','mp4'),
                  tmp_dir,
                  data.get('audio_lang', None)),  # ← новый параметр
            daemon=True).start()
    return jsonify({'job_id':job_id})

@app.route('/status/<job_id>')
@require_auth
def job_status(job_id):
    j = jobs.get(job_id)
    if not j: return jsonify({'error':'Не найдено'}), 404
    return jsonify({'status':j['status'],'progress':int(j.get('progress',0)),
                    'error':j.get('error'),'filename':j.get('filename')})

@app.route('/file/<job_id>')
@require_auth
def get_file(job_id):
    j = jobs.get(job_id)
    if not j: return jsonify({'error':'Не найдено'}), 404
    if j['status'] != 'done': return jsonify({'error':f'Не готово: {j["status"]}'}), 425
    fp = j.get('file')
    if not fp or not os.path.exists(fp): return jsonify({'error':'Файл удалён'}), 404
    return send_file(fp, as_attachment=True, download_name=j.get('filename','video.mp4'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',8000)), threaded=True)
                        
