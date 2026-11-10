#!/usr/bin/env python3
"""
================================================================================
  BeatStream  v8  -  Reproductor de musica auto-alojado (self-hosted)
================================================================================

QUE ES
  App de musica en UN SOLO ARCHIVO (este). Un backend Flask (Python) sirve una
  SPA (pagina unica HTML/CSS/JS incrustada en la variable UI, mas abajo). Busca,
  reproduce (audio y video) y descarga musica de YouTube / YouTube Music usando
  yt-dlp + ytmusicapi. Pensada para correr en una Raspberry Pi y empaquetarse en
  un APK de Android con Chaquopy (ver GUIA-CHAQUOPY.md).

COMO ARRANCA
  python app.py           ->  instala dependencias que falten y levanta en :5000
  Abre http://localhost:5000  (o la IP de la Pi desde el celular)

ARQUITECTURA (de arriba a abajo en este archivo)
  1. Auto-instalador de dependencias ...... (import + pip si falta)
  2. Rutas de datos y config .............. SANDBOX, *.json, DEFAULT_CFG
  3. Almacenes en memoria + _load/_save ... history, likes, playlists, taste
  4. Algoritmo de gustos y radio .......... record_play, get_radio, _build_mix
  5. Likes y playlists (logica) ........... toggle_like_*, pl_*
  6. yt-dlp: streams e info ................ get_stream_info, get_video_info, do_search
  7. Descargas + metadatos (ID3/MP4) ...... _do_dl, _embed_meta, _square_crop
  8. Rutas HTTP / API (Flask) ............. @app.route(...)  (ver mapa abajo)
  9. UI = r-string gigante ................ TODO el front (HTML+CSS+JS) como texto
 10. if __name__ == "__main__": ........... arranca el servidor

MAPA DE LA API
  GET  /api/stream/<vid>          audio por proxy con Range (302 reproducible)
  GET  /api/vstream/<vid>         video por proxy con Range
  GET  /api/search?q=&page=       resultados de busqueda
  GET  /api/dashboard             datos del inicio (top, recent, mix, suggestions)
  GET  /api/artist?name=          discografia de un artista
  GET  /api/album?id=             canciones de un album
  GET  /api/lyrics?title=&artist= letra (sincronizada si hay)
  GET  /api/cover/<name>          portada embebida de un archivo local
  POST /api/play                  registra reproduccion (alimenta el algoritmo)
  POST /api/feedback              like/skip/complete para aprender gustos
  GET  /api/radio?seed=&artist=   cola de radio infinita
  POST /api/download             inicia descarga (audio o video segun config)
  GET  /api/dl_status/<vid>       progreso de descarga (polling)
  GET/POST/DELETE /api/files...   archivos descargados (sandbox)
  GET/POST /api/like/...          me gusta (yt / local)
  GET/POST/DELETE /api/playlists  CRUD de playlists y sus canciones
  GET/POST /api/config            ajustes (calidad, formato, modo, idioma via front)
  POST /api/history/clear         borra historial
  GET  /                          la SPA

FRONTEND (dentro de la variable UI, en su bloque de script)
  Estado global:   Q/qi (cola YouTube), LQ/lqi (cola local), MODE, curTrack,
                   MA (elemento <audio>), radioOn, LANG (idioma).
  Render:          mkCard (renglon de cancion), mkShelfCard (tarjeta de shelf),
                   renderDash (inicio), renderArtist, loadFiles/Likes/Playlists.
  Reproduccion:    playYT, loadQ, _videoNext, nextTrack. En navegador/Pi el
                   audio es el elemento <audio> (MA); dentro del APK lo reproduce
                   ExoPlayer nativo y MA queda como "reloj" (ver puente AndroidBG).
  Video:           openYTVideo / closeVideo + _enterVidFS/_exitVidFS (pantalla
                   completa con requestFullscreen sobre #vidpanel).
  i18n:            TL(es) traduce; TR = diccionario ES->EN; applyI18n camina el DOM.

Dependencias:  pip install flask yt-dlp mutagen requests ytmusicapi Pillow
================================================================================
"""
import sys, subprocess

PKGS = [("flask","flask"),("yt-dlp","yt_dlp"),("mutagen","mutagen"),("requests","requests"),("ytmusicapi","ytmusicapi"),("Pillow","PIL")]
for pkg, imp in PKGS:
    try: __import__(imp)
    except ImportError:
        print(f"[*] Instalando dependencia: {pkg} ...")
        subprocess.run([sys.executable,"-m","pip","install",pkg,"-q"], check=True)

import json, os, threading, time, uuid, re, shutil
from pathlib import Path
from flask import (Flask, jsonify, request, send_from_directory,
                   Response, stream_with_context)
import requests as req_lib
import yt_dlp

try:
    from ytmusicapi import YTMusic
    _ytm = YTMusic()
    print("[+] ytmusicapi OK")
except Exception as _e:
    _ytm = None
    print("[!] ytmusicapi no disponible:", _e)

try:
    from mutagen.mp3      import MP3
    from mutagen.id3      import ID3
    from mutagen.mp4      import MP4
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus   import OggOpus
    from mutagen           import File as MuFile
    HAS_MUTAGEN = True
except Exception:
    HAS_MUTAGEN = False

FFMPEG_OK = bool(shutil.which("ffmpeg"))

# -----------------------------------------------------
app     = Flask(__name__)
BASE    = Path(os.environ.get("BEATSTREAM_DATA") or Path(__file__).parent)
BASE.mkdir(parents=True, exist_ok=True)
SANDBOX = BASE / "sandbox"; SANDBOX.mkdir(exist_ok=True)
COOKIES  = BASE / "cookies.txt"
CFG_FILE = BASE / "config.json"
HIS_FILE = BASE / "history.json"
LIK_FILE = BASE / "likes.json"
PLS_FILE = BASE / "playlists.json"
TASTE_FILE = BASE / "taste.json"

# ===================================================
#  CONFIG
# ===================================================
DEFAULT_CFG = {
    "download_mode"  : "audio",  # "audio" | "video"
    "audio_quality"  : "best",   # "best" (original) | "128" (compacto)
    "video_quality"  : "720",    # "best" | "1080" | "720" | "480" | "360"
    "audio_format"   : "mp3",    # "mp3" | "m4a" | "opus"
    "search_per_page": 12,
    "history_max"    : 300,
}

def _load(path, default):
    try:
        if path.exists():
            return {**default, **json.loads(path.read_text("utf-8"))}
    except Exception:
        pass
    return dict(default)

def _save(path, data):
    try: path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except Exception: pass

cfg       = _load(CFG_FILE, DEFAULT_CFG)
history   = _load(HIS_FILE, {"tracks": []})
likes     = _load(LIK_FILE, {"yt": {}, "local": {}})
playlists = _load(PLS_FILE, {"lists": []})
taste     = _load(TASTE_FILE, {"artists": {}, "blocked": {}, "skips": {}})
_lock     = threading.Lock()

# ===================================================
#  HISTORIAL
# ===================================================
def record_play(track: dict):
    """Registra una reproduccion en el historial y alimenta el algoritmo de gustos."""
    with _lock:
        tracks = history["tracks"]
        ex = next((t for t in tracks if t["id"] == track["id"]), None)
        now = time.time()
        if ex:
            ex["plays"] = ex.get("plays", 0) + 1
            ex["last"]  = now
            for k in ("title","channel","thumb","dur"):
                if track.get(k): ex[k] = track[k]
        else:
            tracks.insert(0, {
                **{k: track.get(k,"") for k in ("id","title","channel","thumb","dur")},
                "plays": 1, "last": now,
            })
        history["tracks"] = sorted(
            tracks, key=lambda x: x.get("last", 0), reverse=True
        )[:cfg["history_max"]]
        _save(HIS_FILE, history)

# ===================================================
#  ALGORITMO DE GUSTOS + RADIO
# ===================================================
def _record_feedback(vid, action, artist="", title=""):
    """Guarda una senal de gusto (like / skip / complete) para afinar recomendaciones."""
    with _lock:
        arts    = taste.setdefault("artists", {})
        blocked = taste.setdefault("blocked", {})
        skips   = taste.setdefault("skips", {})
        delta = {"complete": 1, "like": 3, "dislike": -4, "skip": -1}.get(action, 0)
        if artist and delta:
            arts[artist] = max(-12, min(25, arts.get(artist, 0) + delta))
        if action == "skip":
            skips[vid] = skips.get(vid, 0) + 1
            if skips[vid] >= 2:
                blocked[vid] = {"title": title, "ts": time.time()}
        elif action == "complete":
            skips.pop(vid, None)
        elif action == "dislike":
            blocked[vid] = {"title": title, "ts": time.time()}
        elif action == "like":
            blocked.pop(vid, None); skips.pop(vid, None)
        _save(TASTE_FILE, taste)

def _top_artist():
    arts = {a: s for a, s in taste.get("artists", {}).items() if s > 0}
    if arts:
        return max(arts, key=lambda a: arts[a])
    for t in history.get("tracks", []):
        if t.get("channel"):
            return t["channel"].split(",")[0].strip()
    return ""

_artist_cache = {}
def _get_artist_cached(name):
    """Devuelve la info de un artista desde cache (o la pide a ytmusicapi y la cachea)."""
    if not _ytm or not name:
        return None
    k = name.strip().lower()
    c = _artist_cache.get(k)
    if c and c["exp"] > time.time():
        return c["data"]
    data = None
    try:
        sr = _ytm.search(name, filter="artists")
        if sr and sr[0].get("browseId"):
            data = _ytm.get_artist(sr[0]["browseId"])
    except Exception:
        data = None
    _artist_cache[k] = {"data": data, "exp": time.time() + 1800}
    return data

def get_radio(seed_vid="", artist="", limit=25):
    """Arma una cola de radio relacionada a una cancion/artista (sin repetidos ni bloqueados)."""
    seed_artist = (artist or "").split(",")[0].strip().lower()
    pool = []
    if _ytm:
        try:
            # 1) Radio de la canción (vibe/relacionadas)
            if seed_vid:
                wp = _ytm.get_watch_playlist(videoId=seed_vid, radio=True, limit=limit + 15)
                for s in ((wp or {}).get("tracks") or []):
                    t = _ytm_track(s)
                    if t:
                        pool.append(t)
            # 2) Canciones del PROPIO artista (mantiene el mix pegado al artista)
            a = _get_artist_cached(artist) if seed_artist else None
            if a:
                for s in ((a.get("songs") or {}).get("results") or [])[:14]:
                    t = _ytm_track(s)
                    if t:
                        pool.append(t)
                # 3) "Fans también escuchan" - artistas relacionados, poquito para variedad
                rel = (a.get("related") or {}).get("results") or []
                for r in rel[:3]:
                    ra = None
                    try:
                        if r.get("browseId"):
                            ra = _ytm.get_artist(r["browseId"])
                    except Exception:
                        ra = None
                    if ra:
                        for s in ((ra.get("songs") or {}).get("results") or [])[:3]:
                            t = _ytm_track(s)
                            if t:
                                pool.append(t)
        except Exception as e:
            print("[radio] fallo:", e)
    if not pool and artist:
        pool = do_search(artist, 0).get("results", [])

    blocked = taste.get("blocked", {})
    arts    = taste.get("artists", {})
    clean, seen = [], set()
    for t in pool:
        vid = t.get("id")
        if not vid or vid == seed_vid or vid in seen or vid in blocked:
            continue
        a2 = (t.get("channel") or "").split(",")[0].strip().lower()
        if arts.get(a2, 0) <= -6:
            continue
        seen.add(vid); clean.append(t)

    # Priorizar: 0=mismo artista, 1=artista que te gusta, 2=resto
    def _tier(t):
        a2 = (t.get("channel") or "").split(",")[0].strip().lower()
        if seed_artist and (seed_artist in a2 or a2 in seed_artist):
            return 0
        if arts.get(a2, 0) > 0:
            return 1
        return 2
    same   = [t for t in clean if _tier(t) == 0]
    others = [t for t in clean if _tier(t) != 0]
    others.sort(key=_tier)
    # Interleave ~2 del artista : 1 de otros (mix de artista con variedad)
    mixed, oi = [], 0
    for i, t in enumerate(same):
        mixed.append(t)
        if i % 2 == 1 and oi < len(others):
            mixed.append(others[oi]); oi += 1
    mixed.extend(others[oi:])
    return mixed[:limit]

_mix_cache = {"t": 0, "data": []}
def _mix_seed():
    import random
    yt = list((likes.get("yt") or {}).values())
    if yt:
        pick = random.choice(yt[:25]) or {}
        if pick.get("id"):
            return pick["id"]
    for t in (history.get("tracks") or [])[:10]:
        if t.get("id"):
            return t["id"]
    return ""

def _taste_artists(n=3):
    # Tus artistas favoritos: primero los que te gustan, si no, los más escuchados
    arts = taste.get("artists", {})
    top = sorted([a for a, s in arts.items() if s > 0], key=lambda a: -arts[a])[:n]
    if len(top) < n:
        lower = [a.lower() for a in top]
        for t in (history.get("tracks") or []):
            ch = (t.get("channel") or "").split(",")[0].strip()
            if ch and ch.lower() not in lower:
                top.append(ch); lower.append(ch.lower())
            if len(top) >= n:
                break
    return top

def _build_mix(limit=24):
    """Construye el 'Mix para ti' del inicio a partir de tus artistas mas escuchados."""
    # "Mix para ti": varios de tus artistas, cada bloque enfocado a su artista,
    # intercalados por turnos para que no sea un revoltijo.
    top = _taste_artists(3)
    if not top:
        return []
    per = max(6, (limit // len(top)) + 2)
    buckets = []
    seen = set()
    for name in top:
        chunk = []
        try:
            for t in get_radio("", name, limit=per):
                vid = t.get("id")
                if vid and vid not in seen:
                    seen.add(vid); chunk.append(t)
        except Exception:
            chunk = []
        if chunk:
            buckets.append(chunk)
    # round-robin entre artistas
    mixed = []
    i = 0
    while any(buckets) and len(mixed) < limit:
        b = buckets[i % len(buckets)]
        if b:
            mixed.append(b.pop(0))
        i += 1
        if i > 400:
            break
    return mixed[:limit]

def _refresh_mix():
    try:
        _mix_cache["data"] = _build_mix(24)
    except Exception:
        pass
    _mix_cache["t"] = time.time()

# ===================================================
#  LIKES
# ===================================================
def toggle_like_yt(track: dict) -> bool:
    with _lock:
        vid = track["id"]
        if vid in likes["yt"]:
            del likes["yt"][vid]; liked = False
        else:
            likes["yt"][vid] = {
                **{k: track.get(k,"") for k in ("id","title","channel","thumb","dur")},
                "added": time.time()
            }
            liked = True
        _save(LIK_FILE, likes); return liked

def toggle_like_local(name: str, meta: dict) -> bool:
    with _lock:
        if name in likes["local"]:
            del likes["local"][name]; liked = False
        else:
            likes["local"][name] = {**meta, "added": time.time()}; liked = True
        _save(LIK_FILE, likes); return liked

def is_liked_yt(vid):    return vid in likes.get("yt", {})
def is_liked_local(name): return name in likes.get("local", {})

# ===================================================
#  PLAYLISTS
# ===================================================
def pl_create(name):
    pl = {"id": str(uuid.uuid4())[:8], "name": name,
          "tracks": [], "created": time.time()}
    with _lock:
        playlists["lists"].append(pl); _save(PLS_FILE, playlists)
    return pl

def pl_delete(plid):
    with _lock:
        playlists["lists"] = [p for p in playlists["lists"] if p["id"] != plid]
        _save(PLS_FILE, playlists)

def pl_rename(plid, name):
    with _lock:
        pl = next((p for p in playlists["lists"] if p["id"]==plid), None)
        if pl: pl["name"] = name; _save(PLS_FILE, playlists)

def pl_add(plid, track):
    with _lock:
        pl = next((p for p in playlists["lists"] if p["id"]==plid), None)
        if not pl: return False
        if not any(t["id"]==track["id"] for t in pl["tracks"]):
            pl["tracks"].append(track); _save(PLS_FILE, playlists)
        return True

def pl_remove_track(plid, tid):
    with _lock:
        pl = next((p for p in playlists["lists"] if p["id"]==plid), None)
        if pl:
            pl["tracks"] = [t for t in pl["tracks"] if t["id"]!=tid]
            _save(PLS_FILE, playlists)

# ===================================================
#  YT-DLP BASE
# ===================================================
def _base():
    o = {
        "quiet": True, "no_warnings": True, "nocheckcertificate": True,
        "extractor_args": {"youtube": {"player_client": ["android","mweb","web"]}},
    }
    if COOKIES.exists(): o["cookiefile"] = str(COOKIES)
    return o

# ===================================================
#  STREAM PROXY - bypasa error 150
# ===================================================
_stream_cache: dict = {}

def get_stream_info(vid: str) -> tuple:
    """Resuelve la URL de audio reproducible de un video de YouTube (via yt-dlp)."""
    entry = _stream_cache.get(vid)
    if entry and entry["exp"] > time.time():
        return entry["url"], entry["hdrs"], entry["ct"]

    # Formato: prioridad a m4a/webm audio puro; fallback a cualquier cosa
    fmt = "140/bestaudio[ext=m4a]/251/bestaudio[ext=webm]/bestaudio[vcodec=none]/bestaudio/best"
    opts = {**_base(), "format": fmt}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://youtube.com/watch?v={vid}", download=False)

    url  = info["url"]
    hdrs = dict(info.get("http_headers") or {})
    ext  = info.get("ext","mp4")
    ct   = {"mp4":"audio/mp4","m4a":"audio/mp4","webm":"audio/webm",
             "ogg":"audio/ogg","opus":"audio/ogg"}.get(ext,"audio/mpeg")
    _stream_cache[vid] = {"url":url,"hdrs":hdrs,"ct":ct,"exp":time.time()+3000}
    return url, hdrs, ct

_vstream_cache: dict = {}

def get_video_info(vid: str, vq: str = "720") -> tuple:
    """Resuelve la URL de video reproducible en la calidad pedida (progresivo <=720p)."""
    key = f"{vid}:{vq}"
    entry = _vstream_cache.get(key)
    if entry and entry["exp"] > time.time():
        return entry["url"], entry["hdrs"], entry["ct"]
    # Progresivo mp4 (video+audio en UN archivo, reproducible en <video> sin MSE).
    # 22=720p, 18=360p. Si no hay progresivo, cae al mejor mp4 combinado.
    vq = str(vq)
    if vq == "360":
        fmt = "18/best[height<=360][ext=mp4][acodec!=none][vcodec!=none]/best[height<=480][acodec!=none][vcodec!=none]"
    elif vq == "480":
        fmt = "18/best[height<=480][ext=mp4][acodec!=none][vcodec!=none]/best[acodec!=none][vcodec!=none]"
    else:
        # 720/1080/best -> mejor progresivo disponible (tope real 720 sin MSE)
        fmt = ("best[height<=720][ext=mp4][acodec!=none][vcodec!=none]/22"
               "/best[ext=mp4][acodec!=none][vcodec!=none]/18"
               "/best[acodec!=none][vcodec!=none]")
    opts = {**_base(), "format": fmt}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://youtube.com/watch?v={vid}", download=False)
    url = info.get("url")
    if not url and info.get("requested_formats"):
        url = info["requested_formats"][0].get("url")
    hdrs = dict(info.get("http_headers") or {})
    ct = "video/mp4"
    _vstream_cache[key] = {"url": url, "hdrs": hdrs, "ct": ct, "exp": time.time()+3000}
    return url, hdrs, ct

# ===================================================
#  BÚSQUEDA
# ===================================================
_scache: dict = {}

def do_search(q: str, page: int = 0) -> dict:
    """Busca canciones en YouTube y devuelve resultados paginados."""
    per = int(cfg["search_per_page"])
    n   = per * (page + 1)
    key = f"{q}|{n}"
    if key not in _scache:
        opts = {**_base(), "extract_flat": True, "playlistend": n}
        out  = []
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{q}", download=False)
            for e in (info.get("entries") or []):
                if not e or not e.get("id"): continue
                dur = int(e.get("duration") or 0); m,s = divmod(dur,60)
                out.append({
                    "id"     : e["id"],
                    "title"  : e.get("title","?"),
                    "channel": e.get("channel") or e.get("uploader","?"),
                    "dur"    : f"{m}:{s:02d}" if dur else "-",
                    "thumb"  : f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
                })
        _scache[key] = out
    chunk = _scache[key][page*per:(page+1)*per]
    return {"results":chunk,"page":page,
            "has_next":len(_scache[key])>=n,"has_prev":page>0}

# ===================================================
#  DESCARGA - config leída en tiempo de ejecución
# ===================================================
_dl: dict = {}

# -- Metadatos de descarga (etiquetas ID3/MP4 + portada) -----------
def _meta_from_info(info: dict) -> dict:
    title   = (info.get("track") or info.get("title") or "").strip()
    artist  = (info.get("artist") or info.get("uploader") or info.get("channel") or "").strip()
    album   = (info.get("album") or "").strip()
    aartist = (info.get("album_artist") or artist).strip()
    ud      = info.get("upload_date") or ""
    if ud and len(ud) == 8:
        date = f"{ud[0:4]}-{ud[4:6]}-{ud[6:8]}"
    elif info.get("release_year"):
        date = str(info["release_year"])
    else:
        date = ""
    try:
        trk = int(info.get("track_number") or info.get("playlist_index") or 0)
    except Exception:
        trk = 0
    return {"title": title, "artist": artist, "album": album,
            "album_artist": aartist, "date": date, "track": trk}

def _thumb_bytes(vid: str, aq: str):
    order = {
        "320": ["maxresdefault", "sddefault", "hqdefault"],
        "192": ["sddefault", "hqdefault", "mqdefault"],
        "128": ["hqdefault", "mqdefault"],
    }.get(str(aq), ["hqdefault", "mqdefault"])
    for name in order:
        try:
            r = req_lib.get(f"https://i.ytimg.com/vi/{vid}/{name}.jpg", timeout=10, verify=False)
            if r.status_code == 200 and len(r.content) > 1500:
                return r.content
        except Exception:
            pass
    return None

def _square_crop(data):
    """Recorta una imagen de portada a cuadrado 720x720 centrado (con Pillow)."""
    # Portada cuadrada centrada - como la muestran las apps de música
    if not data:
        return data
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        s = min(w, h)
        x, y = (w - s)//2, (h - s)//2
        im = im.crop((x, y, x + s, y + s))
        if s > 720:
            im = im.resize((720, 720), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=90)
        return out.getvalue()
    except Exception:
        return data

def _embed_meta(path: Path, vid: str, aq: str, meta: dict):
    """Incrusta metadatos (titulo, artista, album, fecha, portada) en el archivo con mutagen."""
    if not HAS_MUTAGEN:
        return
    ext = path.suffix.lower()
    img = _square_crop(_thumb_bytes(vid, aq))
    if ext == ".mp3":
        from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, APIC
        try:
            tags = ID3(path)
        except Exception:
            tags = ID3()
        if meta["title"]:        tags.setall("TIT2", [TIT2(encoding=3, text=meta["title"])])
        if meta["artist"]:       tags.setall("TPE1", [TPE1(encoding=3, text=meta["artist"])])
        if meta["album_artist"]: tags.setall("TPE2", [TPE2(encoding=3, text=meta["album_artist"])])
        if meta["album"]:        tags.setall("TALB", [TALB(encoding=3, text=meta["album"])])
        if meta["date"]:         tags.setall("TDRC", [TDRC(encoding=3, text=meta["date"])])
        if meta["track"]:        tags.setall("TRCK", [TRCK(encoding=3, text=str(meta["track"]))])
        if img:
            tags.setall("APIC", [APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img)])
        tags.save(path)
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4, MP4Cover
        a = MP4(path)
        if meta["title"]:        a["\xa9nam"] = [meta["title"]]
        if meta["artist"]:       a["\xa9ART"] = [meta["artist"]]
        if meta["album_artist"]: a["aART"]     = [meta["album_artist"]]
        if meta["album"]:        a["\xa9alb"] = [meta["album"]]
        if meta["date"]:         a["\xa9day"] = [meta["date"]]
        if meta["track"]:        a["trkn"]     = [(meta["track"], 0)]
        if img:
            a["covr"] = [MP4Cover(img, imageformat=MP4Cover.FORMAT_JPEG)]
        a.save()
    elif ext in (".opus", ".ogg"):
        import base64
        from mutagen.flac import Picture
        a = OggOpus(path) if ext == ".opus" else OggVorbis(path)
        if meta["title"]:        a["title"]       = [meta["title"]]
        if meta["artist"]:       a["artist"]      = [meta["artist"]]
        if meta["album_artist"]: a["albumartist"] = [meta["album_artist"]]
        if meta["album"]:        a["album"]       = [meta["album"]]
        if meta["date"]:         a["date"]        = [meta["date"]]
        if meta["track"]:        a["tracknumber"] = [str(meta["track"])]
        if img:
            pic = Picture(); pic.type = 3; pic.mime = "image/jpeg"; pic.data = img
            a["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        a.save()

def _do_dl(vid: str, ctx: dict = None):
    """Descarga una cancion segun la config (modo y calidad) y le incrusta los metadatos."""
    # Lee config EN ESTE MOMENTO - nunca del request HTTP
    mode = cfg["download_mode"]
    afmt = cfg["audio_format"]
    aq   = cfg["audio_quality"]
    vq   = cfg["video_quality"]

    _dl[vid] = {"status":"dl","pct":0,"file":None,"error":None,"mode":mode}

    def hook(d):
        if d["status"] == "downloading":
            raw = d.get("_percent_str","0%").strip().rstrip("%")
            try: _dl[vid]["pct"] = min(98, float(raw))
            except Exception: pass
        elif d["status"] == "finished":
            _dl[vid]["pct"] = 99
    def pp_hook(d):
        try:
            if d.get("status") == "started":
                _dl[vid]["status"] = "processing"   # ffmpeg extrayendo/uniendo
        except Exception: pass

    tmpl = str(SANDBOX / "%(title).80s.%(ext)s")

    if mode == "audio":
        if FFMPEG_OK:
            # Con ffmpeg (Pi): extrae el audio. Fiable y SIEMPRE produce audio (nunca video).
            fmt = "bestaudio/best"
            q = "128" if str(aq) == "128" else "0"
            attempts = [
                {"postprocessors": [{
                    "key"             : "FFmpegExtractAudio",
                    "preferredcodec"  : afmt,
                    "preferredquality": q,
                }]},
            ]
        else:
            # Sin ffmpeg (APK): baja audio-only directo (m4a/opus); mutagen etiqueta.
            fmt = "140/bestaudio[ext=m4a]/251/bestaudio[vcodec=none]"
            attempts = [{}]

    else:
        # -- VIDEO -----------------------------------------------
        if vq == "best":
            fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        else:
            fmt = (f"bestvideo[height<={vq}][ext=mp4]+bestaudio[ext=m4a]"
                   f"/bestvideo[height<={vq}]+bestaudio/best[height<={vq}]/best")
        attempts = [{"merge_output_format":"mp4"}, {}]

    last_err = ""
    for extra in attempts:
        opts = {
            **_base(),
            "format"        : fmt,
            "outtmpl"       : tmpl,
            "progress_hooks": [hook],
            "postprocessor_hooks": [pp_hook],
            "noplaylist"    : True,
            **extra,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://youtube.com/watch?v={vid}", download=True)
            # Ruta real del archivo final (ya post-procesado)
            fp = None
            try:
                rd = info.get("requested_downloads") or []
                if rd: fp = rd[0].get("filepath") or rd[0].get("_filename")
            except Exception:
                fp = None
            if not fp:
                all_exts = {".mp3",".m4a",".opus",".webm",".ogg",
                            ".flac",".aac",".mp4",".mkv",".avi"}
                files = sorted(
                    [f for f in SANDBOX.glob("*") if f.suffix.lower() in all_exts],
                    key=lambda f: f.stat().st_mtime, reverse=True,
                )
                fp = str(files[0]) if files else None
            if fp:
                final = Path(fp)
                try:
                    _m = _meta_from_info(info)
                    if ctx:
                        if ctx.get("album"): _m["album"] = ctx["album"]
                        if ctx.get("album_artist"): _m["album_artist"] = ctx["album_artist"]
                        if ctx.get("artist") and not _m["artist"]: _m["artist"] = ctx["artist"]
                        if ctx.get("track"):
                            try: _m["track"] = int(ctx["track"])
                            except Exception: pass
                    _embed_meta(final, vid, aq, _m)
                except Exception as _me:
                    print("[meta] no se pudo etiquetar:", _me)
                _dl[vid] = {"status":"done","pct":100,
                            "file":final.name,"error":None,"mode":mode}
                return
        except Exception as e:
            last_err = str(e)[:300]

    _dl[vid] = {"status":"error","pct":0,"file":None,"error":last_err,"mode":mode}

# ===================================================
#  METADATA ARCHIVOS LOCALES
# ===================================================
VIDEO_EXTS = {".mp4",".mkv",".avi",".mov",".webm"}
AUDIO_EXTS = {".mp3",".m4a",".opus",".ogg",".flac",".aac"}

def read_meta(path: Path) -> dict:
    """Lee los metadatos de un archivo de audio/video local ya descargado."""
    info = {"title":path.stem,"artist":"","album":"","dur_sec":0,"dur":"-",
            "is_video":path.suffix.lower() in VIDEO_EXTS}
    if not HAS_MUTAGEN: return info
    try:
        ext = path.suffix.lower()
        if ext == ".mp3":
            a=MP3(path); info["dur_sec"]=int(a.info.length)
            t=ID3(path); info["title"]=str(t.get("TIT2",path.stem)); info["artist"]=str(t.get("TPE1","")); info["album"]=str(t.get("TALB",""))
        elif ext in (".m4a",".mp4"):
            a=MP4(path); info["dur_sec"]=int(a.info.length)
            t=a.tags or {}; info["title"]=str(t.get("©nam",[path.stem])[0]); info["artist"]=str(t.get("©ART",[""])[0]); info["album"]=str((t.get("©alb") or [""])[0])
        elif ext == ".ogg":
            a=OggVorbis(path); info["dur_sec"]=int(a.info.length)
            info["title"]=a.get("title",[""])[0]; info["artist"]=a.get("artist",[""])[0]; info["album"]=a.get("album",[""])[0]
        elif ext == ".opus":
            a=OggOpus(path); info["dur_sec"]=int(a.info.length)
            info["title"]=a.get("title",[""])[0]; info["artist"]=a.get("artist",[""])[0]; info["album"]=a.get("album",[""])[0]
        else:
            a=MuFile(path)
            if a: info["dur_sec"]=int(getattr(a.info,"length",0))
        if info["dur_sec"]:
            m,s=divmod(info["dur_sec"],60); info["dur"]=f"{m}:{s:02d}"
    except Exception:
        pass
    return info

# ===================================================
#  API ROUTES
# ===================================================

# -- STREAM (proxy con Range) ----------------------
@app.route("/api/stream/<vid>")
def api_stream(vid):
    try:
        url, hdrs, ct = get_stream_info(vid)
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 502

    range_hdr = request.headers.get("Range", "bytes=0-")
    proxy_hdrs = {**hdrs, "Range": range_hdr}

    try:
        upstream = req_lib.get(url, headers=proxy_hdrs,
                               stream=True, timeout=30, verify=False)
        resp_hdrs = {
            "Content-Type" : upstream.headers.get("Content-Type", ct),
            "Accept-Ranges": "bytes",
        }
        if "Content-Range" in upstream.headers:
            resp_hdrs["Content-Range"] = upstream.headers["Content-Range"]
        if "Content-Length" in upstream.headers:
            resp_hdrs["Content-Length"] = upstream.headers["Content-Length"]

        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk: yield chunk

        return Response(stream_with_context(generate()),
                        status=upstream.status_code, headers=resp_hdrs)
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 502

# -- BÚSQUEDA --------------------------------------
@app.route("/api/vstream/<vid>")
def api_vstream(vid):
    try:
        url, hdrs, ct = get_video_info(vid, cfg.get("video_quality", "720"))
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 502
    range_hdr = request.headers.get("Range", "bytes=0-")
    proxy_hdrs = {**hdrs, "Range": range_hdr}
    try:
        upstream = req_lib.get(url, headers=proxy_hdrs, stream=True, timeout=30, verify=False)
        resp_hdrs = {"Content-Type": upstream.headers.get("Content-Type", ct),
                     "Accept-Ranges": "bytes"}
        if "Content-Range" in upstream.headers:
            resp_hdrs["Content-Range"] = upstream.headers["Content-Range"]
        if "Content-Length" in upstream.headers:
            resp_hdrs["Content-Length"] = upstream.headers["Content-Length"]
        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk: yield chunk
        return Response(stream_with_context(generate()),
                        status=upstream.status_code, headers=resp_hdrs)
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 502

_lyrics_cache: dict = {}

def get_lyrics(title: str, artist: str, dur: int = 0) -> dict:
    """Busca la letra (sincronizada si existe) de una cancion en lrclib."""
    import re as _re
    key = f"{title}|{artist}|{dur}"
    c = _lyrics_cache.get(key)
    if c and c["exp"] > time.time():
        return c["data"]
    clean = _re.sub(r"\((?:feat|ft|official|video|audio|lyric|lyrics|hd|4k|remix)[^)]*\)",
                    "", title, flags=_re.I)
    clean = _re.sub(r"\[[^\]]*\]", "", clean).strip() or title
    data = {"synced": "", "plain": "", "has_synced": False}
    try:
        if artist:
            params = {"track_name": clean, "artist_name": artist}
            if dur: params["duration"] = dur
            r = req_lib.get("https://lrclib.net/api/get", params=params,
                            timeout=8, verify=False,
                            headers={"User-Agent": "Lyra (self-hosted)"})
            if r.status_code == 200:
                j = r.json()
                data["synced"] = j.get("syncedLyrics") or ""
                data["plain"]  = j.get("plainLyrics") or ""
        if not data["synced"]:
            q = (clean + " " + artist).strip()
            r = req_lib.get("https://lrclib.net/api/search", params={"q": q},
                            timeout=8, verify=False,
                            headers={"User-Agent": "Lyra (self-hosted)"})
            if r.status_code == 200:
                for j in (r.json() or [])[:6]:
                    if j.get("syncedLyrics"):
                        data["synced"] = j["syncedLyrics"]
                        data["plain"]  = j.get("plainLyrics") or data["plain"]
                        break
                    if not data["plain"] and j.get("plainLyrics"):
                        data["plain"] = j["plainLyrics"]
    except Exception as e:
        data["error"] = str(e)[:120]
    data["has_synced"] = bool(data["synced"])
    _lyrics_cache[key] = {"data": data, "exp": time.time() + 3600}
    return data

@app.route("/api/lyrics")
def api_lyrics():
    title  = request.args.get("title", "").strip()
    artist = request.args.get("artist", "").strip()
    try:
        dur = int(float(request.args.get("dur", "0") or 0))
    except Exception:
        dur = 0
    if not title:
        return jsonify({"has_synced": False, "synced": "", "plain": ""})
    return jsonify(get_lyrics(title, artist, dur))

def _ytm_track(s, fallback_thumb=""):
    vid = s.get("videoId")
    if not vid:
        return None
    thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    arts = s.get("artists") or []
    channel = ", ".join(a.get("name", "") for a in arts if a.get("name")) if arts else (s.get("author") or "")
    dsec = s.get("duration_seconds") or 0
    if not dsec and s.get("duration"):
        try:
            parts = [int(x) for x in str(s["duration"]).split(":")]
            dsec = parts[0]*60 + parts[1] if len(parts) == 2 else parts[0]*3600 + parts[1]*60 + parts[2]
        except Exception:
            dsec = 0
    m, sec = divmod(int(dsec), 60)
    return {"id": vid, "title": s.get("title", ""), "channel": channel,
            "thumb": thumb, "dur": ("%d:%02d" % (m, sec)) if dsec else "-"}

def _ytm_releases(block, limit=36):
    out = []
    for al in ((block or {}).get("results") or [])[:limit]:
        th = (al.get("thumbnails") or [{}])[-1].get("url", "")
        out.append({"id": al.get("browseId"), "title": al.get("title", ""),
                    "thumb": th, "year": al.get("year", "")})
    return out

@app.route("/api/cover/<path:name>")
def api_cover(name):
    safe = os.path.basename(name)
    f = SANDBOX / safe
    if not f.exists() or not HAS_MUTAGEN:
        return "", 404
    ext = f.suffix.lower()
    data = None
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3
            tags = ID3(f)
            for k in list(tags.keys()):
                if k.startswith("APIC"):
                    data = tags[k].data; break
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            a = MP4(f)
            covr = (a.tags or {}).get("covr")
            if covr:
                data = bytes(covr[0])
        elif ext in (".opus", ".ogg"):
            import base64
            from mutagen.flac import Picture
            a = OggOpus(f) if ext == ".opus" else OggVorbis(f)
            b = a.get("metadata_block_picture")
            if b:
                data = Picture(base64.b64decode(b[0])).data
    except Exception:
        data = None
    if not data:
        return "", 404
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

@app.route("/api/artist")
def api_artist():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "sin nombre"}), 400
    if _ytm:
        try:
            res = _ytm.search(name, filter="artists")
            if res:
                bid = res[0].get("browseId")
                thumb0 = (res[0].get("thumbnails") or [{}])[-1].get("url", "")
                a = _ytm.get_artist(bid)
                songs = []
                for s in ((a.get("songs") or {}).get("results") or [])[:24]:
                    t = _ytm_track(s)
                    if t:
                        songs.append(t)
                thumb = (a.get("thumbnails") or [{}])[-1].get("url", "") or thumb0
                return jsonify({
                    "name": a.get("name", name), "thumb": thumb,
                    "desc": (a.get("description") or "")[:400],
                    "subs": a.get("subscribers") or "",
                    "songs": songs,
                    "albums": _ytm_releases(a.get("albums")),
                    "singles": _ytm_releases(a.get("singles")),
                    "source": "ytmusic",
                })
        except Exception as e:
            print("[artist] ytmusic fallo:", e)
    d = do_search(name, 0)
    return jsonify({"name": name, "thumb": "", "desc": "", "subs": "",
                    "songs": d.get("results", []), "albums": [], "singles": [],
                    "source": "search"})

@app.route("/api/album")
def api_album():
    bid = request.args.get("id", "").strip()
    if not bid or not _ytm:
        return jsonify({"error": "no disponible"}), 400
    try:
        a = _ytm.get_album(bid)
        th = (a.get("thumbnails") or [{}])[-1].get("url", "")
        tracks = []
        for s in (a.get("tracks") or []):
            t = _ytm_track(s, th)
            if t:
                if not t["thumb"]:
                    t["thumb"] = th
                tracks.append(t)
        return jsonify({"title": a.get("title", ""), "thumb": th,
                        "year": a.get("year", ""), "tracks": tracks})
    except Exception as e:
        return jsonify({"error": str(e)[:120]}), 502

@app.route("/api/search")
def api_search():
    q    = request.args.get("q","").strip()
    page = int(request.args.get("page",0))
    if not q:
        return jsonify({"results":[],"page":0,"has_next":False,"has_prev":False})
    try:    return jsonify(do_search(q, page))
    except Exception as e: return jsonify({"error":str(e)[:300]}), 500

# -- DASHBOARD -------------------------------------
@app.route("/api/dashboard")
def api_dashboard():
    top    = sorted(history["tracks"], key=lambda x:x.get("plays",0), reverse=True)[:12]
    recent = history["tracks"][:12]
    # Sugerencias = canales únicos más escuchados
    seen, sugs = set(), []
    for t in history["tracks"][:80]:
        ch = t.get("channel","")
        if ch and ch not in seen:
            seen.add(ch); sugs.append(ch)
        if len(sugs) >= 8: break
    if time.time() - _mix_cache["t"] > 300:
        _mix_cache["t"] = time.time()
        threading.Thread(target=_refresh_mix, daemon=True).start()
    return jsonify({"top":top,"recent":recent,"suggestions":sugs,
                    "mix":_mix_cache["data"][:12],
                    "has_history":bool(history["tracks"])})

@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    d = request.get_json() or {}
    vid = d.get("id", ""); action = d.get("action", "")
    artist = (d.get("artist") or "").split(",")[0].strip().lower()
    if vid and action:
        _record_feedback(vid, action, artist, d.get("title", ""))
    return jsonify({"ok": True})

@app.route("/api/radio")
def api_radio():
    seed   = request.args.get("seed", "").strip()
    artist = request.args.get("artist", "").strip()
    return jsonify({"tracks": get_radio(seed, artist)})

# -- REPRODUCCIÓN ----------------------------------
@app.route("/api/play", methods=["POST"])
def api_play():
    t = request.get_json() or {}
    if t.get("id"): record_play(t)
    return jsonify({"ok":True})

# -- DESCARGA --------------------------------------
@app.route("/api/download", methods=["POST"])
def api_download():
    d   = request.get_json() or {}
    vid = d.get("id","")
    if not vid: return jsonify({"error":"id requerido"}), 400
    if _dl.get(vid,{}).get("status") == "dl":
        return jsonify({"ok":True,"msg":"en progreso","mode":cfg["download_mode"]})
    ctx = d.get("ctx") or None
    threading.Thread(target=_do_dl, args=(vid, ctx), daemon=True).start()
    return jsonify({"ok":True,"mode":cfg["download_mode"]})

@app.route("/api/dl_status/<vid>")
def api_dl_status(vid):
    return jsonify(_dl.get(vid, {"status":"none","pct":0}))

# -- ARCHIVOS LOCALES ------------------------------
@app.route("/api/files")
def api_files():
    all_exts = AUDIO_EXTS | VIDEO_EXTS
    out = []
    for f in sorted(SANDBOX.glob("*"), key=lambda x:x.stat().st_mtime, reverse=True):
        if f.suffix.lower() not in all_exts: continue
        m = read_meta(f)
        out.append({
            "name"    : f.name,
            "size"    : f.stat().st_size,
            "ext"     : f.suffix[1:],
            "title"   : m["title"],
            "artist"  : m["artist"],
            "dur"     : m["dur"],
            "dur_sec" : m["dur_sec"],
            "is_video": m["is_video"],
            "album"   : m.get("album",""),
            "liked"   : is_liked_local(f.name),
        })
    return jsonify(out)

@app.route("/api/files/delete", methods=["POST"])
def api_files_delete():
    name = os.path.basename((request.get_json() or {}).get("name", ""))
    if not name:
        return jsonify({"ok": False}), 400
    f = SANDBOX / name
    try:
        if f.exists() and f.resolve().parent == SANDBOX.resolve():
            f.unlink()
        if name in likes.get("local", {}):
            del likes["local"][name]; _save(LIK_FILE, likes)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]}), 500

@app.route("/sandbox/<path:fn>")
def sandbox_file(fn):
    return send_from_directory(SANDBOX, fn, conditional=True)

# -- LIKES -----------------------------------------
@app.route("/api/like/yt", methods=["POST"])
def api_like_yt():
    t = request.get_json() or {}
    if not t.get("id"): return jsonify({"error":"id requerido"}), 400
    return jsonify({"ok":True,"liked":toggle_like_yt(t)})

@app.route("/api/like/local", methods=["POST"])
def api_like_local():
    d = request.get_json() or {}
    name = d.get("name",""); meta = d.get("meta",{})
    if not name: return jsonify({"error":"name requerido"}), 400
    return jsonify({"ok":True,"liked":toggle_like_local(name,meta)})

@app.route("/api/likes")
def api_likes_get():
    return jsonify({
        "yt":    sorted(likes.get("yt",{}).values(),
                        key=lambda x:x.get("added",0), reverse=True),
        "local": sorted(likes.get("local",{}).items(),
                        key=lambda x:x[1].get("added",0), reverse=True),
    })

# -- PLAYLISTS -------------------------------------
@app.route("/api/playlists")
def api_pl_list(): return jsonify(playlists["lists"])

@app.route("/api/playlists", methods=["POST"])
def api_pl_create():
    name = (request.get_json() or {}).get("name","").strip()
    if not name: return jsonify({"error":"nombre requerido"}), 400
    return jsonify({"ok":True,"playlist":pl_create(name)})

@app.route("/api/playlists/<plid>", methods=["DELETE"])
def api_pl_delete(plid): pl_delete(plid); return jsonify({"ok":True})

@app.route("/api/playlists/<plid>/rename", methods=["POST"])
def api_pl_rename(plid):
    name = (request.get_json() or {}).get("name","").strip()
    if name: pl_rename(plid,name)
    return jsonify({"ok":bool(name)})

@app.route("/api/playlists/<plid>/tracks", methods=["POST"])
def api_pl_add_track(plid):
    t = request.get_json() or {}
    if not t.get("id"): return jsonify({"error":"id requerido"}), 400
    return jsonify({"ok":pl_add(plid,t)})

@app.route("/api/playlists/<plid>/tracks/<tid>", methods=["DELETE"])
def api_pl_rm_track(plid,tid): pl_remove_track(plid,tid); return jsonify({"ok":True})

# -- CONFIG ----------------------------------------
@app.route("/api/config")
def api_cfg_get(): return jsonify(cfg)

@app.route("/api/config", methods=["POST"])
def api_cfg_set():
    global cfg
    for k,v in (request.get_json() or {}).items():
        if k in DEFAULT_CFG: cfg[k] = v
    _save(CFG_FILE, cfg)
    return jsonify({"ok":True,"config":cfg})

@app.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    history["tracks"] = []; _save(HIS_FILE, history); return jsonify({"ok":True})

@app.route("/")
def index(): return Response(UI, mimetype="text/html")

# ===================================================
#  UI
# ===================================================
UI = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="theme-color" content="#0d0d0d">
<title>BeatStream</title>
<link rel="preconnect" href="https://i.ytimg.com" crossorigin>
<link rel="preconnect" href="https://lh3.googleusercontent.com" crossorigin>
<link rel="preconnect" href="https://yt3.ggpht.com" crossorigin>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0d0d;--s1:#141414;--s2:#1c1c1c;--s3:#242424;--s4:#2e2e2e;--s5:#3a3a3a;
  --bd:#2a2a2a;--tx:#f0f0f0;--t2:#888;--t3:#3c3c3c;
  --red:#e53935;--rd2:#ff6659;--grn:#1db954;--ylw:#ffd600;
  --bar:82px;--hdr:52px;
}
html,body{height:100%;overflow:hidden;-webkit-tap-highlight-color:transparent;user-select:none}
body{font-family:-apple-system,"Segoe UI",Roboto,sans-serif;
     background:var(--bg);color:var(--tx);display:flex;flex-direction:column}
::-webkit-scrollbar{width:3px}::-webkit-scrollbar-thumb{background:var(--s4);border-radius:3px}
button{cursor:pointer;font-family:inherit;border:none;background:none;color:inherit}
a{color:inherit;text-decoration:none}

/* HEADER */
header{height:var(--hdr);min-height:var(--hdr);background:var(--s1);
  border-bottom:1px solid var(--bd);display:flex;align-items:center;
  gap:8px;padding:0 10px;z-index:20;flex-shrink:0}
.logo{font-size:16px;font-weight:800;display:flex;align-items:center;
  gap:5px;flex-shrink:0;letter-spacing:-.4px}
.logo-dot{width:8px;height:8px;border-radius:50%;background:var(--red)}
.sb{flex:1;display:flex;background:var(--s3);border:1.5px solid var(--bd);
  border-radius:20px;overflow:hidden;transition:border-color .2s;max-width:520px}
.sb:focus-within{border-color:var(--red)}
.sb input{flex:1;background:transparent;border:none;outline:none;color:var(--tx);
  padding:0 14px;font-size:14px;height:36px;min-width:0}
.sb input::placeholder{color:var(--t3)}
.sb button{color:var(--t2);width:40px;font-size:16px;flex-shrink:0;transition:color .15s}
.sb button:hover{color:var(--tx)}
.hbtn{color:var(--t2);font-size:17px;padding:5px 7px;border-radius:8px;
  transition:color .15s,background .15s;flex-shrink:0}
.hbtn:hover{color:var(--tx);background:var(--s3)}

/* TABS */
.tabs{display:flex;background:var(--s1);border-bottom:1px solid var(--bd);
  padding:0 10px;flex-shrink:0;overflow-x:auto}
.tabs::-webkit-scrollbar{display:none}
.tab{padding:9px 12px;font-size:13px;font-weight:500;color:var(--t2);white-space:nowrap;
  border-bottom:2px solid transparent;transition:color .15s,border-color .15s;flex-shrink:0}
.tab:hover{color:var(--tx)}.tab.on{color:var(--tx);border-bottom-color:var(--red)}

/* PANELS */
#shell{display:flex;flex:1;overflow:hidden;min-height:0}
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.panel{flex:1;overflow-y:auto;padding:14px;-webkit-overflow-scrolling:touch}
.panel.hidden{display:none!important}

/* STATES */
.splash{display:flex;flex-direction:column;align-items:center;
  justify-content:center;min-height:180px;gap:10px;color:var(--t3);text-align:center}
.splash-ico{opacity:.18;line-height:0;filter:none}
.ic{width:1.1em;height:1.1em;display:inline-block;vertical-align:-.16em;flex:0 0 auto}
.tab .ic,.sch h2 .ic,.pl-hdr h2 .ic,.pl-act .ic,.ob .ic,.cfg-save .ic,.cfg-db .ic{margin-right:.45em}
.pl-card h3 .ic{margin-right:.4em}
.splash-ico .ic{width:46px;height:46px;stroke-width:1.4}
.splash p .ic{width:1em;height:1em;vertical-align:-.12em;opacity:.65;margin:0 1px}
.cfg-note .ic{width:1em;height:1em;vertical-align:-.12em}
.bc .ic,.bov .ic,.hbtn .ic,.vid-x .ic,.bpp .ic{width:1.2em;height:1.2em}
.bc.on .ic,.lk .ic{fill:currentColor}
.pvol .ic{width:1.1em;height:1.1em;margin-right:3px;opacity:.85}
.tst{display:flex;align-items:center;gap:7px}
.tst-i{display:inline-flex}.tst-i .ic{width:1.15em;height:1.15em}
.splash p{font-size:14px;line-height:1.6}
.swrap{display:flex;align-items:center;justify-content:center;height:140px}
.spin{width:24px;height:24px;border:3px solid var(--s4);
  border-top-color:var(--red);border-radius:50%;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

/* SECTION */
.sch{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.sch h2{font-size:15px;font-weight:700}
.sch small{font-size:11px;color:var(--t2)}
.ssep{margin-bottom:20px}

/* GRID */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:10px}

/* CARD */
.card{background:var(--s2);border-radius:10px;overflow:hidden;
  transition:background .15s,transform .18s,box-shadow .18s;position:relative}
.card:hover{background:var(--s3);transform:translateY(-2px);box-shadow:0 6px 24px #000a}
.card:hover .ov{opacity:1}.card.active{outline:2px solid var(--red)}
.art{position:relative;width:100%;aspect-ratio:16/9;background:var(--s4);overflow:hidden}
.art img{width:100%;height:100%;object-fit:cover;display:block}
.art-ph{width:100%;height:100%;display:flex;align-items:center;
  justify-content:center;font-size:26px;background:linear-gradient(135deg,#1a1a2e,#0f3460)}
.dur-b{position:absolute;bottom:4px;right:4px;background:#000a;
  font-size:10px;padding:1px 5px;border-radius:3px}
.plays-b{position:absolute;top:4px;left:4px;background:#e53935cc;
  font-size:9px;padding:1px 6px;border-radius:3px;font-weight:700}
.ov{position:absolute;inset:0;background:#0008;display:flex;align-items:center;
  justify-content:center;opacity:0;transition:opacity .18s;gap:5px}
.bpp{width:40px;height:40px;border-radius:50%;background:var(--red);
  color:#fff;font-size:18px;display:flex;align-items:center;justify-content:center;
  transition:transform .1s}.bpp:hover{transform:scale(1.08)}
.bov{width:28px;height:28px;border-radius:50%;background:#fff2;border:1px solid #fff3;
  color:#fff;font-size:12px;display:flex;align-items:center;justify-content:center}
.cov{position:absolute;inset:0;background:#000c;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:5px;padding:12px}
.cpb{width:100%;height:3px;background:var(--s4);border-radius:2px;overflow:hidden}
.cpf{height:100%;background:var(--grn);border-radius:2px;transition:width .3s}
.cpp{font-size:10px;color:var(--t2)}
.cb{padding:8px 10px 10px}
.ct{font-size:12px;font-weight:600;line-height:1.35;margin-bottom:2px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.cc{font-size:11px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* CHIPS */
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.chip{background:var(--s3);border:1px solid var(--bd);color:var(--t2);
  padding:5px 12px;border-radius:16px;font-size:12px;transition:all .15s;white-space:nowrap}
.chip:hover{background:var(--red);border-color:var(--red);color:#fff}

/* PAGER */
.pager{display:flex;align-items:center;justify-content:center;gap:10px;margin-top:14px}
.pgb{background:var(--s3);border:1px solid var(--bd);color:var(--tx);
  padding:7px 18px;border-radius:20px;font-size:13px;transition:background .15s}
.pgb:hover{background:var(--s4);border-color:var(--red)}
.pgb:disabled{opacity:.3;pointer-events:none}
.pgi{font-size:12px;color:var(--t2);min-width:52px;text-align:center}

/* FILE ROWS */
.frow{display:flex;align-items:center;gap:9px;padding:10px 12px;
  background:var(--s2);border-radius:8px;margin-bottom:5px;transition:background .15s}
.frow:hover{background:var(--s3)}.frow.playing{outline:1px solid var(--red)}
.fico{font-size:20px;flex-shrink:0;cursor:pointer}
.finfo{flex:1;min-width:0;cursor:pointer}
.fname{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fmeta{font-size:11px;color:var(--t2);margin-top:1px}
.fbtns{display:flex;gap:5px;flex-shrink:0;align-items:center}
.fbtn{color:var(--t2);font-size:15px;width:28px;height:28px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;transition:color .15s,background .15s}
.fbtn:hover{color:var(--tx);background:var(--s4)}.fbtn.lk{color:var(--ylw)}
.fdl{color:var(--rd2);font-size:11px;padding:4px 10px;
  border:1px solid var(--red);border-radius:12px;transition:background .15s,color .15s}
.fdl:hover{background:var(--red);color:#fff}

/* PLAYLISTS */
.pl-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.new-pl{background:var(--red);color:#fff;padding:6px 14px;border-radius:16px;
  font-size:12px;font-weight:600;transition:background .15s}
.new-pl:hover{background:var(--rd2)}
.pl-card{background:var(--s2);border-radius:10px;padding:14px;margin-bottom:8px;
  cursor:pointer;transition:background .15s}
.pl-card:hover{background:var(--s3)}
.pl-card h3{font-size:14px;font-weight:600;margin-bottom:3px}
.pl-card small{font-size:11px;color:var(--t2)}
.pl-acts{display:flex;gap:6px;margin-top:8px}
.pl-act{font-size:11px;padding:3px 10px;border-radius:12px;
  background:var(--s4);color:var(--t2);transition:all .15s}
.pl-act:hover{color:var(--tx);background:var(--s5)}
.pl-act.del:hover{color:var(--rd2);background:#3a1414}

/* PLAYLIST DROPDOWN */
.pl-drop{position:fixed;background:var(--s2);border:1px solid var(--bd);
  border-radius:10px;padding:5px 0;min-width:190px;z-index:201;
  box-shadow:0 8px 24px #000a;display:none}
.pl-drop.show{display:block}
.pl-di{padding:9px 14px;font-size:13px;cursor:pointer;
  transition:background .15s;white-space:nowrap}
.pl-di:hover{background:var(--s3)}
.pl-di.new{color:var(--grn);font-weight:600}

/* INPUT MODAL */
.inp-modal{position:fixed;inset:0;background:#0009;z-index:210;
  display:flex;align-items:center;justify-content:center;padding:16px;
  opacity:0;pointer-events:none;transition:opacity .2s}
.inp-modal.show{opacity:1;pointer-events:auto}
.inp-box{background:var(--s1);border:1px solid var(--bd);border-radius:14px;
  padding:20px;width:100%;max-width:340px}
.inp-box h3{font-size:15px;font-weight:700;margin-bottom:12px}
.inp-box input{width:100%;background:var(--s3);border:1.5px solid var(--bd);
  color:var(--tx);padding:9px 13px;border-radius:8px;font-size:14px;outline:none;
  transition:border-color .2s;font-family:inherit}
.inp-box input:focus{border-color:var(--red)}
.inp-btns{display:flex;gap:8px;margin-top:10px}
.ibtn{flex:1;padding:9px;border-radius:8px;font-size:13px;font-weight:600;transition:all .15s}
.ibtn.ok{background:var(--red);color:#fff}.ibtn.ok:hover{background:var(--rd2)}
.ibtn.no{background:var(--s3);color:var(--t2)}.ibtn.no:hover{color:var(--tx);background:var(--s4)}

/* VIDEO POPUP
   ── Solo se usa para VER el video (no para audio)
   ── El audio principal <audio> se PAUSA cuando se abre
   ── El video embedded arranca en el timestamp exacto
*/
#vidpanel{position:fixed;inset:0;background:#000;z-index:190;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:0;opacity:0;transform:scale(1.03);pointer-events:none;
  transition:opacity .3s ease,transform .3s ease}
#vidpanel.show{opacity:1;transform:scale(1);pointer-events:auto}
#vidpanel:not(.show){display:none !important}
#vidwrap{width:100%;height:100%;max-width:none;border-radius:0;overflow:hidden;
  background:#000;position:relative;display:flex;align-items:center;justify-content:center}
#ytifr{width:100%;aspect-ratio:16/9;border:none;display:block}
#localvid{width:100%;height:100%;max-height:100vh;object-fit:contain;display:none;background:#000}
#localvid.fill{object-fit:cover}
.vc-ic.on{background:#ffffff2e;color:#fff}
.vid-x{position:fixed;top:14px;right:14px;width:42px;height:42px;
  border-radius:50%;background:#ffffff1f;border:none;color:#fff;font-size:20px;
  display:flex;align-items:center;justify-content:center;backdrop-filter:blur(10px);
  z-index:101;transition:background .15s,transform .15s}
.vid-x:active{transform:scale(.9)}.vid-x:hover{background:#ffffff33}

/* PLAYER BAR */
#player{height:var(--bar);min-height:var(--bar);background:var(--s1);
  border-top:1px solid var(--bd);display:flex;flex-direction:column;
  align-items:center;justify-content:center;padding:4px 10px;gap:4px;
  z-index:21;flex-shrink:0}
.pltop{width:100%;display:flex;align-items:center;gap:8px}
.part{width:42px;height:42px;border-radius:6px;overflow:hidden;background:var(--s3);flex-shrink:0}
.part img,.part-ph{width:100%;height:100%;object-fit:cover}
.part-ph{display:flex;align-items:center;justify-content:center;font-size:17px}
.pmeta{flex:1;min-width:0}
.ptit{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pch{font-size:11px;color:var(--t2)}
.pctrl{display:flex;align-items:center;gap:2px;flex-shrink:0}
#brep.on{color:var(--rd2)}#brep.on:hover{color:var(--rd2)}
.bc{color:var(--t2);width:30px;height:30px;border-radius:50%;font-size:14px;
  display:flex;align-items:center;justify-content:center;transition:color .15s,background .15s}
.bc:hover{color:var(--tx);background:var(--s3)}
.bc.main{background:var(--red);color:#fff;width:38px;height:38px;font-size:18px}
.bc.main:hover{background:var(--rd2)}.bc:disabled{opacity:.3;pointer-events:none}
.bc .ic{width:1.15em;height:1.15em;pointer-events:none}
.bc.main{box-shadow:0 4px 14px #0007;transition:transform .12s ease,background .15s ease}
.bc.main .ic{width:21px;height:21px;fill:#fff}
.bc.main .ic.play-ic{transform:translateX(1px)}
.bc.main:hover{transform:scale(1.06)}.bc.main:active{transform:scale(.9)}
.bpp .ic{width:1.5em;height:1.5em}
.spin{animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
.bc.on{color:var(--ylw)}
.pvol{display:flex;align-items:center;gap:4px;flex-shrink:0;color:var(--t3);font-size:13px}
.pvol input{width:58px;accent-color:var(--red)}
.buf-ring{display:none;width:14px;height:14px;border:2px solid var(--s4);
  border-top-color:var(--red);border-radius:50%;animation:sp .7s linear infinite;flex-shrink:0}
.buf-ring.show{display:block}
/* seek */
.plseek{width:100%;display:flex;align-items:center;gap:6px}
.ptime{font-size:10px;color:var(--t3);white-space:nowrap;flex-shrink:0;min-width:30px}
.ptime.r{text-align:right}
.sk{flex:1;height:16px;display:flex;align-items:center;position:relative;cursor:pointer}
.sk-rail{width:100%;height:4px;background:var(--s4);border-radius:2px;
  position:relative;transition:height .1s}
.sk:hover .sk-rail,.sk.drag .sk-rail{height:5px}
.sk-buf{position:absolute;top:0;left:0;height:100%;background:var(--s5);border-radius:2px}
.sk-fill{position:absolute;top:0;left:0;height:100%;background:var(--red);border-radius:2px}
.sk-hdl{position:absolute;top:50%;transform:translate(50%,-50%);right:0;
  width:12px;height:12px;border-radius:50%;background:var(--tx);
  opacity:0;pointer-events:none;box-shadow:0 1px 4px #0009;transition:opacity .15s}
.sk:hover .sk-hdl,.sk.drag .sk-hdl{opacity:1}

/* CONFIG MODAL */
#cfgM{position:fixed;inset:0;background:#0009;z-index:150;
  display:flex;align-items:center;justify-content:center;padding:14px;
  opacity:0;pointer-events:none;transition:opacity .2s}
#cfgM.show{opacity:1;pointer-events:auto}
.cfg-box{background:var(--s1);border:1px solid var(--bd);border-radius:14px;
  width:100%;max-width:420px;max-height:90vh;overflow-y:auto;padding:18px}
.cfg-box h2{font-size:16px;font-weight:700;margin-bottom:14px;
  display:flex;justify-content:space-between;align-items:center}
.cfg-sec{margin-bottom:15px}
.cfg-sec label{display:block;font-size:10px;color:var(--t2);margin-bottom:5px;
  font-weight:700;letter-spacing:.7px;text-transform:uppercase}
.cfg-note{font-size:11px;color:var(--t3);margin-top:5px;line-height:1.5}
.cfg-row{display:flex;gap:5px;flex-wrap:wrap}
.ob{background:var(--s3);border:1.5px solid var(--bd);color:var(--t2);
  padding:5px 13px;border-radius:18px;font-size:12px;font-weight:500;transition:all .15s}
.ob.on{background:var(--red);border-color:var(--red);color:#fff}
.ob:hover:not(.on){border-color:var(--t2);color:var(--tx)}
.cfg-save{width:100%;margin-top:8px;background:var(--red);color:#fff;
  padding:10px;border-radius:10px;font-size:14px;font-weight:600;transition:background .15s}
.cfg-save:hover{background:var(--rd2)}
.cfg-danger{margin-top:12px;padding-top:12px;border-top:1px solid var(--bd)}
.cfg-db{width:100%;background:none;border:1px solid #c62828;
  color:#ef9a9a;padding:8px;border-radius:8px;font-size:13px;transition:all .15s}
.cfg-db:hover{background:#c62828;color:#fff}

/* TOASTS */
#toasts{position:fixed;bottom:calc(var(--bar)+8px);right:10px;
  display:flex;flex-direction:column;gap:5px;z-index:300;pointer-events:none}
.tst{background:#252525;color:var(--tx);border-radius:8px;padding:9px 14px;
  font-size:12px;max-width:280px;box-shadow:0 4px 16px #0009;
  animation:ti .2s ease,to .2s ease 2.8s forwards}
@keyframes ti{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
@keyframes to{to{opacity:0;transform:translateY(-5px)}}

/* PLAYLIST DROP */
.pl-drop{position:fixed;background:var(--s2);border:1px solid var(--bd);
  border-radius:10px;padding:5px 0;min-width:190px;z-index:201;
  box-shadow:0 8px 24px #000a;display:none}
.pl-drop.show{display:block}
.pl-di{padding:9px 14px;font-size:13px;cursor:pointer;transition:background .15s;white-space:nowrap}
.pl-di:hover{background:var(--s3)}.pl-di.new{color:var(--grn);font-weight:600}
.pl-di.has{display:flex;align-items:center;justify-content:space-between;gap:8px;color:var(--t2);cursor:default;opacity:.9}
.pl-di.has:hover{background:transparent}
.pl-di.has .ic{width:16px;height:16px;color:var(--grn);flex:0 0 auto}

@media(max-width:500px){
  .pvol{display:none}
  .grid{grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
}
#karpanel{position:fixed;inset:0;background:linear-gradient(180deg,#0d0d0d,#000);
  z-index:120;display:none;flex-direction:column}
#karpanel.show{display:flex}
.kar-top{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--bd)}
.kar-ti{flex:1;font-weight:700;color:var(--tx);font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kar-x{background:var(--s2);color:var(--tx);border:none;width:34px;height:34px;border-radius:50%;
  font-size:16px;cursor:pointer}
.kar-x:hover{background:var(--s3)}
#karBody{flex:1;overflow-y:auto;padding:32vh 22px;scroll-behavior:smooth;text-align:center}
#karBody::-webkit-scrollbar{width:0}
.kar-line{color:var(--t2);font-size:21px;font-weight:700;line-height:1.42;padding:11px 6px;
  opacity:.4;transition:opacity .3s ease,transform .3s ease;transform:scale(.96);
  will-change:opacity,transform}
.kar-line.clickable{cursor:pointer}
.kar-line.passed{opacity:.26}
.kar-line.on{opacity:1;transform:scale(1.06);color:#fff}
.kar-line .kw{color:#6f6f6f;transition:color .22s ease}
.kar-line.on .kw{color:#6f6f6f}
.kar-line.on .kw.sung{color:var(--rd2);text-shadow:0 0 8px rgba(255,90,80,.28)}
.kar-line.passed .kw{color:#5a5a5a;transition:none}
.kar-plain{color:var(--tx);font-size:16px;line-height:1.7;white-space:pre-wrap;text-align:left;opacity:.92}
.kar-empty{color:var(--t2);text-align:center;padding:40px 10px;font-size:15px;line-height:1.6}
.cc{cursor:pointer}.cc:hover{color:var(--tx);text-decoration:underline}
.pch{cursor:pointer}.pch:hover{color:var(--tx)}
.art-hd{display:flex;align-items:center;gap:16px;padding:8px 2px 18px}
.art-av{width:88px;height:88px;border-radius:50%;object-fit:cover;flex-shrink:0;
  box-shadow:0 6px 20px #000a}
.art-hn{font-size:22px;font-weight:800;color:var(--tx);line-height:1.15}
.art-hs{font-size:12px;color:var(--t2);margin-top:4px}
.agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:14px;padding:4px 0 10px}
.acard{cursor:pointer;transition:transform .15s}
.acard:hover{transform:translateY(-3px)}
.acard img{width:100%;aspect-ratio:1;border-radius:10px;object-fit:cover;background:var(--s2);
  box-shadow:0 4px 12px #0007}
.acard .an{font-size:12.5px;color:var(--tx);font-weight:600;margin-top:6px;line-height:1.25;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.acard .ay{font-size:11px;color:var(--t2);margin-top:1px}
/* ═══════════ POLISH / ESTÉTICA COMERCIAL ═══════════ */
:root{--rdg:linear-gradient(135deg,#ff5e57,#e5143a)}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
header{backdrop-filter:blur(14px);background:rgba(18,18,18,.82)}
.logo{letter-spacing:-.5px}
.logo-dot{background:var(--rdg);box-shadow:0 0 12px #e5143a99}
.sb{background:var(--s2);border-radius:14px;border-color:transparent}
.sb:focus-within{border-color:var(--red);box-shadow:0 0 0 3px #e5143a22}
.tabs{background:rgba(18,18,18,.9);backdrop-filter:blur(14px)}
.tab{padding:11px 13px;font-weight:600}
.tab.on{border-bottom-color:transparent;position:relative}
.tab.on::after{content:"";position:absolute;left:11px;right:11px;bottom:0;height:2.5px;
  background:var(--rdg);border-radius:3px}
.sch h2{font-size:17px;letter-spacing:-.3px}
.ssep{margin-bottom:22px}
.grid{gap:14px}
.card{border-radius:14px;background:var(--s1)}
.card:hover{transform:translateY(-4px);box-shadow:0 12px 32px #000b}
.card:active{transform:translateY(-1px) scale(.994)}
.art{background:var(--s3)}
.art::before{content:"";position:absolute;inset:0;z-index:0;
  background:linear-gradient(100deg,var(--s3) 28%,var(--s4) 50%,var(--s3) 72%);
  background-size:220% 100%;animation:shim 1.25s linear infinite}
.art:has(img.ld)::before{animation:none;background:none}
.art img,.art-ph{position:relative;z-index:1}
.art img{opacity:0;transition:opacity .45s ease}
.art img.ld{opacity:1}
@keyframes shim{to{background-position:-220% 0}}
.ov{background:linear-gradient(0deg,#000d 0%,#0006 60%,#0003 100%)}
.bpp{background:var(--rdg);box-shadow:0 6px 20px #e5143a55}
.ct{font-size:12.5px;font-weight:600;letter-spacing:-.1px}
.dur-b{background:#000b;backdrop-filter:blur(4px);border-radius:5px;font-weight:600}
.plays-b{background:var(--rdg);border-radius:5px}
.bc.main{background:var(--rdg)}.bc.main:hover{background:var(--rdg)}
#player{backdrop-filter:blur(14px);background:rgba(18,18,18,.92)}
.part{border-radius:8px;box-shadow:0 2px 8px #0006}
.chip{border-radius:14px;font-weight:500}
.chip:hover{color:var(--tx);border-color:var(--t2);background:var(--s4)}
.cfg-box{border-radius:18px}
/* artista: wrap con shimmer + fade */
.acard .aw{position:relative;border-radius:12px;overflow:hidden;background:var(--s3);aspect-ratio:1;
  box-shadow:0 4px 14px #0008}
.acard .aw::before{content:"";position:absolute;inset:0;
  background:linear-gradient(100deg,var(--s3) 28%,var(--s4) 50%,var(--s3) 72%);
  background-size:220% 100%;animation:shim 1.25s linear infinite}
.acard .aw:has(img.ld)::before{animation:none;background:none}
.acard img{width:100%;height:100%;object-fit:cover;display:block;position:relative;z-index:1;
  opacity:0;transition:opacity .45s;border-radius:0}
.acard img.ld{opacity:1}
.art-av{opacity:0;transition:opacity .4s}.art-av.ld{opacity:1}
/* responsive */
@media(max-width:560px){
  .grid{grid-template-columns:repeat(2,1fr);gap:11px}
  .sch h2{font-size:16px}
  header{padding:0 8px}
  .art-av{width:72px;height:72px}
  .art-hn{font-size:19px}
}
@media(min-width:900px){
  .panel{padding:20px 26px}
  .grid{grid-template-columns:repeat(auto-fill,minmax(172px,1fr));gap:16px}
}
.vid-dl{position:fixed;top:14px;right:66px;width:42px;height:42px;border-radius:50%;
  background:#ffffff1f;border:none;color:#fff;display:flex;align-items:center;justify-content:center;
  backdrop-filter:blur(10px);z-index:101;transition:background .15s,transform .15s}
.vid-dl:hover{background:#ffffff33}.vid-dl:active{transform:scale(.9)}
.vid-dl:disabled{opacity:.6;pointer-events:none}
.vid-dl .ic{width:20px;height:20px}
.vidload{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
  background:#0007;z-index:5;pointer-events:none}
.vidload.show{display:flex}
.vidload .ic{width:48px;height:48px}
.fart{position:relative;width:46px;height:46px;border-radius:7px;overflow:hidden;flex-shrink:0;
  cursor:pointer;background:var(--s4)}
.fico-ph{position:absolute;inset:0;z-index:0;display:flex;align-items:center;justify-content:center;color:var(--t2)}
.fico-ph .ic{width:22px;height:22px}
.fart img{position:absolute;inset:0;z-index:1;width:100%;height:100%;object-fit:cover;
  opacity:0;transition:opacity .35s}
.fart img.ld{opacity:1}
.vidload{z-index:7}
#vctrl{position:absolute;inset:0;z-index:6;opacity:0;pointer-events:none;
  transition:opacity .25s ease;display:flex;flex-direction:column;justify-content:space-between}
#vctrl.show{opacity:1;pointer-events:auto}
.vc-top{display:flex;align-items:center;gap:10px;padding:16px 14px 30px;
  background:linear-gradient(180deg,#000c 0%,#0007 45%,transparent)}
.vc-title{flex:1;color:#fff;font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;text-shadow:0 1px 5px #000b}
.vc-center{flex:1;display:flex;align-items:center;justify-content:center;gap:36px}
.vc-big{width:70px;height:70px;border-radius:50%;background:#ffffff24;backdrop-filter:blur(10px);
  display:flex;align-items:center;justify-content:center;color:#fff;
  box-shadow:0 8px 28px #0007;transition:transform .13s ease,background .15s}
.vc-big:hover{background:#ffffff3a}.vc-big:active{transform:scale(.88)}
.vc-big .ic{width:34px;height:34px;fill:#fff}.vc-big .ic.play-ic{transform:translateX(2px)}
.vc-mini{width:48px;height:48px;border-radius:50%;color:#fff;display:flex;align-items:center;
  justify-content:center;background:#ffffff14;transition:transform .13s ease,background .15s}
.vc-mini:hover{background:#ffffff26}.vc-mini:active{transform:scale(.86)}.vc-mini .ic{width:27px;height:27px}
.vc-bottom{display:flex;align-items:center;gap:9px;padding:30px 14px 16px;
  background:linear-gradient(0deg,#000d 0%,#0008 50%,transparent)}
.vc-t{color:#fff;font-size:11px;font-variant-numeric:tabular-nums;min-width:34px;text-shadow:0 1px 3px #000}
.vc-bottom .vc-t:nth-of-type(2){text-align:right}
.vc-sk{flex:1;height:20px;display:flex;align-items:center;cursor:pointer;position:relative;touch-action:none}
.vc-rail{width:100%;height:4px;background:#ffffff33;border-radius:3px;position:relative;transition:height .12s}
.vc-sk:hover .vc-rail,.vc-sk.drag .vc-rail{height:6px}
.vc-buf{position:absolute;top:0;left:0;height:100%;width:0;background:#ffffff40;border-radius:3px}
.vc-fill{position:absolute;top:0;left:0;height:100%;width:0;background:var(--rdg);border-radius:3px}
.vc-hdl{position:absolute;top:50%;left:0;transform:translate(-50%,-50%);width:14px;height:14px;
  border-radius:50%;background:#fff;box-shadow:0 1px 6px #000b;opacity:1;transition:transform .15s}
.vc-sk:hover .vc-hdl,.vc-sk.drag .vc-hdl{opacity:1}
.vc-ic{width:42px;height:42px;border-radius:50%;color:#fff;display:flex;align-items:center;
  justify-content:center;transition:background .15s,transform .12s;flex-shrink:0}
.vc-ic:hover{background:#ffffff1f}.vc-ic:active{transform:scale(.9)}
.vc-ic .ic{width:22px;height:22px}.vc-back .ic{width:26px;height:26px}
@media(max-width:560px){.vc-center{gap:26px}.vc-big{width:64px;height:64px}.vc-big .ic{width:30px;height:30px}
  .vc-mini{width:44px;height:44px}}
#np{position:fixed;inset:0;z-index:110;background:var(--bg);
  transform:translateY(100%);transition:transform .38s cubic-bezier(.33,0,.1,1);
  display:flex;flex-direction:column;overflow:hidden}
#np.show{transform:translateY(0)}
.np-bg{position:absolute;inset:0;background-size:cover;background-position:center;
  filter:blur(64px) saturate(1.5) brightness(.48);transform:scale(1.35);z-index:0}
.np-bg::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,#0005 0%,var(--bg) 88%)}
.np-inner{position:relative;z-index:1;flex:1;display:flex;flex-direction:column;min-height:0;
  padding:12px 24px 26px;max-width:540px;width:100%;margin:0 auto}
.np-top{display:flex;align-items:center;gap:10px;padding:4px 0 6px}
.np-sub{flex:1;text-align:center;font-size:11px;letter-spacing:2px;color:var(--t2);
  text-transform:uppercase;font-weight:700}
.np-ic{width:42px;height:42px;border-radius:50%;color:var(--tx);display:flex;align-items:center;
  justify-content:center;transition:background .15s}.np-ic:hover{background:#ffffff14}
.np-ic:active{transform:scale(.9)}.np-ic .ic{width:24px;height:24px}
.np-cover{flex:1;display:flex;align-items:center;justify-content:center;padding:14px 0;min-height:0}
.np-cover-wrap{position:relative;width:min(76vw,330px);aspect-ratio:1;border-radius:16px;overflow:hidden;
  box-shadow:0 22px 55px #000b;background:var(--s3)}
.np-cover-wrap img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .45s}
.np-cover-wrap img.ld{opacity:1}
.np-cover-ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--t2)}
.np-cover-ph .ic{width:62px;height:62px}
.np-meta{padding:6px 2px 2px}
.np-title{font-size:22px;font-weight:800;letter-spacing:-.5px;line-height:1.2;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.np-artist{font-size:14px;color:var(--t2);margin-top:5px;font-weight:500;cursor:pointer;display:inline-block}
.np-artist:active{color:var(--tx)}
.np-seek{padding:16px 0 4px}
.np-sk{height:22px;display:flex;align-items:center;cursor:pointer;position:relative;touch-action:none}
.np-rail{width:100%;height:4px;background:var(--s4);border-radius:3px;position:relative;transition:height .12s}
.np-sk:hover .np-rail,.np-sk.drag .np-rail{height:6px}
.np-buf{position:absolute;top:0;left:0;height:100%;width:0;background:var(--s5);border-radius:3px}
.np-fill{position:absolute;top:0;left:0;height:100%;width:0;background:var(--rdg);border-radius:3px}
.np-hdl{position:absolute;top:50%;left:0;transform:translate(-50%,-50%);width:15px;height:15px;border-radius:50%;
  background:#fff;box-shadow:0 1px 6px #000b;opacity:0;transition:opacity .15s}
.np-sk:hover .np-hdl,.np-sk.drag .np-hdl{opacity:1}
.np-times{display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-top:7px;
  font-variant-numeric:tabular-nums}
.np-ctrl{display:flex;align-items:center;justify-content:space-between;padding:12px 0 4px}
.np-cb{width:54px;height:54px;border-radius:50%;color:var(--tx);display:flex;align-items:center;
  justify-content:center;transition:transform .12s,background .15s}
.np-cb:hover{background:#ffffff12}.np-cb:active{transform:scale(.86)}.np-cb .ic{width:32px;height:32px;fill:currentColor}
.np-play{width:68px;height:68px;border-radius:50%;background:var(--rdg);color:#fff;display:flex;align-items:center;
  justify-content:center;box-shadow:0 8px 26px #e5143a66;transition:transform .12s}
.np-play:active{transform:scale(.9)}.np-play .ic{width:30px;height:30px;fill:#fff}.np-play .ic.play-ic{transform:translateX(2px)}
.np-like,.np-kar{width:48px;height:48px;border-radius:50%;color:var(--t2);display:flex;align-items:center;
  justify-content:center;transition:color .15s,transform .12s}
.np-like:active,.np-kar:active{transform:scale(.85)}.np-like .ic,.np-kar .ic{width:25px;height:25px}
.np-kar:hover{color:var(--tx)}
.np-like.on{color:var(--red)}.np-like.on .ic{fill:currentColor}
.np-extra{display:flex;gap:10px;padding:16px 0 0;justify-content:center}
.np-xb{display:flex;align-items:center;gap:7px;padding:10px 18px;border-radius:14px;background:#ffffff12;
  color:var(--tx);font-size:13px;font-weight:600;transition:background .15s}
.np-xb:hover{background:#ffffff1f}.np-xb:active{transform:scale(.96)}.np-xb .ic{width:18px;height:18px}
.part,.ptit{cursor:pointer}
@media(max-width:560px){.np-title{font-size:20px}.np-play{width:64px;height:64px}}
#dlwidget{position:fixed;left:50%;bottom:calc(var(--bar) + 14px);
  transform:translateX(-50%) translateY(16px);z-index:95;display:flex;align-items:center;gap:11px;
  background:rgba(26,26,26,.96);backdrop-filter:blur(16px);border:1px solid var(--bd);border-radius:16px;
  padding:9px 16px 9px 9px;box-shadow:0 10px 34px #000c;max-width:88vw;
  opacity:0;pointer-events:none;transition:opacity .3s,transform .3s}
#dlwidget.show{opacity:1;transform:translateX(-50%) translateY(0);pointer-events:auto}
.dlw-ring{position:relative;width:40px;height:40px;flex-shrink:0}
.dlw-ring svg{width:100%;height:100%;transform:rotate(-90deg)}
.dlw-bg{fill:none;stroke:var(--s4);stroke-width:3}
.dlw-pr{fill:none;stroke:var(--red);stroke-width:3;stroke-linecap:round;stroke-dasharray:100;stroke-dashoffset:100;
  transition:stroke-dashoffset .4s ease,stroke .3s}
#dlwidget.done .dlw-pr{stroke:var(--grn)}
.dlw-dl,.dlw-chk{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}
.dlw-dl{color:var(--tx)}.dlw-dl .ic{width:17px;height:17px}
#dlwidget:not(.done) .dlw-dl{animation:dlpulse 1.2s ease-in-out infinite}
@keyframes dlpulse{0%,100%{opacity:.55;transform:translateY(1px)}50%{opacity:1;transform:translateY(-2px)}}
.dlw-chk{color:var(--grn);opacity:0;transform:scale(.5);transition:opacity .25s,transform .25s}
.dlw-chk .ic{width:18px;height:18px}
#dlwidget.done .dlw-chk{opacity:1;transform:scale(1)}#dlwidget.done .dlw-dl{opacity:0}
.dlw-t{font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:210px}
.dlw-s{font-size:11px;color:var(--t2);margin-top:1px}
.alb-top{display:flex;align-items:center;gap:8px}
.alb-ttl{display:flex;align-items:center;gap:8px;flex:1;min-width:0}
.alb-ttl h2{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dla-btn{display:flex;align-items:center;gap:6px;background:var(--rdg);color:#fff;font-size:12.5px;
  font-weight:700;padding:8px 14px;border-radius:13px;flex-shrink:0;box-shadow:0 4px 14px #e5143a44;
  transition:transform .12s}
.dla-btn:active{transform:scale(.94)}.dla-btn .ic{width:16px;height:16px}
.alb-hd{display:flex;align-items:center;gap:13px;padding:14px 4px 9px;margin-top:4px}
.alb-cv{width:58px;height:58px;border-radius:9px;object-fit:cover;box-shadow:0 5px 16px #0009;
  flex-shrink:0;background:var(--s4)}
.alb-n{font-size:16px;font-weight:800;letter-spacing:-.3px;line-height:1.2}
.alb-c{font-size:11.5px;color:var(--t2);margin-top:3px}
.dlrow{display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--s2);
  border-radius:9px;margin-bottom:6px}
.dlrow-t{flex:1;min-width:0;font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dlrow-bar{width:34%;max-width:160px;height:5px;background:var(--s4);border-radius:3px;overflow:hidden;flex-shrink:0}
.dlrow-f{height:100%;background:var(--rdg);border-radius:3px;transition:width .3s ease}
.dlrow-p{font-size:11px;color:var(--t2);width:36px;text-align:right;flex-shrink:0;font-variant-numeric:tabular-nums}
#dlActive:not(:empty){margin-bottom:10px}
#qpanel{position:fixed;inset:0;z-index:115;background:var(--bg);transform:translateY(100%);
  transition:transform .35s cubic-bezier(.33,0,.1,1);display:flex;flex-direction:column}
#qpanel.show{transform:translateY(0)}
.q-top{display:flex;align-items:center;gap:10px;padding:16px 18px;border-bottom:1px solid var(--bd)}
.q-ti{flex:1;font-size:16px;font-weight:800;letter-spacing:-.3px}
#qList{flex:1;overflow-y:auto;padding:8px 12px calc(env(safe-area-inset-bottom,0) + 24px)}
.q-lbl{font-size:11px;letter-spacing:1.3px;text-transform:uppercase;color:var(--t2);font-weight:700;margin:14px 6px 7px}
.qrow{display:flex;align-items:center;gap:11px;padding:7px 8px;border-radius:11px;cursor:pointer;transition:background .15s}
.qrow:hover{background:var(--s2)}.qrow.on{background:var(--s2)}
.qrow-c{width:46px;height:46px;border-radius:7px;object-fit:cover;flex-shrink:0;background:var(--s4)}
.qrow-i{flex:1;min-width:0}
.qrow-t{font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qrow.on .qrow-t{color:var(--red)}
.qrow-a{font-size:11.5px;color:var(--t2);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qrow-x{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  color:var(--t2);flex-shrink:0;transition:color .15s,background .15s}
.qrow-x:hover{color:var(--tx);background:var(--s4)}.qrow-x .ic{width:16px;height:16px}
#vidpanel.rot{overflow:hidden}
#vidpanel.rot #vidwrap{position:absolute;top:50%;left:50%;width:100vh;height:100vw;
  max-width:none;max-height:none;transform:translate(-50%,-50%) rotate(90deg);transform-origin:center center}
#ctxveil{position:fixed;inset:0;z-index:199;display:none;pointer-events:auto}
#ctxveil:not(.show){pointer-events:none;display:none}
#ctxveil.show{display:block}
#ctxmenu{position:fixed;z-index:200;min-width:216px;max-width:260px;padding:7px;display:none;
  background:rgba(26,26,26,.98);backdrop-filter:blur(18px);border:1px solid var(--border2,#2a2a2a);
  border-radius:16px;box-shadow:0 16px 48px #000d, 0 2px 8px #0008;
  transform-origin:var(--ox,10%) var(--oy,0%)}
#ctxmenu.show{display:block;animation:ctxin .16s cubic-bezier(.2,.9,.3,1.2)}
@keyframes ctxin{from{opacity:0;transform:scale(.82)}to{opacity:1;transform:scale(1)}}
.ctx-i{display:flex;align-items:center;gap:12px;padding:11px 13px;border-radius:11px;cursor:pointer;
  font-size:14px;font-weight:600;color:var(--tx);transition:background .12s}
.ctx-i:hover,.ctx-i:active{background:var(--s3)}
.ctx-i .ic{width:19px;height:19px;color:var(--t2);flex-shrink:0}
.ctx-i:hover .ic{color:var(--tx)}
.ctx-i.danger{color:#ff6b6b}.ctx-i.danger .ic{color:#ff6b6b}
.ctx-i.danger:hover{background:#c6282822}
/* ── corazón "me gusta": relleno + pop ── */
@keyframes heartpop{0%{transform:scale(1)}30%{transform:scale(1.45)}55%{transform:scale(.88)}75%{transform:scale(1.12)}100%{transform:scale(1)}}
.bc.on .ic,.np-like.on .ic,.fbtn.lk .ic,.lk .ic{fill:currentColor;animation:heartpop .5s cubic-bezier(.2,.8,.3,1.1)}
.bc.on,.np-like.on{color:var(--ylw)}
.np-like.on{color:var(--ylw)}
.fbtn.lk{color:var(--ylw)}

/* ═══════════ PULIDO PREMIUM — botones, tabs, cards ═══════════ */
.bc{transition:background .18s ease,color .18s ease,transform .12s cubic-bezier(.2,.7,.3,1.25)}
.bc:hover{background:var(--s4)}
.bc:active{transform:scale(.84)}
.bc.main{box-shadow:0 6px 20px rgba(229,57,53,.45),0 2px 6px #0007}
.bc.main:hover{box-shadow:0 9px 28px rgba(229,57,53,.58),0 2px 6px #0007;transform:scale(1.07)}
.bc.main:active{transform:scale(.9)}
#bvid.on,#bkar.on{color:var(--red)}
.tab{position:relative;transition:color .18s ease}
.tab.on::after{content:"";position:absolute;left:11px;right:11px;bottom:0;height:2.5px;border-radius:3px;background:var(--rdg);box-shadow:0 1px 6px rgba(229,57,53,.5)}
.hbtn,.np-ic,.fbtn,.vid-x{transition:background .18s ease,color .18s ease,transform .12s cubic-bezier(.2,.7,.3,1.25)}
.hbtn:active,.np-ic:active,.fbtn:active,.vid-x:active{transform:scale(.86)}
.card{transition:transform .22s cubic-bezier(.2,.7,.3,1),box-shadow .22s ease}
.card:hover{transform:translateY(-3px);box-shadow:0 10px 30px #0008}
.card:active{transform:translateY(-1px) scale(.99)}
.ob{transition:all .16s ease}
.ob:active{transform:scale(.95)}
.ob.on{box-shadow:0 4px 14px rgba(229,57,53,.4)}
.pl-di{transition:background .15s ease,padding-left .15s ease}
.pl-di:not(.has):hover{padding-left:18px;background:var(--s3)}
.cfg-save{transition:transform .12s ease,background .18s ease,box-shadow .2s ease;box-shadow:0 4px 16px rgba(229,57,53,.38)}
.cfg-save:hover{box-shadow:0 7px 22px rgba(229,57,53,.5)}
.cfg-save:active{transform:scale(.97)}
.sb{transition:border-color .2s ease,box-shadow .2s ease}
.sb:focus-within{box-shadow:0 0 0 3px rgba(229,57,53,.16)}
.tst{box-shadow:0 8px 28px #000a;border:1px solid var(--bd)}
.lk .ic,.bc.on .ic{filter:drop-shadow(0 0 5px rgba(255,214,0,.5))}
button{-webkit-tap-highlight-color:transparent}

/* ═══════════ LISTA DE CANCIONES estilo YT Music (nuestro toque) ═══════════ */
.grid:has(.srow){display:flex;flex-direction:column;gap:2px}
.srow{display:flex;align-items:center;gap:12px;padding:7px 8px;border-radius:12px;
  cursor:pointer;transition:background .16s ease;position:relative;transform:none !important;box-shadow:none !important}
.srow:hover{background:var(--s2)}
.srow:active{background:var(--s3)}
.srow.active{background:linear-gradient(90deg,rgba(229,57,53,.17),rgba(229,57,53,.04) 55%,transparent)}
.srow .art{position:relative;width:52px;height:52px;border-radius:10px;overflow:hidden;
  flex-shrink:0;background:var(--s3);aspect-ratio:auto}
.srow .art img{width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .35s}
.srow .art img.ld{opacity:1}
.sr-ph{position:absolute;inset:0;align-items:center;justify-content:center;color:var(--t3);background:linear-gradient(135deg,#242433,#14141f)}
.sr-ph .ic{width:22px;height:22px}
.sr-play{position:absolute !important;inset:0;width:100%;height:100%;border-radius:0;
  background:#0007;color:#fff;opacity:0;transition:opacity .15s}
.srow:hover .sr-play{opacity:1}
.sr-play .ic{width:22px;height:22px}
.sr-meta{flex:1;min-width:0}
.sr-ti{font-size:14px;font-weight:600;color:var(--tx);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;letter-spacing:-.1px}
.srow.active .sr-ti{color:var(--rd2)}
.sr-ar{font-size:12px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.sr-arn{cursor:pointer}
.sr-arn:hover{color:var(--tx);text-decoration:underline}
.sr-dur{font-size:12px;color:var(--t3);font-variant-numeric:tabular-nums;flex-shrink:0;min-width:32px;text-align:right}
.sr-act{flex-shrink:0;width:34px;height:34px;border-radius:50%;color:var(--t2);
  background:transparent;border:none;opacity:.7;display:flex;align-items:center;justify-content:center;
  transition:background .15s,color .15s,opacity .15s,transform .1s}
.sr-act:hover{color:var(--tx);background:var(--s3);opacity:1}
.sr-act:active{transform:scale(.88)}
.sr-act .ic{width:18px;height:18px}
/* Ecualizador animado en la cancion que suena */
.sr-eq{position:absolute;inset:0;display:none;align-items:flex-end;justify-content:center;
  gap:2.5px;padding-bottom:17px;background:linear-gradient(0deg,#000b,#0006)}
.srow.active .sr-eq{display:flex}
.srow.active:hover .sr-play{opacity:0}
.sr-eq span{width:3px;height:14px;background:var(--rd2);border-radius:2px;
  transform-origin:bottom;animation:eqbar .9s ease-in-out infinite}
.sr-eq span:nth-child(2){animation-delay:.28s}
.sr-eq span:nth-child(3){animation-delay:.56s}
@keyframes eqbar{0%,100%{transform:scaleY(.35)}50%{transform:scaleY(1)}}
/* En pantallas anchas, la lista no se estira infinito */
@media(min-width:720px){.grid:has(.srow){max-width:860px}}

/* ═══════════ PULIDO COMERCIAL — animaciones finas ═══════════ */
/* pantalla completa real del contenedor */
#vidpanel:fullscreen,#vidpanel:-webkit-full-screen{width:100vw;height:100vh;background:#000}
#vidpanel:fullscreen #vidwrap,#vidpanel:-webkit-full-screen #vidwrap{width:100%;height:100%}

/* entrada escalonada de la lista de canciones */
@keyframes rowIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.grid:has(.srow) .srow{animation:rowIn .40s cubic-bezier(.22,.7,.3,1) both}
.srow:nth-child(1){animation-delay:.01s}.srow:nth-child(2){animation-delay:.045s}
.srow:nth-child(3){animation-delay:.08s}.srow:nth-child(4){animation-delay:.115s}
.srow:nth-child(5){animation-delay:.15s}.srow:nth-child(6){animation-delay:.185s}
.srow:nth-child(7){animation-delay:.22s}.srow:nth-child(8){animation-delay:.255s}
.srow:nth-child(9){animation-delay:.29s}.srow:nth-child(n+10){animation-delay:.32s}

/* miniatura: leve zoom al interactuar */
.srow .art img{transition:opacity .35s ease,transform .35s cubic-bezier(.2,.7,.3,1)}
.srow:hover .art img{transform:scale(1.07)}
.srow:active .art{transform:scale(.97);transition:transform .1s}

/* encabezados de seccion */
.sch h2{letter-spacing:-.4px}
.sch{animation:rowIn .45s ease both}


/* barra de progreso del player: brillo al arrastrar */
#skFill,.vc-fill{box-shadow:0 0 8px rgba(229,57,53,.5)}

/* cambio de pestana: panel entra suave */
.panel:not(.hidden){animation:panelIn .32s ease both}
@keyframes panelIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}

/* toasts con entrada mas viva */
@keyframes tstIn{from{opacity:0;transform:translateY(16px) scale(.96)}to{opacity:1;transform:translateY(0) scale(1)}}
.tst{animation:tstIn .3s cubic-bezier(.2,.8,.3,1.1) both}

/* ripple sutil al tocar renglon */
.srow::after{content:"";position:absolute;inset:0;border-radius:12px;background:radial-gradient(circle at center,rgba(255,255,255,.12),transparent 60%);opacity:0;transition:opacity .4s;pointer-events:none}
.srow:active::after{opacity:1;transition:opacity 0s}

/* respeta usuarios que prefieren menos movimiento */
@media(prefers-reduced-motion:reduce){*{animation-duration:.01ms !important;transition-duration:.08s !important}}

/* ═══════════ SHELVES HORIZONTALES (deslizables) estilo YT Music ═══════════ */
.shelf{display:flex;gap:13px;overflow-x:auto;overflow-y:hidden;padding:4px 2px 12px;
  scroll-snap-type:x proximity;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.shelf::-webkit-scrollbar{display:none}
.shcard,.plshc{flex:0 0 auto;width:134px;scroll-snap-align:start;cursor:pointer}
.sh-art{position:relative;width:134px;height:134px;border-radius:13px;overflow:hidden;
  background:var(--s3);box-shadow:0 4px 14px #0006}
.sh-art img{width:100%;height:100%;object-fit:cover;opacity:0;
  transition:opacity .4s ease,transform .4s cubic-bezier(.2,.7,.3,1)}
.sh-art img.ld{opacity:1}
.sh-ph{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
  color:var(--t3);background:linear-gradient(135deg,#242433,#13131d)}
.sh-ph .ic{width:34px;height:34px}
.plart .sh-ph{color:var(--rd2)}
.shcard:hover .sh-art img,.plshc:hover .sh-art img{transform:scale(1.06)}
.shcard:active .sh-art,.plshc:active .sh-art{transform:scale(.96);transition:transform .1s}
.sh-play{position:absolute;right:8px;bottom:8px;width:40px;height:40px;border-radius:50%;
  background:var(--rdg);display:flex;align-items:center;justify-content:center;
  opacity:0;transform:translateY(8px);transition:opacity .22s,transform .22s;
  box-shadow:0 8px 22px rgba(0,0,0,.5)}
.shcard:hover .sh-play{opacity:1;transform:translateY(0)}
.sh-play .ic{width:19px;height:19px;fill:#fff}
.sh-ti{font-size:13px;font-weight:600;margin-top:8px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;letter-spacing:-.1px}
.sh-ar{font-size:11.5px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.shcard.active .sh-ti,.plshc.active .sh-ti{color:var(--rd2)}
.shcard.active .sh-art{outline:2px solid var(--red);outline-offset:-2px}
/* el nombre del artista en renglon ya no es link */
.sr-arn:hover{color:var(--t2);text-decoration:none}
/* entrada suave de los shelves */
.shelf{animation:rowIn .45s ease both}

/* indicador de radio infinito (tres puntos que laten) */
.q-more{display:flex;gap:6px;align-items:center;justify-content:center;padding:22px 0 30px}
.q-more span{width:7px;height:7px;border-radius:50%;background:var(--rd2);opacity:.5;animation:qmore 1s ease-in-out infinite}
.q-more span:nth-child(2){animation-delay:.18s}
.q-more span:nth-child(3){animation-delay:.36s}
@keyframes qmore{0%,100%{opacity:.35;transform:scale(.8)}50%{opacity:1;transform:scale(1.15)}}

/* descarga: barra "procesando" (ffmpeg) animada, para que no parezca congelada en 99% */
.cov.proc .cpf{width:100% !important;background:linear-gradient(90deg,#1db95433 25%,var(--grn) 50%,#1db95433 75%);background-size:200% 100%;animation:dlsweep .9s linear infinite;transition:none}
@keyframes dlsweep{0%{background-position:200% 0}100%{background-position:-200% 0}}
.cov.proc .cpp{color:var(--grn)}

/* barra de SIN INTERNET (solo aparece cuando de plano no hay red) */
#offbar{position:fixed;top:0;left:0;right:0;z-index:9999;background:#2a1414;color:#ff8a80;
  font-size:13px;font-weight:600;text-align:center;padding:9px 12px;
  display:flex;align-items:center;justify-content:center;gap:8px;
  transform:translateY(-105%);transition:transform .32s cubic-bezier(.2,.8,.3,1);
  border-bottom:1px solid #4a1f1f;box-shadow:0 4px 16px #0007}
#offbar.show{transform:translateY(0)}
#offbar .ic{width:16px;height:16px;flex-shrink:0}
</style>
</head>
<body>

<header>
  <a class="logo" href="#" onclick="showDash();return false">
    <div class="logo-dot"></div>BeatStream
  </a>
  <div class="sb">
    <input id="qi" type="text" placeholder="Artista, canción, álbum…"
           autocomplete="off" spellcheck="false">
    <button id="qb">⌕</button>
  </div>
  <button class="hbtn" id="btnCfg"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
</header>

<div class="tabs">
  <div class="tab on" id="t-home"  onclick="switchTab('home')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10l9-7 9 7"/><path d="M5 9v11h14V9"/><path d="M10 20v-6h4v6"/></svg> Inicio</div>
  <div class="tab"    id="t-music" onclick="switchTab('music')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg> Buscar</div>
  <div class="tab"    id="t-files" onclick="switchTab('files')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg> Descargas</div>
  <div class="tab"    id="t-likes" onclick="switchTab('likes')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 5.6a5 5 0 0 0-7.1 0L12 7.3l-1.7-1.7a5 5 0 0 0-7.1 7.1l1.7 1.7L12 21l7.1-7.1 1.7-1.7a5 5 0 0 0 0-7.1z"/></svg> Me gusta</div>
  <div class="tab"    id="t-pl"    onclick="switchTab('pl')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h13M3 12h13M3 18h9"/><circle cx="19" cy="16" r="2"/><path d="M21 16V8"/></svg> Playlists</div>
</div>

<div id="shell"><div id="main">
  <div class="panel" id="panel-home"><div class="swrap"><div class="spin"></div></div></div>
  <div class="panel hidden" id="panel-music">
    <div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg></div><p>Busca tu música favorita</p></div>
  </div>
  <div class="panel hidden" id="panel-files"></div>
  <div class="panel hidden" id="panel-likes"></div>
  <div class="panel hidden" id="panel-pl"></div>

  <!-- PLAYER BAR -->
  <div id="player">
    <div class="pltop">
      <div class="part" onclick="openNP()">
        <div class="part-ph" id="pph"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div>
        <img id="pimg" style="display:none" alt="">
      </div>
      <div class="pmeta">
        <div class="ptit" id="ptit" onclick="openNP()">Sin reproducción</div>
        <div class="pch" id="pch" onclick="openArtist(this.textContent)">—</div>
      </div>
      <div class="pctrl">
        <button class="bc" id="bprev" disabled><svg class="ic" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="2.4" height="14" rx="1"/><path d="M20 5.5v13c0 .82-.93 1.3-1.6.83l-9-6.5a1 1 0 0 1 0-1.66l9-6.5c.67-.48 1.6 0 1.6.83z"/></svg></button>
        <button class="bc main" id="bplay"><svg class="ic play-ic" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5.3c0-.8.86-1.3 1.54-.88l11 6.7c.65.4.65 1.36 0 1.76l-11 6.7A1.03 1.03 0 0 1 7 18.7z"/></svg></button>
        <button class="bc" id="bnext" disabled><svg class="ic" viewBox="0 0 24 24" fill="currentColor"><rect x="16.6" y="5" width="2.4" height="14" rx="1"/><path d="M4 5.5v13c0 .82.93 1.3 1.6.83l9-6.5a1 1 0 0 0 0-1.66l-9-6.5C4.93 4.2 4 4.68 4 5.5z"/></svg></button>
        <button class="bc" id="brep" title="Repetir"></button>
        <button class="bc" id="blike" title="Me gusta"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 5.6a5 5 0 0 0-7.1 0L12 7.3l-1.7-1.7a5 5 0 0 0-7.1 7.1l1.7 1.7L12 21l7.1-7.1 1.7-1.7a5 5 0 0 0 0-7.1z"/></svg></button>
        <button class="bc" id="bvid"  title="Ver video" ><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M9.5 8.5l5 2.5-5 2.5z" fill="currentColor" stroke="none"/><path d="M8 21h8M12 18v3"/></svg></button>
        <button class="bc" id="bkar" title="Karaoke"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg></button>
      </div>
      <div class="buf-ring" id="bufRing"></div>
      <div class="pvol"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M16 9a3 3 0 0 1 0 6"/><path d="M19 6a7 7 0 0 1 0 12"/></svg><input type="range" id="vol" min="0" max="100" value="85"></div>
    </div>
    <div class="plseek">
      <span class="ptime" id="pnow">0:00</span>
      <div class="sk" id="sk">
        <div class="sk-rail" id="skRail">
          <div class="sk-buf"  id="skBuf"  style="width:0%"></div>
          <div class="sk-fill" id="skFill" style="width:0%"></div>
          <div class="sk-hdl"  id="skHdl"></div>
        </div>
      </div>
      <span class="ptime r" id="ptot">0:00</span>
    </div>
  </div>
</div></div>

<!-- ══ VIDEO PANEL ════════════════════════════════ -->
<!-- ══ COLA ══════════════════════════════════════ -->
<div id="qpanel">
  <div class="q-top"><div class="q-ti">A continuación</div><button class="np-ic" onclick="closeQueue()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></button></div>
  <div id="qList"></div>
</div>

<!-- ══ NOW PLAYING ════════════════════════════════ -->
<div id="np">
  <div class="np-bg" id="npBg"></div>
  <div class="np-inner">
    <div class="np-top">
      <button class="np-ic" onclick="closeNP()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></button>
      <div class="np-sub">Reproduciendo</div>
      <button class="np-ic" onclick="openQueue()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h11M4 12h11M4 17h7"/><path d="M16 14l5 3-5 3z" fill="currentColor" stroke="none"/></svg></button>
      <button class="np-ic" style="display:none" onclick="npDownload()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg></button>
    </div>
    <div class="np-cover"><div class="np-cover-wrap"><img id="npImg" alt=""><div class="np-cover-ph" id="npPh"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div></div></div>
    <div class="np-meta">
      <div class="np-title" id="npTitle">—</div>
      <div class="np-artist" id="npArtist" onclick="npArtistClick()">—</div>
    </div>
    <div class="np-seek">
      <div class="np-sk" id="npSk"><div class="np-rail"><div class="np-buf" id="npBuf"></div><div class="np-fill" id="npFill"></div><div class="np-hdl" id="npHdl"></div></div></div>
      <div class="np-times"><span id="npNow">0:00</span><span id="npDur">0:00</span></div>
    </div>
    <div class="np-ctrl">
      <button class="np-like" id="npLike" onclick="G('blike').click()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 5.6a5 5 0 0 0-7.1 0L12 7.3l-1.7-1.7a5 5 0 0 0-7.1 7.1l1.7 1.7L12 21l7.1-7.1 1.7-1.7a5 5 0 0 0 0-7.1z"/></svg></button>
      <button class="np-cb" onclick="G('bprev').click()"><svg class="ic" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="2.4" height="14" rx="1"/><path d="M20 5.5v13c0 .82-.93 1.3-1.6.83l-9-6.5a1 1 0 0 1 0-1.66l9-6.5c.67-.48 1.6 0 1.6.83z"/></svg></button>
      <button class="np-play" id="npPlay" onclick="G('bplay').click()"><svg class="ic play-ic" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5.3c0-.8.86-1.3 1.54-.88l11 6.7c.65.4.65 1.36 0 1.76l-11 6.7A1.03 1.03 0 0 1 7 18.7z"/></svg></button>
      <button class="np-cb" onclick="G('bnext').click()"><svg class="ic" viewBox="0 0 24 24" fill="currentColor"><rect x="16.6" y="5" width="2.4" height="14" rx="1"/><path d="M4 5.5v13c0 .82.93 1.3 1.6.83l9-6.5a1 1 0 0 0 0-1.66l-9-6.5C4.93 4.2 4 4.68 4 5.5z"/></svg></button>
      <button class="np-kar" onclick="openKaraoke()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg></button>
    </div>
    <div class="np-extra">
      <button class="np-xb" onclick="startRadio()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="13.5" r="3.2"/><path d="M8.5 8a5 5 0 0 1 7 0M6 5.5a8.5 8.5 0 0 1 12 0"/></svg> Radio</button>
      <button class="np-xb" onclick="npVideo()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M9.5 8.5l5 2.5-5 2.5z" fill="currentColor" stroke="none"/><path d="M8 21h8M12 18v3"/></svg> Video</button>
      <button class="np-xb" onclick="npDownload()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg> Descargar</button>
    </div>
  </div>
</div>

<div id="vidpanel">
  <div id="vidwrap">
    <div id="ytifr"></div>
    <video id="localvid" playsinline preload="metadata"></video>
    <div class="vidload" id="vidLoad"><svg class="ic spin" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.2-8.55"/></svg></div>
    <div id="vctrl">
      <div class="vc-top">
        <button class="vc-ic vc-back" onclick="closeVideo()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></button>
        <div class="vc-title" id="vcTitle">Video</div>
        <button class="vc-ic" id="vidDl" title="Descargar" onclick="dlCurrent()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg></button>
      </div>
      <div class="vc-center">
        <button class="vc-mini" onclick="vSkip(-10)"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6.5 9 11 13"/><path d="M6.5 9H15a5 5 0 1 1-5 5"/><text x="12.3" y="15.6" font-size="6.5" fill="currentColor" stroke="none" text-anchor="middle" font-weight="700">10</text></svg></button>
        <button class="vc-big" id="vcPlay" onclick="vTogglePlay()"><svg class="ic play-ic" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5.3c0-.8.86-1.3 1.54-.88l11 6.7c.65.4.65 1.36 0 1.76l-11 6.7A1.03 1.03 0 0 1 7 18.7z"/></svg></button>
        <button class="vc-mini" onclick="vSkip(10)"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13 5 17.5 9 13 13"/><path d="M17.5 9H9a5 5 0 1 0 5 5"/><text x="11.7" y="15.6" font-size="6.5" fill="currentColor" stroke="none" text-anchor="middle" font-weight="700">10</text></svg></button>
      </div>
      <div class="vc-bottom">
        <span class="vc-t" id="vcNow">0:00</span>
        <div class="vc-sk" id="vcSk"><div class="vc-rail"><div class="vc-buf" id="vcBuf"></div><div class="vc-fill" id="vcFill"></div><div class="vc-hdl" id="vcHdl"></div></div></div>
        <span class="vc-t" id="vcDur">0:00</span>
        <button class="vc-ic" id="vcMute" onclick="vToggleMute()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M16 9a3 3 0 0 1 0 6"/><path d="M19 6a7 7 0 0 1 0 12"/></svg></button>
        <button class="vc-ic" id="vcZoom" onclick="vZoom()" title="Rellenar"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg></button>
        <button class="vc-ic" id="vcFull" onclick="vFull()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V5a1 1 0 0 1 1-1h4M20 9V5a1 1 0 0 0-1-1h-4M4 15v4a1 1 0 0 0 1 1h4M20 15v4a1 1 0 0 1-1 1h-4"/></svg></button>
      </div>
    </div>
  </div>
</div>
<script src="https://www.youtube.com/iframe_api"></script>

<!-- ══ KARAOKE ════════════════════════════════════ -->
<div id="karpanel">
  <div class="kar-top">
    <div class="kar-ti" id="karTi">Karaoke</div>
    <button class="kar-x" onclick="closeKaraoke()">✕</button>
  </div>
  <div id="karBody"><div class="kar-empty">Buscando letra…</div></div>
</div>

<!-- Playlist dropdown -->
<div class="pl-drop" id="plDrop"></div>
<div id="ctxmenu"></div>
<div id="ctxveil" onclick="closeCtx()"></div>

<!-- Input modal -->
<div class="inp-modal" id="inpM">
  <div class="inp-box">
    <h3 id="inpTitle">Nombre</h3>
    <input id="inpF" type="text" maxlength="60" placeholder="…">
    <div class="inp-btns">
      <button class="ibtn no"  id="inpNo">Cancelar</button>
      <button class="ibtn ok"  id="inpOk">Aceptar</button>
    </div>
  </div>
</div>

<!-- Config modal -->
<div id="cfgM">
  <div class="cfg-box">
    <h2><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg> Configuración <button onclick="closeCfg()">✕</button></h2>

    <div class="cfg-sec">
      <label>Modo de descarga</label>
      <div class="cfg-row">
        <button class="ob" data-key="download_mode" data-val="audio"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg> Solo Audio</button>
        <button class="ob" data-key="download_mode" data-val="video"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M17 9h4M3 15h4M17 15h4"/></svg> Video</button>
      </div>
      <div class="cfg-note">Aplica al botón <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg> en cada canción</div>
    </div>

    <div class="cfg-sec">
      <label>Calidad de Audio</label>
      <div class="cfg-row">
        <button class="ob" data-key="audio_quality" data-val="best">Original (mejor)</button>
        <button class="ob" data-key="audio_quality" data-val="128">Compacto 128k</button>
      </div>
      <div class="cfg-note">Original baja la mejor calidad que ofrece YouTube (~160 kbps, sin re-codificar). Compacto solo reduce el tamaño del archivo.</div>
    </div>

    <div class="cfg-sec">
      <label>Formato de Audio</label>
      <div class="cfg-row">
        <button class="ob" data-key="audio_format" data-val="mp3">MP3</button>
        <button class="ob" data-key="audio_format" data-val="m4a">M4A</button>
        <button class="ob" data-key="audio_format" data-val="opus">OPUS</button>
      </div>
      <div class="cfg-note">Solo aplica al modo Compacto.</div>
    </div>

    <div class="cfg-sec">
      <label>Calidad de Video</label>
      <div class="cfg-row">
        <button class="ob" data-key="video_quality" data-val="best">Mejor</button>
        <button class="ob" data-key="video_quality" data-val="1080">1080p</button>
        <button class="ob" data-key="video_quality" data-val="720">720p</button>
        <button class="ob" data-key="video_quality" data-val="480">480p</button>
        <button class="ob" data-key="video_quality" data-val="360">360p</button>
      </div>
    </div>

    <div class="cfg-sec">
      <label>Resultados por página</label>
      <div class="cfg-row">
        <button class="ob" data-key="search_per_page" data-val="8">8</button>
        <button class="ob" data-key="search_per_page" data-val="12">12</button>
        <button class="ob" data-key="search_per_page" data-val="20">20</button>
      </div>
    </div>

    <div class="cfg-sec">
      <label>Idioma / Language</label>
      <div class="cfg-row">
        <button class="ob lang-opt" data-lang="es" onclick="setLang('es')">Español</button>
        <button class="ob lang-opt" data-lang="en" onclick="setLang('en')">English</button>
      </div>
    </div>

    <button class="cfg-save" onclick="saveCfg()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3h11l3 3v15H5z"/><path d="M8 3v5h7V3M8 21v-7h8v7"/></svg> Guardar configuración</button>
    <div class="cfg-danger">
      <button class="cfg-db" onclick="clearHistory()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg> Borrar historial</button>
    </div>
  </div>
</div>

<!-- AUDIO ELEMENT PRINCIPAL (único, siempre activo) -->
<audio id="ma" preload="metadata"></audio>
<div id="offbar"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 1l22 22"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55M5 12.55a10.94 10.94 0 0 1 5.17-2.39M10.71 5.05A16 16 0 0 1 22.58 9M1.42 9a15.91 15.91 0 0 1 4.7-2.88M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"/></svg><span>Sin conexión a internet</span></div>
<div id="toasts"></div>

<script>
'use strict';

/* ═══════════ i18n — ES-LatAm (predeterminado) / EN ═══════════ */
const LANG_KEY='lyra_lang';
let LANG=(function(){try{return localStorage.getItem(LANG_KEY)||'es';}catch(e){return 'es';}})();
const TR={
 'Inicio':{en:'Home'},'Buscar':{en:'Search'},'Descargas':{en:'Downloads'},'Me gusta':{en:'Liked'},
 'Playlists':{en:'Playlists'},'Radio':{en:'Radio'},'Video':{en:'Video'},'Descargar':{en:'Download'},
 'Configuración':{en:'Settings'},'Guardar configuración':{en:'Save settings'},'Borrar historial':{en:'Clear history'},
 'Nueva playlist':{en:'New playlist'},'Para ti':{en:'For you'},'Mix para ti':{en:'Your Mix'},
 'Más escuchados':{en:'Most played'},'Recientes':{en:'Recent'},'Renombrar':{en:'Rename'},'Eliminar':{en:'Delete'},
 'Canciones':{en:'Songs'},'Álbumes':{en:'Albums'},'Sencillos':{en:'Singles'},'Álbum':{en:'Album'},
 'Modo de descarga':{en:'Download mode'},'Calidad de Audio':{en:'Audio quality'},'Formato de Audio':{en:'Audio format'},
 'Calidad de Video':{en:'Video quality'},'Solo Audio':{en:'Audio only'},'Mejor':{en:'Best'},
 'Resultados por página':{en:'Results per page'},'Nombre':{en:'Name'},'Aceptar':{en:'OK'},'Cancelar':{en:'Cancel'},
 'Crear':{en:'Create'},'Guardar':{en:'Save'},'Renombrar playlist':{en:'Rename playlist'},
 'Buscando letra…':{en:'Searching lyrics…'},'Sonando ahora':{en:'Now playing'},'Sin reproducción':{en:'Nothing playing'},
 'Reproduciendo':{en:'Playing'},'Reproducir':{en:'Play'},'A continuación':{en:'Up next'},'Siguientes':{en:'Up next'},
 'Siguiente ▶':{en:'Next ▶'},'Idioma':{en:'Language'},'Quitar':{en:'Remove'},
 'No hay cola.':{en:'Queue is empty.'},'Playlist vacía.':{en:'Empty playlist.'},
 'Sin descargas aún.':{en:'No downloads yet.'},'Sin me gustas.':{en:'No likes yet.'},'Sin playlists.':{en:'No playlists.'},
 'Sin resultados':{en:'No results'},'Sin resultados para este artista':{en:'No results for this artist'},
 'Tu historial aparecerá aquí':{en:'Your history will appear here'},'Crea tu primera.':{en:'Create your first one.'},
 'Busca tu música favorita':{en:'Search your favorite music'},'Busca tu primera canción':{en:'Search your first song'},
 'No hay karaoke para esta canción.':{en:'No karaoke for this song.'},'No se pudo cargar la letra.':{en:'Could not load lyrics.'},
 'Activa la radio para que no pare.':{en:'Turn on radio so it never stops.'},'Reproduce algo o activa la radio.':{en:'Play something or turn on radio.'},
 'Descargando':{en:'Downloading'},'Descargando álbum':{en:'Downloading album'},
 'Error':{en:'Error'},'Error al buscar':{en:'Search error'},'Error al cargar':{en:'Loading error'},
 'Error al cargar el artista':{en:'Error loading artist'},
 'Usa':{en:'Use'},'Añade canciones con':{en:'Add songs with'},'Aplica al botón':{en:'Applies to the'},
 'en cada canción':{en:'on each song'},'en cualquier canción.':{en:'on any song.'},'mientras escuchas.':{en:'while you listen.'},
 'Pág':{en:'Page'},'Español':{en:'Español'},'English':{en:'English'},
 'Reproduce algo primero':{en:'Play something first'},'Reproduce una canción primero':{en:'Play a song first'},
 'Nada que descargar aquí':{en:'Nothing to download here'},'Nada que descargar':{en:'Nothing to download'},
 'No hay video disponible':{en:'No video available'},'No se pudo cargar el video':{en:'Could not load the video'},
 'Error al cargar el video':{en:'Error loading video'},'No disponible, saltando…':{en:'Unavailable, skipping…'},
 'Configuración guardada':{en:'Settings saved'},'Creando mix de canciones…':{en:'Creating song mix…'},
 'Mix de canciones listo':{en:'Song mix ready'},'No se pudo crear el mix':{en:'Could not create mix'},
 'Descarga el archivo primero':{en:'Download the file first'},'Eliminada':{en:'Deleted'},'Eliminado':{en:'Deleted'},
 'Error al descargar':{en:'Download error'},'Error al guardar':{en:'Save error'},'Error al cargar álbum':{en:'Error loading album'},
 'Guardado':{en:'Saved'},'Historial borrado':{en:'History cleared'},'No se pudo eliminar':{en:'Could not delete'},
 'Playlist creada':{en:'Playlist created'},'Quitado':{en:'Removed'},'Renombrada':{en:'Renamed'},
 'Se reproducirá a continuación':{en:'Will play next'},'Ya hay un álbum descargando':{en:'An album is already downloading'},
 'Álbum no disponible':{en:'Album unavailable'},'Añadido a':{en:'Added to'},'creada':{en:'created'},'guardado':{en:'saved'},
 '¿Borrar historial?':{en:'Clear history?'},'¿Eliminar':{en:'Delete'},'No se puede deshacer.':{en:'This cannot be undone.'},
 'Artista, canción, álbum…':{en:'Artist, song, album…'},
 'Sin conexión a internet':{en:'No internet connection'},
 'Original (mejor)':{en:'Original (best)'},
 'Compacto 128k':{en:'Compact 128k'},
 'Original baja la mejor calidad que ofrece YouTube (~160 kbps, sin re-codificar). Compacto solo reduce el tamaño del archivo.':{en:'Original downloads the best quality YouTube offers (~160 kbps, no re-encoding). Compact only reduces file size.'},
 'Solo aplica al modo Compacto.':{en:'Only applies to Compact mode.'}
};
function TL(es){ if(LANG==='es')return es; const e=TR[es]; return (e&&e.en!=null)?e.en:es; }
const _i18nOrig=new WeakMap();
function _trText(n){
  const raw=n.nodeValue; if(raw==null)return;
  const k=raw.trim(); if(!k)return;
  if(LANG==='en'){ const e=TR[k]; if(e&&e.en!=null){ if(!_i18nOrig.has(n))_i18nOrig.set(n,raw); n.nodeValue=raw.replace(k,e.en); } }
  else { if(_i18nOrig.has(n)){ n.nodeValue=_i18nOrig.get(n); _i18nOrig.delete(n); } }
}
function _walkAll(root){ try{ const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,null); const a=[]; let x; while(x=w.nextNode())a.push(x); a.forEach(_trText); }catch(e){} }
function _trAttrs(){
  try{ document.querySelectorAll('[placeholder]').forEach(el=>{
    let o=el.getAttribute('data-ph-es'); if(o==null){o=el.getAttribute('placeholder')||'';el.setAttribute('data-ph-es',o);}
    el.setAttribute('placeholder', LANG==='en'?((TR[o]&&TR[o].en)||o):o);
  }); }catch(e){}
}
function applyI18n(){ _walkAll(document.body); _trAttrs(); }
function _syncLangUI(){ try{ document.querySelectorAll('.lang-opt').forEach(b=>b.classList.toggle('on',b.dataset.lang===LANG)); }catch(e){} }
function setLang(l){ if(l!==LANG){ LANG=l; try{localStorage.setItem(LANG_KEY,l);}catch(e){} } document.documentElement.lang=l; applyI18n(); _syncLangUI(); }
let _i18nMO=null;
function _i18nBoot(){
  try{
    _i18nMO=new MutationObserver(ms=>{ if(LANG==='es')return;
      ms.forEach(m=>{ if(m.addedNodes) m.addedNodes.forEach(nd=>{ if(nd.nodeType===3)_trText(nd); else if(nd.nodeType===1)_walkAll(nd); }); });
    });
    _i18nMO.observe(document.body,{childList:true,subtree:true});
  }catch(e){}
  document.documentElement.lang=LANG;
  if(LANG==='en')applyI18n();
  _syncLangUI();
}

/* ═══ ESTADO ═══════════════════════════════════════ */
let Q=[], qi=-1;
let LQ=[], lqi=-1;
let MODE='yt';          // 'yt' | 'local'
let curTrack=null;
// ── YouTube IFrame Player ──────────────────────────
let _ytPlayer=null;       // instancia YT.Player
let _ytReady=false;       // API lista
let _ytPendingOpen=null;  // {vid, startSec} si se abre antes de que API esté lista
let _vidIsLocal=false;
let sqQ='', sqPage=0, sqHasNext=false;
let CFG={}, pendCFG={};
let activePlID=null;
let plDropTrack=null;
const pollers={};
let MA=document.getElementById('ma');

/* ═══ REPRODUCTOR NATIVO (ExoPlayer/MediaSession en el APK) ══════════
   Si corremos dentro del APK, el audio lo reproduce Android nativo:
   suena en segundo plano, con notificacion (barra de minutos, play/
   pausa/anterior/siguiente) y repetir. El WebView queda de control y
   el <audio> se reemplaza por un "reloj" que recibe la posicion nativa. */
const NATIVE=(function(){try{return !!(window.AndroidBG&&AndroidBG.isNative&&AndroidBG.isNative());}catch(e){return false;}})();
let RPT=0;   // repetir: 0 off · 1 todo · 2 una  (compartido nativo/web)

function _natUrl(t){
  const isLoc=t.local||String(t.id||'').indexOf('local:')===0;
  if(isLoc){const ln=t.name||String(t.id||'').replace('local:','');return location.origin+'/sandbox/'+encodeURIComponent(ln);}
  return location.origin+'/api/stream/'+t.id;
}
function _natArt(t){
  const v=t.id||'';
  if(v&&!t.local&&String(v).indexOf('local:')!==0)return 'https://i.ytimg.com/vi/'+v+'/hqdefault.jpg';
  return t.thumb||'';
}
function _natItem(t){return {url:_natUrl(t),title:t.title||'',artist:t.channel||t.artist||'',art:_natArt(t)};}
function _pushNativeQueue(idx){try{AndroidBG.setQueue(JSON.stringify(Q.map(_natItem)),idx);AndroidBG.setRepeat(RPT);}catch(e){}}
function _pushLocalNative(idx){try{AndroidBG.setQueue(JSON.stringify(LQ.map(function(f){return {url:location.origin+'/sandbox/'+encodeURIComponent(f.name),title:f.title||f.name,artist:f.artist||'Local',art:f.thumb||''};})),idx);AndroidBG.setRepeat(RPT);}catch(e){}}
function _natAppend(tracks){try{AndroidBG.addItems(JSON.stringify(tracks.map(_natItem)));}catch(e){}}

let _natLastIdx=-1;
function _onNativeTrack(i){
  if(MODE==='local'){
    if(i<0||i>=LQ.length)return;
    lqi=i;const f=LQ[i];curTrack={id:'local:'+f.name,...f};
    document.querySelectorAll('.frow.playing').forEach(r=>r.classList.remove('playing'));
    const row=G('fr-'+i);if(row)row.classList.add('playing');
    setMeta(f.title||f.name.replace(/\.[^.]+$/,''),f.artist||'Local',null,true);
    setLikeBtn(f.name,'local');
    setMediaMeta({title:f.title,channel:f.artist||'Local',thumb:''});
    _karRefresh();_natLastIdx=i;return;
  }
  if(i<0||i>=Q.length)return;
  if(_natLastIdx>=0&&i>_natLastIdx&&Q[_natLastIdx])_feedback(Q[_natLastIdx],'complete');
  qi=i;const t=Q[i];curTrack=t;
  hlCard(t.id);
  setMeta(t.title,t.channel,t.thumb,false);
  setMediaMeta(t);
  setLikeBtn(t.id,'yt');
  G('bprev').disabled=Q.length<=1;G('bnext').disabled=false;
  fetch('/api/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(t)}).catch(()=>{});
  _karRefresh();
  if(G('qpanel')&&G('qpanel').classList.contains('show'))renderQueue();
  _ensureRadio();
  _natLastIdx=i;
}

if(NATIVE){
  const L={};let _ct=0,_dur=0,_paused=true,_rs=0,_src='',_vol=1;
  function fire(type){(L[type]||[]).slice().forEach(function(o){try{o.fn({type:type});}catch(e){}});if(L[type])L[type]=L[type].filter(function(o){return !o.once;});}
  MA={
    get currentTime(){return _ct;},
    set currentTime(v){_ct=v;try{AndroidBG.seekTo(Math.round(v*1000));}catch(e){}},
    get duration(){return _dur;},
    get paused(){return _paused;},
    get ended(){return false;},
    get readyState(){return _rs;},
    get muted(){return false;},set muted(v){},
    get buffered(){return {length:0,end:function(){return 0;}};},
    get src(){return _src;},set src(v){_src=v;},
    get volume(){return _vol;},set volume(v){_vol=v;},
    play(){try{AndroidBG.play();}catch(e){}},
    pause(){try{AndroidBG.pause();}catch(e){}},
    load(){},
    addEventListener:function(t,fn,opts){(L[t]=L[t]||[]).push({fn:fn,once:!!(opts&&opts.once)});},
    removeEventListener:function(t,fn){if(L[t])L[t]=L[t].filter(function(o){return o.fn!==fn;});}
  };
  window.BS={
    state:function(p){_paused=!p;fire(p?'play':'pause');},
    pos:function(ms,dur){_ct=(ms||0)/1000;if(dur&&dur>0)_dur=dur/1000;_rs=4;fire('timeupdate');},
    ready:function(){_rs=4;fire('canplay');},
    waiting:function(){fire('waiting');},
    track:function(i){_onNativeTrack(i);},
    ended:function(){_paused=true;fire('pause');},
    needmore:function(){try{_ensureRadio();}catch(e){}},
    error:function(){fire('error');}
  };
}


/* ═══ MEDIA SESSION ═════════════════════════════════ */
if('mediaSession' in navigator){
  navigator.mediaSession.setActionHandler('play',()=>MA.play());
  navigator.mediaSession.setActionHandler('pause',()=>MA.pause());
  navigator.mediaSession.setActionHandler('previoustrack',()=>G('bprev').click());
  navigator.mediaSession.setActionHandler('nexttrack',()=>G('bnext').click());
}
function setMediaMeta(t){
  if(!('mediaSession' in navigator)||!t)return;
  let art=[];
  const vid=t.id||'';
  if(vid&&!t.isLocal){
    const defs=[['default','120x90'],['mqdefault','320x180'],['hqdefault','480x360'],['sddefault','640x480'],['maxresdefault','1280x720']];
    art=defs.map(d=>({src:`https://i.ytimg.com/vi/${vid}/${d[0]}.jpg`,sizes:d[1],type:'image/jpeg'}));
  }else if(t.thumb){
    art=[{src:t.thumb,sizes:'320x180',type:'image/jpeg'}];
  }
  navigator.mediaSession.metadata=new MediaMetadata({
    title:t.title||'',artist:t.channel||t.artist||'',album:'BeatStream',artwork:art
  });
}

/* ═══ AUDIO EVENTS ══════════════════════════════════ */
MA.addEventListener('timeupdate',()=>updateSeek(MA.currentTime,MA.duration||0));
MA.addEventListener('progress',()=>{
  if(!MA.duration)return;
  try{const b=MA.buffered;if(b.length)G('skBuf').style.width=(b.end(b.length-1)/MA.duration*100)+'%';}
  catch(e){}
});
MA.addEventListener('play',()=>{
  G('bplay').innerHTML=IC.pause;G('bufRing').classList.remove('show');
  if('mediaSession' in navigator)navigator.mediaSession.playbackState='playing';
  try{if(window.AndroidBG)AndroidBG.playing();}catch(e){}   // mantiene vivo el audio en 2do plano (APK)
});
MA.addEventListener('pause',()=>{
  G('bplay').innerHTML=IC.play;
  if('mediaSession' in navigator)navigator.mediaSession.playbackState='paused';
  try{if(window.AndroidBG)AndroidBG.paused();}catch(e){}
});
MA.addEventListener('waiting',()=>G('bufRing').classList.add('show'));
MA.addEventListener('canplay',()=>G('bufRing').classList.remove('show'));
MA.addEventListener('play',()=>document.body.classList.add('playing'));
MA.addEventListener('pause',()=>document.body.classList.remove('playing'));
MA.addEventListener('ended',()=>document.body.classList.remove('playing'));
MA.addEventListener('ended',()=>{
  if(RPT===2){MA.currentTime=0;MA.play().catch(()=>{});return;}
  G('bplay').innerHTML=IC.play;
  if(MODE==='local'){if(LQ.length>1)playLocal((lqi+1)%LQ.length);return;}
  if(curTrack)_feedback(curTrack,'complete');
  _playNextYT();
});
let _errCount=0;
MA.addEventListener('canplay',()=>{_errCount=0;});
MA.addEventListener('error',()=>{
  if(MA.muted)return;
  G('bufRing').classList.remove('show');
  G('bplay').innerHTML=IC.play;
  const msg=MA.error?.message||'';
  if(msg.toLowerCase().includes('abort'))return;
  if(MODE==='yt'&&Q.length>0){
    _errCount++;
    if(_errCount<=8){toast('No disponible, saltando…','err');_playNextYT();return;}
    _errCount=0;
  }
  toast((msg||'Error de audio'),'err');
});

/* ═══ SEEK ══════════════════════════════════════════ */
let skDrag=false;
function updateSeek(c,d){
  const p=d>0?(c/d*100):0;
  if(!skDrag){G('skFill').style.width=p+'%';G('skHdl').style.left=p+'%';
    G('pnow').textContent=fmt(c);G('ptot').textContent=fmt(d);}
  if(!npDrag){G('npFill').style.width=p+'%';G('npHdl').style.left=p+'%';
    G('npNow').textContent=fmt(c);G('npDur').textContent=fmt(d);}
}
function seekFrac(f){if(MA.readyState>=1)MA.currentTime=f*(MA.duration||0);}
function fracFromEv(e,el){
  const r=el.getBoundingClientRect(),x=(e.touches?e.touches[0]:e).clientX-r.left;
  return Math.max(0,Math.min(1,x/r.width));
}
const SK=G('sk'),SR=G('skRail');
SK.addEventListener('mousedown',e=>{
  skDrag=true;SK.classList.add('drag');
  const f=fracFromEv(e,SR);G('skFill').style.width=(f*100)+'%';G('skHdl').style.left=(f*100)+'%';
  e.preventDefault();});
document.addEventListener('mousemove',e=>{if(!skDrag)return;const f=fracFromEv(e,SR);G('skFill').style.width=(f*100)+'%';G('skHdl').style.left=(f*100)+'%';});
document.addEventListener('mouseup',e=>{if(!skDrag)return;skDrag=false;SK.classList.remove('drag');seekFrac(fracFromEv(e,SR));});
SK.addEventListener('touchstart',e=>{skDrag=true;SK.classList.add('drag');const f=fracFromEv(e,SR);G('skFill').style.width=(f*100)+'%';G('skHdl').style.left=(f*100)+'%';e.preventDefault();},{passive:false});
document.addEventListener('touchmove',e=>{if(!skDrag)return;const f=fracFromEv(e,SR);G('skFill').style.width=(f*100)+'%';G('skHdl').style.left=(f*100)+'%';},{passive:true});
document.addEventListener('touchend',e=>{if(!skDrag)return;skDrag=false;SK.classList.remove('drag');seekFrac(fracFromEv(e.changedTouches[0],SR));});

/* ═══ VOLUMEN ════════════════════════════════════════ */
G('vol').oninput=()=>{MA.volume=parseInt(G('vol').value)/100;};

/* ═══ REPRODUCIR YOUTUBE ════════════════════════════
   Usa /api/stream/<vid> (proxy HTTP con Range)
   → sin IFrame como audio → sin error 150
═══════════════════════════════════════════════════ */
function playYT(idx){
  if(!Q.length)return;
  MODE='yt';qi=idx;
  const t=Q[idx];curTrack=t;
  const isLoc=t.local||String(t.id||'').indexOf('local:')===0;
  const lname=t.name||String(t.id||'').replace('local:','');
  hlCard(t.id);
  setMeta(t.title,t.channel,t.thumb,false);
  G('bprev').disabled=Q.length<=1;G('bnext').disabled=Q.length<=1;
  G('bplay').innerHTML=IC.load;G('bufRing').classList.add('show');
  if(NATIVE){
    _natLastIdx=qi;
    _pushNativeQueue(qi);
  }else{
    MA.src=isLoc?('/sandbox/'+encodeURIComponent(lname)):('/api/stream/'+t.id);
    MA.volume=parseInt(G('vol').value)/100;
    MA.currentTime=0;
    MA.load();
    MA.addEventListener('canplay',function onReady(){
      MA.removeEventListener('canplay',onReady);
      MA.currentTime=0;
      MA.play().catch(()=>{});
    },{once:true});
  }
  setMediaMeta(t);
  if(isLoc){
    setLikeBtn(lname,'local');
  }else{
    setLikeBtn(t.id,'yt');
    fetch('/api/play',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(t)}).catch(()=>{});
    _ensureRadio();
  }
  if(G('qpanel')&&G('qpanel').classList.contains('show'))renderQueue();
}

/* ═══ REPRODUCIR LOCAL ══════════════════════════════ */
function playLocal(idx){
  if(!LQ.length)return;
  MODE='local';lqi=idx;
  const f=LQ[idx];curTrack={id:'local:'+f.name,...f};
  document.querySelectorAll('.frow.playing').forEach(r=>r.classList.remove('playing'));
  const row=G('fr-'+idx);if(row)row.classList.add('playing');
  setMeta(f.title||f.name.replace(/\.[^.]+$/,''),f.artist||'Local',null,true);
  G('bprev').disabled=LQ.length<=1;G('bnext').disabled=LQ.length<=1;
  G('bplay').innerHTML=IC.load;G('bufRing').classList.add('show');
  if(NATIVE){
    _natLastIdx=lqi;
    _pushLocalNative(lqi);
  }else{
    MA.src='/sandbox/'+encodeURIComponent(f.name);
    MA.volume=parseInt(G('vol').value)/100;
    MA.currentTime=0;
    MA.load();
    MA.addEventListener('canplay',function onReady(){
      MA.removeEventListener('canplay',onReady);
      MA.currentTime=0;
      MA.play().catch(()=>{});
    },{once:true});
  }
  setLikeBtn(f.name,'local');
  setMediaMeta({title:f.title,channel:f.artist||'Local',thumb:''});
}

function hlCard(id){
  document.querySelectorAll('[data-vid].active').forEach(c=>c.classList.remove('active'));
  document.querySelectorAll(`[data-vid="${id}"]`).forEach(c=>c.classList.add('active'));
}

function setMeta(title,ch,thumb,isLocal){
  G('ptit').textContent=title;G('pch').textContent=ch;
  if(thumb&&!isLocal){G('pimg').src=thumb;G('pimg').style.display='block';G('pph').style.display='none';}
  else{G('pimg').style.display='none';G('pph').style.display='flex';G('pph').innerHTML=isLocal?IC.save:IC.note;}
  G('skFill').style.width='0%';G('skHdl').style.left='0%';G('skBuf').style.width='0%';
  G('pnow').textContent='0:00';G('ptot').textContent='0:00';
  try{_npSync();}catch(e){}
  try{if(_karOpen)_karRefresh();}catch(e){}
}

/* ═══ CONTROLES ══════════════════════════════════════ */
G('bplay').onclick=()=>{if(MA.paused||MA.ended)MA.play().catch(()=>{});else MA.pause();};
G('bprev').onclick=()=>{
  if(NATIVE){try{AndroidBG.previous();}catch(e){}return;}
  if(MODE==='yt'&&Q.length)     playYT((qi-1+Q.length)%Q.length);
  else if(MODE==='local'&&LQ.length) playLocal((lqi-1+LQ.length)%LQ.length);
};
G('bnext').onclick=()=>{
  _skipFeedback();
  if(NATIVE){try{AndroidBG.next();}catch(e){}return;}
  if(MODE==='yt')_playNextYT();
  else if(MODE==='local'&&LQ.length) playLocal((lqi+1)%LQ.length);
};
G('brep').onclick=()=>{RPT=(RPT+1)%3;_applyRepeat();};
function _applyRepeat(){
  const b=G('brep');if(!b)return;
  b.classList.toggle('on',RPT>0);
  b.innerHTML=RPT===2?IC.rep1:IC.rep;
  b.title=RPT===0?TL('Repetir'):(RPT===1?TL('Repetir todo'):TL('Repetir una'));
  if(NATIVE){try{AndroidBG.setRepeat(RPT);}catch(e){}}
}
setTimeout(_applyRepeat,0);
G('bvid').onclick=()=>{
  if(!curTrack){toast('Reproduce algo primero');return;}
  if(MODE==='local'&&curTrack.is_video){openLocalVideo();return;}
  if(MODE==='yt') openYTVideo();
  else toast('No hay video disponible');
};
G('bkar').onclick=()=>openKaraoke();

/* ═══ YOUTUBE IFRAME PLAYER API ═════════════════════
   onYouTubeIframeAPIReady es el callback global que
   invoca la API cuando termina de cargar.
   Creamos el player UNA sola vez y reutilizamos.
═══════════════════════════════════════════════════ */
function onYouTubeIframeAPIReady(){
  _ytReady=true;
  if(_ytPendingOpen){
    const p=_ytPendingOpen;_ytPendingOpen=null;
    _launchYTPlayer(p.vid,p.startSec);
  }
}

function _launchYTPlayer(vid,startSec){
  // Si ya existe un player, carga el nuevo video directamente
  if(_ytPlayer){
    _ytPlayer.loadVideoById({videoId:vid,startSeconds:startSec});
    return;
  }
  // Crea contenedor limpio (la API reemplaza el div con un iframe)
  G('ytifr').innerHTML='<div id="ytplayer"></div>';
  _ytPlayer=new YT.Player('ytplayer',{
    width:'100%',height:'100%',
    videoId:vid,
    playerVars:{autoplay:1,start:Math.floor(startSec),
                rel:0,modestbranding:1,playsinline:1},
    events:{
      onReady:e=>e.target.playVideo(),
      onError: ()=>toast('Error al cargar el video','err'),
    }
  });
}

/* ═══ NOW PLAYING (pantalla completa estilo YT Music) ═ */
let npDrag=false;
function _npSync(){
  if(!curTrack)return;
  G('npTitle').textContent=curTrack.title||'—';
  G('npArtist').textContent=curTrack.channel||curTrack.artist||'';
  let cov=curTrack.thumb||'';
  if(!cov&&curTrack.name)cov='/api/cover/'+encodeURIComponent(curTrack.name);
  const img=G('npImg'),ph=G('npPh');
  if(cov){
    img.classList.remove('ld');img.style.display='';
    img.onload=()=>img.classList.add('ld');
    img.onerror=()=>{img.style.display='none';ph.style.display='flex';};
    img.src=cov;ph.style.display='none';
    G('npBg').style.backgroundImage='url("'+cov.replace(/"/g,'')+'")';
  }else{
    img.style.display='none';ph.style.display='flex';G('npBg').style.backgroundImage='';
  }
  G('npLike').className='np-like'+(G('blike').classList.contains('on')?' on':'');
}
/* ═══ COLA / A CONTINUACIÓN ═════════════════════════ */
function openQueue(){ _ensureRadio(); renderQueue(); G('qpanel').classList.add('show'); }
function closeQueue(){ G('qpanel').classList.remove('show'); }
function _qRow(t,i,now){
  return '<div class="qrow'+(now?' on':'')+'" onclick="qJump('+i+')">'+
    '<img class="qrow-c" src="'+esc(t.thumb||'')+'" loading="lazy" decoding="async" onerror="this.style.visibility=\'hidden\'">'+
    '<div class="qrow-i"><div class="qrow-t">'+esc(t.title||'')+'</div><div class="qrow-a">'+esc(t.channel||'')+'</div></div>'+
    (now?'':'<div class="qrow-x" onclick="event.stopPropagation();qRemove('+i+')">'+IC.x+'</div>')+'</div>';
}
function renderQueue(){
  const box=G('qList');if(!box)return;
  if(MODE!=='yt'||!Q.length){box.innerHTML='<div class="kar-empty">No hay cola.<br>Reproduce algo o activa la radio.</div>';return;}
  let h='';
  if(Q[qi])h+='<div class="q-lbl">Sonando ahora</div>'+_qRow(Q[qi],qi,true);
  const up=[];for(let i=qi+1;i<Q.length;i++)up.push(i);
  if(up.length){h+='<div class="q-lbl">'+TL('Siguientes')+' · '+up.length+'</div>';up.forEach(i=>{h+=_qRow(Q[i],i,false);});}
  if(radioOn){h+='<div class="q-more"><span></span><span></span><span></span></div>';}
  else if(!up.length){h+='<div class="kar-empty">Activa la radio para que no pare.</div>';}
  box.innerHTML=h;
}
function qJump(i){ if(i>=0&&i<Q.length){playYT(i);renderQueue();} }
function qRemove(i){ if(i>qi&&i<Q.length){Q.splice(i,1);renderQueue();} }

function openNP(){
  if(!curTrack){toast('Reproduce algo primero');return;}
  _npSync();
  G('npPlay').innerHTML=MA.paused?IC.play:IC.pause;
  G('np').classList.add('show');
}
function closeNP(){G('np').classList.remove('show');}
function npArtistClick(){const a=G('npArtist').textContent;closeNP();openArtist(a);}
function npVideo(){
  closeNP();
  if(!curTrack){toast('Reproduce algo primero');return;}
  if(MODE==='local'&&curTrack.is_video){openLocalVideo();return;}
  if(MODE==='yt')openYTVideo();
  else toast('No hay video disponible');
}
function npDownload(){
  if(!curTrack||MODE!=='yt'){toast('Nada que descargar aquí','err');return;}
  startDownload(curTrack,null,null);return;
  const t=curTrack;
  fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:t.id})})
    .then(r=>r.json()).then(resp=>{
      toast((resp.mode==='audio'?'Audio':'Video')+': '+(t.title||'').slice(0,30),'dl');
      const poll=setInterval(async()=>{try{
        const s=await(await fetch('/api/dl_status/'+t.id)).json();
        if(s.status==='done'){clearInterval(poll);toast('Guardado','ok');if(!G('panel-files').classList.contains('hidden'))loadFiles();}
        else if(s.status==='error'){clearInterval(poll);toast('Error al descargar','err');}
      }catch(e){clearInterval(poll);}},900);
    }).catch(()=>toast('Error al descargar','err'));
}
(function(){
  const sk=G('npSk');if(!sk)return;
  const paint=f=>{G('npFill').style.width=(f*100)+'%';G('npHdl').style.left=(f*100)+'%';G('npNow').textContent=fmt(f*(MA.duration||0));};
  sk.addEventListener('click',e=>{const f=fracFromEv(e,sk);seekFrac(f);paint(f);});
  const dn=()=>{npDrag=true;sk.classList.add('drag');};
  const mv=e=>{if(npDrag)paint(fracFromEv(e,sk));};
  const up=e=>{if(!npDrag)return;npDrag=false;sk.classList.remove('drag');const ev=e.changedTouches?e.changedTouches[0]:e;seekFrac(fracFromEv(ev,sk));};
  sk.addEventListener('mousedown',dn);document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);
  sk.addEventListener('touchstart',dn,{passive:true});sk.addEventListener('touchmove',mv,{passive:true});sk.addEventListener('touchend',up);
  MA.addEventListener('play',()=>G('npPlay').innerHTML=IC.pause);
  MA.addEventListener('pause',()=>G('npPlay').innerHTML=IC.play);
  MA.addEventListener('progress',()=>{try{const d=MA.duration||0;if(d&&MA.buffered.length)G('npBuf').style.width=(MA.buffered.end(MA.buffered.length-1)/d*100)+'%';}catch(e){}});
})();

function _vClosePanels(){try{closeNP();}catch(e){}try{closeQueue();}catch(e){}try{closeKaraoke();}catch(e){}try{closeCfg();}catch(e){}try{closeCtx();}catch(e){}}
function openYTVideo(){
  if(!curTrack)return;
  const startSec=MA.currentTime||0;
  _vidIsLocal=false;
  MA.muted=true;MA.pause();
  if(_ytPlayer){try{_ytPlayer.stopVideo();}catch(e){}}
  G('ytifr').style.display='none';
  const lv=G('localvid');
  lv.style.display='block';
  G('vidLoad').classList.add('show');
  lv.onwaiting=()=>G('vidLoad').classList.add('show');
  lv.onplaying=()=>G('vidLoad').classList.remove('show');
  lv.src='/api/vstream/'+encodeURIComponent(curTrack.id);
  lv.volume=parseInt(G('vol').value)/100;
  lv.muted=false;
  lv.addEventListener('loadedmetadata',function onM(){
    lv.removeEventListener('loadedmetadata',onM);
    try{lv.currentTime=startSec;}catch(e){}
    lv.play().catch(()=>{});
  },{once:true});
  lv.onerror=()=>{G('vidLoad').classList.remove('show');toast('No se pudo cargar el video','err');};
  _vClosePanels();G('vidpanel').classList.add('show');_vBind();G('vcTitle').textContent=(curTrack.title||'Video');G('vcMute').innerHTML=G('localvid').muted?IC.mute:IC.vol;_vShow();_enterVidFS();
}

function openLocalVideo(){
  if(!curTrack)return;
  const startSec=MA.currentTime||0;
  _vidIsLocal=true;
  MA.muted=true;
  MA.pause();
  // Destruye player YT si existe
  if(_ytPlayer){try{_ytPlayer.stopVideo();}catch(e){}}
  G('ytifr').style.display='none';
  const lv=G('localvid');
  lv.style.display='block';
  G('vidLoad').classList.add('show');
  lv.onwaiting=()=>G('vidLoad').classList.add('show');
  lv.onplaying=()=>G('vidLoad').classList.remove('show');
  lv.src='/sandbox/'+encodeURIComponent(curTrack.name||curTrack.id.replace('local:',''));
  lv.currentTime=startSec;
  lv.volume=parseInt(G('vol').value)/100;
  lv.play().catch(()=>{});
  _vClosePanels();G('vidpanel').classList.add('show');_vBind();G('vcTitle').textContent=(curTrack.title||'Video');G('vcMute').innerHTML=G('localvid').muted?IC.mute:IC.vol;_vShow();_enterVidFS();
}

/* ═══ KARAOKE PRO (sincronía fina por rAF + relleno) ═══ */
let _karLines=[];let _karActive=-1;let _karOpen=false;let _karRAF=0;let _karNodes=null;

function _parseLRC(lrc){
  const out=[];
  (lrc||'').split(/\r?\n/).forEach(line=>{
    const tags=[...line.matchAll(/\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]/g)];
    if(!tags.length)return;
    const text=line.replace(/\[[^\]]*\]/g,'').trim();
    tags.forEach(m=>{
      const cs=m[3]?parseInt((m[3]+'000').slice(0,3),10):0;
      out.push({t:(+m[1])*60+(+m[2])+cs/1000,text});
    });
  });
  return out.sort((a,b)=>a.t-b.t);
}

let _karReq=0;
async function openKaraoke(){
  if(!curTrack){toast('Reproduce algo primero');return;}
  _karOpen=true;
  G('karpanel').classList.add('show');
  _karLoad();
  _startKarLoop();
}
function _karRefresh(){ if(_karOpen)_karLoad(); }
async function _karLoad(){
  _karLines=[];_karActive=-1;_karNodes=null;
  const tk=curTrack||{};
  G('karTi').textContent=tk.title||'Karaoke';
  G('karBody').innerHTML='<div class="kar-empty">'+TL('Buscando letra…')+'</div>';
  const title=tk.title||'';
  const artist=tk.channel||tk.artist||'';
  const dur=Math.round(MA.duration||tk.dur_sec||0);
  const rid=++_karReq;                 // token anti-respuesta-vieja
  try{
    const d=await(await fetch(`/api/lyrics?title=${encodeURIComponent(title)}&artist=${encodeURIComponent(artist)}&dur=${dur}`)).json();
    if(!_karOpen||rid!==_karReq)return;   // cambió de canción o se cerró: ignora
    let lines=[];
    if(d.has_synced)lines=_parseLRC(d.synced);
    _karLines=lines;_karActive=-1;
    if(lines.length){
      G('karBody').innerHTML=lines.map((l,i)=>{
        const txt=(l.text||'').trim();
        const inner=txt? txt.split(/\s+/).map(w=>`<span class="kw">${esc(w)}</span>`).join(' ') : '♪';
        return `<div class="kar-line clickable" data-i="${i}" onclick="karSeek(${i})">${inner}</div>`;
      }).join('');
      _karNodes=G('karBody').querySelectorAll('.kar-line');
    }else if(d.plain){
      G('karBody').innerHTML='<div class="kar-plain">'+esc(d.plain)+'</div>';
    }else{
      G('karBody').innerHTML='<div class="kar-empty">'+TL('No hay karaoke para esta canción.')+'</div>';
    }
  }catch(e){
    if(_karOpen&&rid===_karReq)G('karBody').innerHTML='<div class="kar-empty">'+TL('No se pudo cargar la letra.')+'</div>';
  }
}

function _startKarLoop(){
  cancelAnimationFrame(_karRAF);
  const loop=()=>{ if(!_karOpen)return; _karTick(); _karRAF=requestAnimationFrame(loop); };
  _karRAF=requestAnimationFrame(loop);
}

function _karTick(){
  if(!_karLines.length||!_karNodes)return;
  const t=MA.currentTime;
  let idx=-1;
  for(let i=0;i<_karLines.length;i++){ if(_karLines[i].t<=t) idx=i; else break; }
  if(idx!==_karActive){
    _karActive=idx;
    _karNodes.forEach((n,i)=>{
      const on=(i===idx);
      n.classList.toggle('on',on);
      n.classList.toggle('passed',idx>=0&&i<idx);
      const ws=n.querySelectorAll('.kw');
      if(i<idx){ ws.forEach(w=>{w.classList.add('sung');w.classList.remove('cur');}); }      // pasada: todo apagado al instante
      else { ws.forEach(w=>{w.classList.remove('sung');w.classList.remove('cur');}); }       // futura/activa: limpio
    });
    const cur=_karNodes[idx];
    if(cur){
      const body=G('karBody');
      let top=cur.offsetTop-body.clientHeight/2+cur.clientHeight/2;
      body.scrollTo({top:top<0?0:top,behavior:'smooth'});
    }
  }
  // Barrido palabra por palabra en la linea activa (fluye renglon de arriba y luego el de abajo)
  if(idx>=0){
    const cur=_karLines[idx], nxt=_karLines[idx+1];
    const end=nxt?nxt.t:cur.t+4;
    let p=(t-cur.t+0.12)/Math.max(.45,(end-cur.t));   // sigue el tiempo real de la cancion
    p=p<0?0:p>1?1:p;
    const node=_karNodes[idx];
    if(node){
      const ws=node.querySelectorAll('.kw');
      const k=Math.floor(p*ws.length+0.18);     // palabras ya cantadas (se quedan coloreadas)
      ws.forEach((w,wi)=>w.classList.toggle('sung',wi<k));
    }
  }
}

function karSeek(i){
  const l=_karLines[i];if(!l)return;
  MA.currentTime=l.t+0.03;
  if(MA.paused)MA.play().catch(()=>{});
  _karActive=-2;
}

function closeKaraoke(){
  _karOpen=false;
  cancelAnimationFrame(_karRAF);
  G('karpanel').classList.remove('show');
}

function _tryFS(el){
  if(!el)return false;
  const fn=el.requestFullscreen||el.webkitRequestFullscreen||el.mozRequestFullScreen||el.msRequestFullscreen;
  if(fn){try{const r=fn.call(el);if(r&&r.catch)r.catch(()=>{});return true;}catch(e){}}
  return false;
}
function _capStatusBar(hide){
  try{const C=window.Capacitor;if(C&&C.Plugins&&C.Plugins.StatusBar){hide?C.Plugins.StatusBar.hide():C.Plugins.StatusBar.show();}}catch(e){}
}
let _vFsOn=false;
function _lockLandscape(){
  try{if(screen.orientation&&screen.orientation.lock)screen.orientation.lock("landscape").catch(()=>{});}catch(e){}
  try{const C=window.Capacitor;if(C&&C.Plugins&&C.Plugins.ScreenOrientation)C.Plugins.ScreenOrientation.lock({orientation:"landscape"});}catch(e){}
}
function _unlockOrient(){
  try{const C=window.Capacitor;if(C&&C.Plugins&&C.Plugins.ScreenOrientation){const SO=C.Plugins.ScreenOrientation;const r=SO.lock({orientation:"portrait"});if(r&&r.then){r.then(()=>setTimeout(()=>{try{SO.unlock();}catch(e){}},600)).catch(()=>{try{SO.unlock();}catch(e){}});}else{setTimeout(()=>{try{SO.unlock();}catch(e){}},600);}}}catch(e){}
  try{if(screen.orientation&&screen.orientation.unlock)screen.orientation.unlock();}catch(e){}
}
function _applyRot(){const p=G("vidpanel");if(p)p.classList.remove("rot");}
function _isNative(){try{return !!(window.Capacitor&&((window.Capacitor.isNativePlatform&&window.Capacitor.isNativePlatform())||(window.Capacitor.Plugins&&window.Capacitor.Plugins.ScreenOrientation)));}catch(e){return false;}}
let _vClosing=false;
function _wakeUI(){
  // Fuerza al WebView de Android a re-layout: recupera el touch al salir de fullscreen
  try{window.dispatchEvent(new Event("resize"));}catch(e){}
  try{const b=document.body,prev=b.style.minHeight;b.style.minHeight="calc(100% + 0.4px)";requestAnimationFrame(()=>{b.style.minHeight=prev||"";});}catch(e){}
  try{const el=document.querySelector(".panel:not(.hidden)")||G("main");if(el){const t=el.scrollTop;el.scrollTop=t+1;el.scrollTop=t;}}catch(e){}
  try{if(document.activeElement&&document.activeElement.blur)document.activeElement.blur();}catch(e){}
}
// ══ PANTALLA COMPLETA — tecnica estandar (web.dev / videojs) ══════════
// Fullscreen sobre el CONTENEDOR (no el <video>) para CONSERVAR los controles propios.
// El lock de orientacion SOLO funciona con fullscreen ya activo => por eso va despues.
function _enterVidFS(){
  _vFsOn=true;_capStatusBar(true);
  const p=G("vidpanel"),v=G("localvid");
  const req=p.requestFullscreen||p.webkitRequestFullscreen||p.msRequestFullscreen;
  if(!req){ _vFsFallback(v); return; }
  try{
    let r;try{r=req.call(p,{navigationUI:"hide"});}catch(_){r=req.call(p);}
    if(r&&typeof r.then==="function") r.then(()=>_lockLandscape()).catch(()=>_vFsFallback(v));
    else _lockLandscape();
  }catch(e){ _vFsFallback(v); }
}
function _vFsFallback(v){
  // El contenedor no pudo ir a pantalla completa (ej. iOS o bloqueado):
  // fullscreen NATIVO del <video> (en movil siempre funciona).
  try{
    if(v&&v.webkitEnterFullscreen){v.webkitEnterFullscreen();}
    else if(v&&v.requestFullscreen){const r=v.requestFullscreen();if(r&&r.catch)r.catch(()=>{});}
  }catch(e){}
  _lockLandscape();
}
function _exitVidFS(){
  _vFsOn=false;_capStatusBar(false);_unlockOrient();
  const p=G("vidpanel");if(p)p.classList.remove("rot");
  try{if(document.fullscreenElement||document.webkitFullscreenElement){
    const ex=document.exitFullscreen||document.webkitExitFullscreen;
    if(ex){const r=ex.call(document);if(r&&r.catch)r.catch(()=>{});}
  }}catch(e){}
  _wakeUI();
}
function vFull(){
  if(document.fullscreenElement||document.webkitFullscreenElement)_exitVidFS();
  else _enterVidFS();
  _vShow();
}
function vZoom(){const v=G("localvid");const on=v.classList.toggle("fill");try{G("vcZoom").classList.toggle("on",on);}catch(e){}_vShow();}
function _vSyncFullBtn(){try{const b=G("vcFull");if(b)b.classList.toggle("on",!!(document.fullscreenElement||document.webkitFullscreenElement));}catch(e){}}
// Girar el telefono a HORIZONTAL estando en video => pantalla completa; a VERTICAL => salir
function _onVidOrient(){
  if(!G("vidpanel").classList.contains("show"))return;
  const land=window.matchMedia("(orientation: landscape)").matches;
  const fs=!!(document.fullscreenElement||document.webkitFullscreenElement);
  if(land&&!fs)_enterVidFS();
  else if(!land&&fs)_exitVidFS();
}
// Sincroniza al cambiar fullscreen (incluye el boton "atras" del sistema)
function _onFsChange(){
  _vSyncFullBtn();
  if(!(document.fullscreenElement||document.webkitFullscreenElement)){
    _vFsOn=false;_capStatusBar(false);
    try{if(screen.orientation&&screen.orientation.unlock)screen.orientation.unlock();}catch(e){}
    _wakeUI();
  }
}
document.addEventListener("fullscreenchange",_onFsChange);
document.addEventListener("webkitfullscreenchange",_onFsChange);
window.addEventListener("resize",()=>{if(_vFsOn)_applyRot();});
function dlCurrent(){
  if(!curTrack||MODE!=="yt"){toast("Nada que descargar aquí","err");return;}
  startDownload(curTrack,null,null);return;
  const t=curTrack, btn=G("vidDl");
  btn.innerHTML=IC.load; btn.disabled=true;
  fetch("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:t.id})})
    .then(r=>r.json()).then(resp=>{
      const ml=resp.mode==="audio"?"Audio":"Video";
      toast(ml+": "+(t.title||"").slice(0,32),"dl");
      const poll=setInterval(async()=>{
        try{
          const s=await(await fetch("/api/dl_status/"+t.id)).json();
          if(s.status==="done"){clearInterval(poll);btn.innerHTML=IC.check;btn.disabled=false;toast("Guardado","ok");if(!G("panel-files").classList.contains("hidden"))loadFiles();}
          else if(s.status==="error"){clearInterval(poll);btn.innerHTML='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg>';btn.disabled=false;toast("Error al descargar","err");}
        }catch(e){clearInterval(poll);btn.disabled=false;}
      },900);
    }).catch(()=>{btn.disabled=false;btn.innerHTML='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg>';toast("Error al descargar","err");});
}

/* ═══ REPRODUCTOR DE VIDEO CUSTOM ═══════════════════ */
let _vUI=0;let _vSk=false;let _vBound=false;
function _vFmt(s){s=Math.max(0,s|0);return (s/60|0)+':'+('0'+(s%60)).slice(-2);}
function _vBind(){
  if(_vBound)return;_vBound=true;
  const lv=G('localvid'),sk=G('vcSk');
  lv.addEventListener('play',()=>{G('vcPlay').innerHTML=IC.pause;_vAutoHide();});
  lv.addEventListener('pause',()=>{G('vcPlay').innerHTML=IC.play;_vShow(true);});
  lv.addEventListener('ended',()=>{G('vcPlay').innerHTML=IC.play;_videoNext();});
  lv.addEventListener('timeupdate',()=>{
    if(_vSk)return;
    const d=lv.duration||0,c=lv.currentTime||0;
    G('vcNow').textContent=_vFmt(c);G('vcDur').textContent=_vFmt(d);
    const p=d?c/d*100:0;G('vcFill').style.width=p+'%';G('vcHdl').style.left=p+'%';
  });
  lv.addEventListener('progress',()=>{try{const d=lv.duration||0;if(d&&lv.buffered.length)G('vcBuf').style.width=(lv.buffered.end(lv.buffered.length-1)/d*100)+'%';}catch(e){}});
  G('vidwrap').addEventListener('click',e=>{
    if(e.target.closest('button')||e.target.closest('.vc-sk'))return;
    G('vctrl').classList.contains('show')?_vHide():_vShow();
  });
  const frac=e=>{
    const r=sk.getBoundingClientRect();
    const rot=G("vidpanel").classList.contains("rot");
    const cx=(e.touches&&e.touches[0]?e.touches[0].clientX:(e.changedTouches&&e.changedTouches[0]?e.changedTouches[0].clientX:e.clientX));
    const cy=(e.touches&&e.touches[0]?e.touches[0].clientY:(e.changedTouches&&e.changedTouches[0]?e.changedTouches[0].clientY:e.clientY));
    const f=rot?(cy-r.top)/r.height:(cx-r.left)/r.width;
    return Math.min(1,Math.max(0,f));
  };
  const paint=f=>{G('vcFill').style.width=(f*100)+'%';G('vcHdl').style.left=(f*100)+'%';G('vcNow').textContent=_vFmt(f*(lv.duration||0));};
  const seekTo=f=>{const d=lv.duration||0;if(d){lv.currentTime=f*d;paint(f);}};
  sk.addEventListener('click',e=>seekTo(frac(e)));
  const down=()=>{_vSk=true;sk.classList.add('drag');_vShow(true);};
  const move=e=>{if(_vSk)paint(frac(e));};
  const up=e=>{if(!_vSk)return;_vSk=false;sk.classList.remove('drag');seekTo(frac(e));_vAutoHide();};
  sk.addEventListener('mousedown',down);document.addEventListener('mousemove',move);document.addEventListener('mouseup',up);
  sk.addEventListener('touchstart',down,{passive:true});sk.addEventListener('touchmove',move,{passive:true});sk.addEventListener('touchend',up);
  G('vctrl').addEventListener('mousemove',()=>_vShow());
}
function _vShow(stay){G('vctrl').classList.add('show');clearTimeout(_vUI);if(!stay&&!G('localvid').paused)_vAutoHide();}
function _vAutoHide(){clearTimeout(_vUI);_vUI=setTimeout(()=>{if(!G('localvid').paused&&!_vSk)_vHide();},3200);}
function _vHide(){G('vctrl').classList.remove('show');}
function vTogglePlay(){const lv=G('localvid');(lv.paused||lv.ended)?lv.play().catch(()=>{}):lv.pause();_vShow();}
function vSkip(n){const lv=G('localvid');lv.currentTime=Math.max(0,Math.min((lv.duration||1e9),(lv.currentTime||0)+n));_vShow();}
function vToggleMute(){const lv=G('localvid');lv.muted=!lv.muted;G('vcMute').innerHTML=lv.muted?IC.mute:IC.vol;_vShow();}


async function _videoNext(){
  if(_vidIsLocal){
    const vidxs=LQ.map((f,i)=>i).filter(i=>LQ[i].is_video);
    if(vidxs.length<=1){closeVideo();return;}
    const pos=vidxs.indexOf(lqi);const nx=vidxs[(pos+1)%vidxs.length];
    lqi=nx;const f=LQ[nx];curTrack={id:'local:'+f.name,...f};
    setMeta(f.title||f.name.replace(/\.[^.]+$/,''),f.artist||'Local',null,true);
    const lv=G('localvid');G('vidLoad').classList.add('show');
    lv.src='/sandbox/'+encodeURIComponent(f.name);
    lv.addEventListener('loadedmetadata',function onM(){lv.removeEventListener('loadedmetadata',onM);lv.play().catch(()=>{});},{once:true});
    G('vcTitle').textContent=f.title||f.name;return;
  }
  if(qi+1>=Q.length&&radioOn&&curTrack){await _ensureRadio();}
  if(qi+1>=Q.length){closeVideo();return;}
  if(curTrack)_feedback(curTrack,'complete');
  qi=qi+1;const t=Q[qi];curTrack=t;
  setMeta(t.title,t.channel,t.thumb,false);
  setLikeBtn(t.id,'yt');
  try{MA.src='/api/stream/'+t.id;MA.muted=true;MA.load();}catch(e){}
  fetch('/api/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(t)}).catch(()=>{});
  _ensureRadio();
  const lv=G('localvid');G('vidLoad').classList.add('show');lv.muted=false;
  lv.src='/api/vstream/'+encodeURIComponent(t.id);
  lv.addEventListener('loadedmetadata',function onM(){lv.removeEventListener('loadedmetadata',onM);lv.play().catch(()=>{});},{once:true});
  G('vcTitle').textContent=t.title||'Video';
  if(G('qpanel')&&G('qpanel').classList.contains('show'))renderQueue();
}

function closeVideo(){
  if(_vClosing)return;_vClosing=true;
  try{_vSk=false;var _sk=G('vcSk');if(_sk)_sk.classList.remove('drag');}catch(e){}
  _exitVidFS();try{_closePicker();}catch(e){}G('vidLoad').classList.remove('show');G('vidpanel').classList.remove('show');
  _wakeUI();setTimeout(()=>{_vClosing=false;},450);setTimeout(_wakeUI,350);
  const lv=G('localvid');

  // ── Tiempo exacto donde quedó el video ─────────────
  let resumeSec=lv.currentTime||0;

  // Libera el <video> CORRECTAMENTE. Poner src='' dispara un evento 'error'
  // que mostraba "No se pudo cargar el video" y dejaba el decoder colgado (= freeze).
  // Lo correcto es quitar el atributo y hacer load().
  lv.onerror=null;lv.onwaiting=null;lv.onplaying=null;
  try{lv.pause();}catch(e){}
  try{lv.removeAttribute('src');lv.load();}catch(e){}
  lv.style.display='none';

  // Reanuda el audio SIN bloquear. Antes esperaba eventos 'seeked'/'canplay'
  // que en streaming desde la Pi a veces NUNCA llegan => el audio quedaba colgado
  // esperando y la UI parecia congelada (se "destrababa" al picar play/pause,
  // porque eso abortaba la espera). Ahora: buscar segundo y reproducir, sin esperas.
  MA.muted=false;
  // Reproducir PRIMERO (dentro del gesto de cerrar) y buscar despues: asi el navegador
  // no bloquea el play por falta de gesto. Seguro: quita el spinner pase lo que pase.
  const _p=MA.play();if(_p&&_p.catch)_p.catch(()=>{});
  try{if(resumeSec>0&&isFinite(resumeSec)&&Math.abs((MA.currentTime||0)-resumeSec)>0.6)MA.currentTime=resumeSec;}catch(e){}
  G('bufRing').classList.remove('show');
  setTimeout(()=>{try{G('bufRing').classList.remove('show');}catch(e){}},2500);
  _wakeUI();
}

/* ═══ LIKE ════════════════════════════════════════════ */
async function setLikeBtn(id,type){
  try{
    const d=await(await fetch('/api/likes')).json();
    const liked=type==='yt'
      ?d.yt.some(t=>t.id===id)
      :d.local.some(([n])=>n===id);
    G('blike').innerHTML=IC.heart;
    G('blike').classList.toggle('on',liked);
  }catch(e){}
}
G('blike').onclick=async()=>{
  if(!curTrack)return;
  if(MODE==='yt'){
    const r=await(await fetch('/api/like/yt',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(curTrack)})).json();
    G('blike').innerHTML=IC.heart;G('blike').classList.toggle('on',r.liked);
    if(r.liked)_feedback(curTrack,'like');
    toast(r.liked?'Me gusta':'Eliminado',r.liked?'like':'unlike');
    if(!G('panel-likes').classList.contains('hidden'))loadLikes();
  }else{
    const name=curTrack.name||curTrack.id.replace('local:','');
    const meta={title:curTrack.title,artist:curTrack.artist||'',dur:curTrack.dur||''};
    const r=await(await fetch('/api/like/local',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,meta})})).json();
    G('blike').innerHTML=IC.heart;G('blike').classList.toggle('on',r.liked);
    toast(r.liked?'Me gusta':'Eliminado',r.liked?'like':'unlike');
    if(!G('panel-likes').classList.contains('hidden'))loadLikes();
    if(!G('panel-files').classList.contains('hidden'))loadFiles();
  }
};

/* ═══ PLAYLIST DROPDOWN ══════════════════════════════ */
/* ═══ MENÚ CONTEXTUAL (clic derecho / mantener presionado) ═══ */
let _ctx=null;
function _lpAttach(el, ctx){
  if(!el)return;
  el.addEventListener('contextmenu',e=>{e.preventDefault();openCtx(e.clientX,e.clientY,ctx);});
  let tm=null,moved=false,sx=0,sy=0,fired=false;
  el.addEventListener('click',e=>{ if(fired){e.stopPropagation();e.preventDefault();fired=false;} },true);
  el.addEventListener('touchstart',e=>{
    moved=false;fired=false;const t=e.touches[0];sx=t.clientX;sy=t.clientY;
    clearTimeout(tm);
    tm=setTimeout(()=>{ if(!moved){ fired=true; try{navigator.vibrate&&navigator.vibrate(14);}catch(_){ } openCtx(sx,sy,ctx);} },470);
  },{passive:true});
  el.addEventListener('touchmove',e=>{const t=e.touches[0];if(Math.abs(t.clientX-sx)>12||Math.abs(t.clientY-sy)>12){moved=true;clearTimeout(tm);}},{passive:true});
  el.addEventListener('touchend',()=>clearTimeout(tm));
  el.addEventListener('touchcancel',()=>clearTimeout(tm));
}
function _ctxItem(icon,label,fn,danger){return '<div class="ctx-i'+(danger?' danger':'')+'" onclick="'+fn+'">'+icon+'<span>'+esc(label)+'</span></div>';}
let _ctxXY={x:0,y:0};
function openCtx(x,y,ctx){
  _ctx=ctx;_ctxXY={x,y};const t=ctx.track,local=ctx.isLocal;
  let h=_ctxItem(IC.play,'Reproducir','ctxPlay()');
  h+=_ctxItem(IC_QNEXT,'Reproducir a continuación','ctxNext()');
  h+=_ctxItem(IC.list,'Añadir a playlist','ctxPlaylist()');
  h+=_ctxItem(IC.heart,'Me gusta','ctxLike()');
  if(!local)h+=_ctxItem(IC.download,'Descargar','ctxDl()');
  const art=t.channel||t.artist||'';
  if(art&&art!=='Local')h+=_ctxItem(IC_ARTIST,'Ir al artista','ctxArtist()');
  if(local)h+=_ctxItem(IC.trash,'Eliminar','ctxDelete()',true);
  const m=G('ctxmenu');m.innerHTML=h;
  m.style.visibility='hidden';m.style.left='0px';m.style.top='0px';m.classList.add('show');
  G('ctxveil').classList.add('show');
  const mw=m.offsetWidth||230,mh=m.offsetHeight||280;
  let px=Math.min(x,window.innerWidth-mw-10),py=Math.min(y,window.innerHeight-mh-10);
  px=Math.max(10,px);py=Math.max(10,py);
  m.style.left=px+'px';m.style.top=py+'px';
  m.style.setProperty('--ox',((x-px)/mw*100).toFixed(0)+'%');
  m.style.setProperty('--oy',((y-py)/mh*100).toFixed(0)+'%');
  m.style.visibility='visible';
}
function _closePicker(){try{var d=G('plDrop');if(d)d.classList.remove('show');var v=G('ctxveil');if(v)v.classList.remove('show');var m=G('ctxmenu');if(m)m.classList.remove('show');}catch(e){}}
function closeCtx(){G('ctxmenu').classList.remove('show');G('ctxveil').classList.remove('show');var _pd=G('plDrop');if(_pd)_pd.classList.remove('show');}
function _lTrack(t){return {id:'local:'+t.name,name:t.name,title:t.title||t.name,channel:t.artist||'Local',thumb:'/api/cover/'+encodeURIComponent(t.name),dur:t.dur||'',is_video:t.is_video,local:true};}
function ctxPlay(){const c=_ctx;closeCtx();if(!c)return; if(c.isLocal)playLocal(c.idx); else loadQ(c.tracks,c.idx,c.finite);}
function ctxNext(){const c=_ctx;closeCtx();if(!c||!c.track)return;
  const tt=c.isLocal?_lTrack(c.track):c.track;
  if(!Q.length){Q=[tt];qi=-1;radioOn=!c.isLocal;playYT(0);return;}
  Q.splice(qi+1,0,tt);G('bnext').disabled=false;toast('Se reproducirá a continuación','ok');
  if(G('qpanel')&&G('qpanel').classList.contains('show'))renderQueue();
}
function ctxPlaylist(){const c=_ctx;closeCtx();if(!c)return; plDropTrack=c.isLocal?_lTrack(c.track):c.track; _plPickerAt(_ctxXY.x,_ctxXY.y);}
function ctxLike(){const c=_ctx;closeCtx();if(!c)return; _likeTrack(c.track,c.isLocal);}
function ctxDl(){const c=_ctx;closeCtx();if(!c||c.isLocal)return; startDownload(c.track,null,null);}
function ctxArtist(){const c=_ctx;closeCtx();if(!c)return; openArtist(c.track.channel||c.track.artist||'');}
function ctxDelete(){const c=_ctx;closeCtx();if(!c||!c.isLocal)return;
  const nm=c.track.title||c.track.name;
  if(!confirm(TL('¿Eliminar')+' "'+nm+'"? '+TL('No se puede deshacer.')))return;
  fetch('/api/files/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:c.track.name})})
    .then(r=>r.json()).then(()=>{toast('Eliminado','trash');if(!G('panel-files').classList.contains('hidden'))loadFiles();}).catch(()=>toast('No se pudo eliminar','err'));
}
async function _likeTrack(t,isLocal){
  try{
    if(isLocal){
      const name=t.name||String(t.id).replace('local:','');
      const meta={title:t.title||'',artist:t.artist||t.channel||'',dur:t.dur||''};
      const r=await(await fetch('/api/like/local',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,meta})})).json();
      toast(r.liked?'Me gusta':'Eliminado',r.liked?'like':'unlike');
      if(!G('panel-files').classList.contains('hidden'))loadFiles();
      if(!G('panel-likes').classList.contains('hidden'))loadLikes();
    }else{
      const r=await(await fetch('/api/like/yt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(t)})).json();
      if(r.liked)_feedback(t,'like');
      toast(r.liked?'Me gusta':'Eliminado',r.liked?'like':'unlike');
      if(curTrack&&curTrack.id===t.id)G('blike').classList.toggle('on',r.liked);
      if(!G('panel-likes').classList.contains('hidden'))loadLikes();
    }
  }catch(e){}
}
function _plPickerAt(x,y){
  (async()=>{
    const pls=await(await fetch('/api/playlists')).json();
    const drop=G('plDrop');
    let h='<div class="pl-di new" onclick="createPlAndAdd()">'+IC.plus+' Nueva playlist</div>';
    pls.forEach(pl=>{var has=plDropTrack&&(pl.tracks||[]).some(x=>x&&x.id===plDropTrack.id);if(has){h+='<div class="pl-di has"><span>'+esc(pl.name)+'</span>'+IC.check+'</div>';}else{h+='<div class="pl-di" onclick="addToPl(\''+esc(pl.id)+'\',\''+esc(pl.name)+'\')">'+esc(pl.name)+' <span style="color:var(--t2)">('+pl.tracks.length+')</span></div>';}});
    drop.innerHTML=h;
    drop.style.visibility='hidden';drop.classList.add('show');G('ctxveil').classList.add('show');
    const dw=drop.offsetWidth||210,dh=drop.offsetHeight||240;
    drop.style.left=Math.max(10,Math.min(x,window.innerWidth-dw-10))+'px';
    drop.style.top=Math.max(10,Math.min(y,window.innerHeight-dh-10))+'px';
    drop.style.visibility='visible';
  })();
}

async function showPlDrop(track,anchor,e){
  e?.stopPropagation();plDropTrack=track;
  const pls=await(await fetch('/api/playlists')).json();
  const drop=G('plDrop');
  let h=`<div class="pl-di new" onclick="createPlAndAdd()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg> Nueva playlist</div>`;
  pls.forEach(pl=>{
    const has=plDropTrack&&(pl.tracks||[]).some(x=>x&&x.id===plDropTrack.id);
    if(has){h+=`<div class="pl-di has"><span>${esc(pl.name)}</span>${IC.check}</div>`;}
    else{h+=`<div class="pl-di" onclick="addToPl('${esc(pl.id)}','${esc(pl.name)}')">${esc(pl.name)} <span style="color:var(--t2)">(${pl.tracks.length})</span></div>`;}
  });
  drop.innerHTML=h;
  const r=anchor.getBoundingClientRect();
  drop.style.top=(r.bottom+4)+'px';
  drop.style.left=Math.min(r.left,window.innerWidth-200)+'px';
  drop.classList.add('show');
}
async function addToPl(plid,plname){
  _closePicker();
  if(!plDropTrack)return;
  await fetch(`/api/playlists/${plid}/tracks`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(plDropTrack)});
  toast(TL('Añadido a')+' "'+plname+'"','ok');
  if(!G('panel-pl').classList.contains('hidden'))loadPlaylists(activePlID||null);
}
async function createPlAndAdd(){
  _closePicker();
  const name=await promptModal('Nueva playlist','Crear');
  if(!name)return;
  const r=await(await fetch('/api/playlists',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name})})).json();
  if(r.ok&&plDropTrack){
    await fetch(`/api/playlists/${r.playlist.id}/tracks`,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(plDropTrack)});
    toast('"'+name+'" '+TL('creada'),'ok');
    if(!G('panel-pl').classList.contains('hidden'))loadPlaylists();
  }
}
document.addEventListener('click',e=>{
  if(!G('plDrop').contains(e.target))G('plDrop').classList.remove('show');
});

/* ═══ INPUT MODAL ════════════════════════════════════ */
let _mRes=null;
function promptModal(title,ok){
  return new Promise(res=>{
    _mRes=res;G('inpTitle').textContent=TL(title);G('inpOk').textContent=TL(ok||'OK');
    G('inpF').value='';G('inpM').classList.add('show');
    setTimeout(()=>G('inpF').focus(),80);
  });
}
G('inpOk').onclick=()=>{
  const v=G('inpF').value.trim();G('inpM').classList.remove('show');_mRes?.(v||null);
};
G('inpNo').onclick=()=>{G('inpM').classList.remove('show');_mRes?.(null);};
G('inpF').onkeydown=e=>{if(e.key==='Enter')G('inpOk').click();if(e.key==='Escape')G('inpNo').click();};

/* ═══ DASHBOARD ══════════════════════════════════════ */
async function showDash(){
  switchTab('home');
  const el=G('panel-home');
  el.innerHTML='<div class="swrap"><div class="spin"></div></div>';
  try{
    const d=await(await fetch('/api/dashboard')).json();
    renderDash(d,el);
  }catch(e){el.innerHTML='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10l9-7 9 7"/><path d="M5 9v11h14V9"/><path d="M10 20v-6h4v6"/></svg></div><p>Error al cargar</p></div>';}
}

const _IC_MIX='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="13.5" r="3.2"/><path d="M8.5 8a5 5 0 0 1 7 0M6 5.5a8.5 8.5 0 0 1 12 0"/></svg>';
const _IC_CLOCK='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>';
const _IC_FLAME='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2s4.5 3.3 4.5 8.5a4.5 4.5 0 0 1-9 0c0-1.7.8-3 1.5-3.9C9.8 8.6 11 7.5 12 2z"/></svg>';
const _IC_PL='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h13M3 12h13M3 18h9"/><circle cx="19" cy="16" r="2"/><path d="M21 16V8"/></svg>';
const _IC_NOTE='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';
const _IC_PLAYF='<svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5.3c0-.8.86-1.3 1.54-.88l11 6.7c.65.4.65 1.36 0 1.76l-11 6.7A1.03 1.03 0 0 1 7 18.7z"/></svg>';

function mkShelfCard(t){
  return `<div class="shcard" data-vid="${esc(t.id)}">
    <div class="sh-art">
      <img src="${esc(t.thumb||'')}" loading="lazy" decoding="async" onload="this.classList.add('ld')" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
      <div class="sh-ph">${_IC_NOTE}</div>
      <button class="sh-play">${_IC_PLAYF}</button>
    </div>
    <div class="sh-ti">${esc(t.title)}</div>
    <div class="sh-ar">${esc(t.channel||'')}</div>
  </div>`;
}
function bindShelf(container,tracks){
  if(!container)return;
  tracks.forEach((t,i)=>{
    container.querySelectorAll(`.shcard[data-vid="${t.id}"]`).forEach(c=>{
      c.onclick=()=>loadQ(tracks,i,false);
      _lpAttach(c,{track:t,isLocal:false,tracks,idx:i,finite:false});
    });
  });
}
async function loadPlShelf(){
  try{
    const pls=await(await fetch('/api/playlists')).json();
    const wrap=G('pl-shelf-wrap');if(!wrap)return;
    if(!pls||!pls.length){wrap.innerHTML='';return;}
    let h=`<div class="sch"><h2>${_IC_PL} ${TL('Playlists')}</h2></div><div class="shelf">`;
    pls.forEach(pl=>{
      const t0=(pl.tracks&&pl.tracks[0])||null;
      const art=t0&&t0.thumb
        ? `<img src="${esc(t0.thumb)}" loading="lazy" onload="this.classList.add('ld')" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="sh-ph">${_IC_PL}</div>`
        : `<div class="sh-ph" style="display:flex">${_IC_PL}</div>`;
      h+=`<div class="plshc" onclick="switchTab('pl');openPl('${esc(pl.id)}')">
        <div class="sh-art plart">${art}</div>
        <div class="sh-ti">${esc(pl.name)}</div>
        <div class="sh-ar">${pl.tracks.length} ${TL('Canciones').toLowerCase()}</div>
      </div>`;
    });
    h+=`</div>`;wrap.innerHTML=h;
  }catch(e){}
}
function renderDash(d,el){
  if(!d.has_history){
    el.innerHTML='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14v-2a8 8 0 0 1 16 0v2"/><rect x="3" y="14" width="4.5" height="6" rx="1.2"/><rect x="16.5" y="14" width="4.5" height="6" rx="1.2"/></svg></div><p>Busca tu primera canción<br>Tu historial aparecerá aquí</p></div>';
    G('pl-shelf-root')&&(G('pl-shelf-root').innerHTML='');return;
  }
  let h='';
  // 1) Mas escuchados (lo que mas suena, acceso rapido)
  if(d.top?.length){
    h+=`<div class="sch"><h2>${_IC_FLAME} Más escuchados</h2><small>${d.top.length}</small></div><div class="grid">${d.top.map(t=>mkCard(t)).join('')}</div>`;
  }
  // 2) Recientes (retomar lo ultimo) — shelf horizontal
  if(d.recent?.length){
    h+=`<div class="sch"><h2>${_IC_CLOCK} Recientes</h2></div><div class="shelf" id="sh-recent">${d.recent.map(mkShelfCard).join('')}</div>`;
  }
  // 3) Playlists — shelf horizontal (se llena async)
  h+=`<div id="pl-shelf-wrap"></div>`;
  // 4) Mix para ti (radio / descubrimiento) — hasta abajo
  if(d.mix?.length){
    h+=`<div class="sch ssep"><h2>${_IC_MIX} Mix para ti</h2><small>radio</small></div><div class="grid" id="grid-mix"></div>`;
  }
  el.innerHTML=h;
  if(d.mix?.length){const g=G('grid-mix');d.mix.forEach(t=>{const dv=document.createElement('div');dv.innerHTML=mkCard(t);g.appendChild(dv.firstElementChild);});bindCards(el,d.mix);}
  bindCards(el,d.top||[]);
  if(d.recent?.length)bindShelf(G('sh-recent'),d.recent);
  loadPlShelf();
}

/* ═══ CARD ════════════════════════════════════════════ */
/* ═══ ARTISTA / DISCOGRAFÍA ═════════════════════════ */
let _lastArtist='';
async function openArtist(name){
  name=(name||'').trim();
  if(!name||name==='—'||name==='Local')return;
  _lastArtist=name;
  switchTab('music');
  const el=G('panel-music');
  el.innerHTML='<div class="swrap"><div class="spin"></div></div>';
  try{
    const d=await(await fetch('/api/artist?name='+encodeURIComponent(name))).json();
    renderArtist(d,el);
  }catch(e){
    el.innerHTML='<div class="splash"><div class="splash-ico">'+IC.warn+'</div><p>Error al cargar el artista</p></div>';
  }
}

function _relCards(list){
  return '<div class="agrid">'+list.map(al=>
    '<div class="acard" onclick="openAlbum(\''+encodeURIComponent(al.id)+'\')">'+
      '<div class="aw"><img src="'+esc(al.thumb)+'" loading="lazy" decoding="async" onload="this.classList.add(\'ld\')" onerror="this.parentElement.style.visibility=\'hidden\'"></div>'+
      '<div class="an">'+esc(al.title)+'</div>'+
      (al.year?'<div class="ay">'+esc(al.year)+'</div>':'')+
    '</div>').join('')+'</div>';
}

function renderArtist(d,el){
  let h='<div class="art-hd">';
  if(d.thumb)h+='<img class="art-av" src="'+esc(d.thumb)+'" decoding="async" onload="this.classList.add(\'ld\')" onerror="this.style.display=\'none\'">';
  h+='<div class="art-hi"><div class="art-hn">'+esc(d.name)+'</div>'+
     (d.subs?'<div class="art-hs">'+esc(d.subs)+'</div>':'')+'</div></div>';
  if(d.songs&&d.songs.length)h+='<div class="sch"><h2>Canciones</h2></div><div class="grid" id="grid-ar"></div>';
  if(d.albums&&d.albums.length)h+='<div class="sch ssep"><h2>Álbumes</h2></div>'+_relCards(d.albums);
  if(d.singles&&d.singles.length)h+='<div class="sch ssep"><h2>Sencillos</h2></div>'+_relCards(d.singles);
  if((!d.songs||!d.songs.length)&&(!d.albums||!d.albums.length))
    h+='<div class="splash"><div class="splash-ico">'+IC.search+'</div><p>Sin resultados para este artista</p></div>';
  el.innerHTML=h;
  if(d.songs&&d.songs.length){
    const g=G('grid-ar');
    d.songs.forEach(t=>{const div=document.createElement('div');div.innerHTML=mkCard(t);g.appendChild(div.firstElementChild);});
    Q=d.songs;qi=-1;
    bindCards(el,d.songs,true);
  }
}

async function openAlbum(id){
  id=decodeURIComponent(id||'');
  if(!id)return;
  switchTab('music');
  const el=G('panel-music');
  el.innerHTML='<div class="swrap"><div class="spin"></div></div>';
  try{
    const d=await(await fetch('/api/album?id='+encodeURIComponent(id))).json();
    if(d.error){toast('Álbum no disponible','err');openArtist(_lastArtist);return;}
    _curAlbum={name:d.title,artist:((d.tracks&&d.tracks[0]&&d.tracks[0].channel)||_lastArtist),tracks:d.tracks||[]};
    let h='<div class="sch alb-top"><div class="alb-ttl">'+
      '<button class="pgb" onclick="openArtist(_lastArtist)">◀</button>'+
      '<h2>'+esc(d.title)+(d.year?' · '+esc(d.year):'')+'</h2></div>'+
      '<button class="dla-btn" onclick="dlCurrentAlbum()">'+IC.download+'<span>Álbum</span></button></div>'+
      '<div class="grid" id="grid-al"></div>';
    el.innerHTML=h;
    const g=G('grid-al');
    (d.tracks||[]).forEach(t=>{const div=document.createElement('div');div.innerHTML=mkCard(t);g.appendChild(div.firstElementChild);});
    Q=d.tracks||[];qi=-1;
    bindCards(el,d.tracks||[],true);
  }catch(e){toast('Error al cargar álbum','err');}
}

function mkCard(t){
  return `<div class="srow" data-vid="${esc(t.id)}">
    <div class="art">
      <img src="${esc(t.thumb||'')}" loading="lazy" decoding="async"
           onload="this.classList.add('ld')"
           onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
      <div class="art-ph sr-ph" style="display:none"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div>
      <div class="sr-eq"><span></span><span></span><span></span></div>
      <button class="bpp sr-play"><svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5.3c0-.8.86-1.3 1.54-.88l11 6.7c.65.4.65 1.36 0 1.76l-11 6.7A1.03 1.03 0 0 1 7 18.7z"/></svg></button>
    </div>
    <div class="sr-meta">
      <div class="sr-ti">${esc(t.title)}</div>
      <div class="sr-ar"><span class="sr-arn">${esc(t.channel)}</span>${t.plays>1?` · ${t.plays}▶`:''}</div>
    </div>
    <span class="sr-dur">${esc(t.dur||'')}</span>
    <button class="bov bdd sr-act" title="Descargar"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg></button>
    <button class="bov bpl sr-act" title="Añadir a playlist"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h13M3 12h13M3 18h9"/><circle cx="19" cy="16" r="2"/><path d="M21 16V8"/></svg></button>
  </div>`;
}

function bindCards(container, tracks, finite){
  tracks.forEach((t,i)=>{
    container.querySelectorAll(`.srow[data-vid="${t.id}"]`).forEach(c=>{
      const _bpp=c.querySelector('.bpp');if(_bpp)_bpp.onclick=e=>{e.stopPropagation();loadQ(tracks,i,finite);};
      const _bdd=c.querySelector('.bdd');if(_bdd)_bdd.onclick=e=>{e.stopPropagation();startDl(t,c);};
      const _bpl=c.querySelector('.bpl');if(_bpl)_bpl.onclick=e=>{e.stopPropagation();showPlDrop(t,_bpl,e);};
      c.onclick=()=>loadQ(tracks,i,finite);
      _lpAttach(c,{track:t,isLocal:false,tracks,idx:i,finite});
    });
  });
}

function loadQ(tracks,idx,finite){Q=tracks;radioOn=!finite;playYT(idx);}

/* ═══ BÚSQUEDA ═══════════════════════════════════════ */
async function doSearch(q,fromChip=false){
  q=q.trim();if(!q)return;
  if(fromChip)G('qi').value=q;
  sqQ=q;sqPage=0;switchTab('music');await fetchSearch(q,0);
}

async function fetchSearch(q,page){
  const el=G('panel-music');
  el.innerHTML='<div class="swrap"><div class="spin"></div></div>';
  try{
    const d=await(await fetch(`/api/search?q=${encodeURIComponent(q)}&page=${page}`)).json();
    if(d.error){toast(d.error.slice(0,80),'err');return;}
    Q=d.results;qi=-1;sqPage=page;sqHasNext=d.has_next;
    renderSearch(d,el);
  }catch(e){
    el.innerHTML='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 20h20z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg></div><p>Error al buscar</p></div>';
  }
}

function renderSearch(d,el){
  if(!d.results?.length){el.innerHTML='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg></div><p>Sin resultados</p></div>';return;}
  el.innerHTML=`
    <div class="sch"><h2><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg> "${esc(sqQ)}"</h2><small>pág. ${sqPage+1}</small></div>
    <div class="grid" id="grid-s"></div>
    <div class="pager">
      <button class="pgb" ${sqPage===0?'disabled':''} onclick="fetchSearch(sqQ,sqPage-1)">◀ Anterior</button>
      <span class="pgi">${TL('Pág')} ${sqPage+1}</span>
      <button class="pgb" ${!sqHasNext?'disabled':''} onclick="fetchSearch(sqQ,sqPage+1)">Siguiente ▶</button>
    </div>`;
  const g=G('grid-s');
  d.results.forEach(t=>{
    const div=document.createElement('div');div.innerHTML=mkCard(t);g.appendChild(div.firstElementChild);
  });
  bindCards(el,d.results);
}

/* ═══ DESCARGA ═══════════════════════════════════════ */
/* ═══ GESTOR DE DESCARGAS (progreso en pestaña) ════════ */
const DLW={jobs:{},album:null};
let _curAlbum=null;let _curPl=null;let radioOn=true;
function renderDlActive(){
  const box=G('dlActive');if(!box)return;
  const jobs=Object.entries(DLW.jobs).filter(e=>e[1].status==='dl');
  if(!jobs.length&&!DLW.album){box.innerHTML='';return;}
  let h='';
  if(DLW.album){
    const pct=Math.round(DLW.album.done/Math.max(1,DLW.album.total)*100);
    h+='<div class="sch"><h2>Descargando álbum</h2><small>'+DLW.album.done+'/'+DLW.album.total+'</small></div>';
    h+='<div class="dlrow"><div class="dlrow-t">'+esc(DLW.album.name)+'</div><div class="dlrow-bar"><div class="dlrow-f" style="width:'+pct+'%"></div></div><div class="dlrow-p">'+pct+'%</div></div>';
  }else if(jobs.length){
    h+='<div class="sch"><h2>Descargando</h2><small>'+jobs.length+'</small></div>';
  }
  jobs.forEach(e=>{const j=e[1],p=Math.round(j.pct);
    h+='<div class="dlrow"><div class="dlrow-t">'+esc((j.title||'').slice(0,42))+'</div><div class="dlrow-bar"><div class="dlrow-f" style="width:'+p+'%"></div></div><div class="dlrow-p">'+p+'%</div></div>';
  });
  box.innerHTML=h;
}
function dlPaint(){if(!G('panel-files').classList.contains('hidden'))renderDlActive();}
function dlDone(msg){toast(msg||'Listo','ok');if(!G('panel-files').classList.contains('hidden'))renderDlActive();}
function startDownload(t,ctx,card){
  return new Promise(resolve=>{
    DLW.jobs[t.id]={title:t.title||'',pct:0,status:'dl'};dlPaint();
    let ovId,pfId,ppId;
    if(card){
      ovId='cov-'+t.id;pfId='cpf-'+t.id;ppId='cpp-'+t.id;
      const art=card.querySelector('.art');
      if(art&&!document.getElementById(ovId))art.insertAdjacentHTML('beforeend',
        `<div class="cov" id="${ovId}"><div class="cpb"><div class="cpf" id="${pfId}" style="width:0%"></div></div><div class="cpp" id="${ppId}">0%</div></div>`);
    }
    fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:t.id,ctx:ctx||null})})
      .then(r=>r.json()).then(resp=>{
        if(!(ctx&&ctx.album))toast((resp.mode==='audio'?'Audio':'Video')+': '+(t.title||'').slice(0,26),'dl');
        if(pollers[t.id])clearInterval(pollers[t.id]);
        pollers[t.id]=setInterval(async()=>{
          try{
            const s=await(await fetch('/api/dl_status/'+t.id)).json();
            const j=DLW.jobs[t.id];if(j){j.pct=s.pct||0;j.status=s.status;}
            const pct=Math.round(s.pct||0);
            const proc=s.status==='processing'||(pct>=99&&s.status!=='done'&&s.status!=='error');
            if(card){const pf=G(pfId),pp=G(ppId),ov=G(ovId);
              if(ov)ov.classList.toggle('proc',proc);
              if(pf)pf.style.width=(proc?100:pct)+'%';
              if(pp)pp.textContent=proc?'···':(pct+'%');}
            if(s.status==='done'){clearInterval(pollers[t.id]);if(j){j.pct=100;j.status='done';}
              if(card){const ov=G(ovId);if(ov)ov.remove();const b=card.querySelector('.bdd');if(b)b.style.color='var(--grn)';}
              if(DLW.album)DLW.album.done++;
              if(!(ctx&&ctx.album))toast(TL('Guardado')+': '+(t.title||'').slice(0,26),'ok');
              if(!G('panel-files').classList.contains('hidden'))loadFiles();else dlPaint();
              resolve(true);}
            else if(s.status==='error'){clearInterval(pollers[t.id]);if(j)j.status='error';
              if(card){const ov=G(ovId);if(ov)ov.innerHTML='<span style="color:#e53935;font-size:10px;padding:4px">'+IC.x+'</span>';}
              if(DLW.album)DLW.album.done++;dlPaint();if(!(ctx&&ctx.album))toast('Error al descargar','err');resolve(false);}
            else dlPaint();
          }catch(e){clearInterval(pollers[t.id]);resolve(false);}
        },450);
      }).catch(()=>{if(!(ctx&&ctx.album))toast('Error al descargar','err');resolve(false);});
  });
}
async function dlAlbum(name,artist,tracks){
  if(DLW.album){toast('Ya hay un álbum descargando','err');return;}
  if(!tracks||!tracks.length){toast('Nada que descargar','err');return;}
  DLW.album={name,total:tracks.length,done:0};dlPaint();
  toast(TL('Descargando')+': '+name,'dl');
  for(let i=0;i<tracks.length;i++){
    const t=tracks[i];
    await startDownload(t,{album:name,album_artist:artist,artist:(t.channel||artist),track:i+1},null);
  }
  const nm=DLW.album.name;DLW.album=null;dlDone('"'+nm+'" descargado');
  if(!G('panel-files').classList.contains('hidden'))loadFiles();
}
function dlCurrentAlbum(){if(_curAlbum)dlAlbum(_curAlbum.name,_curAlbum.artist,_curAlbum.tracks);}
function dlPlaylist(){if(_curPl&&_curPl.tracks&&_curPl.tracks.length)dlAlbum(_curPl.name,'Playlist',_curPl.tracks);}

/* ═══ RADIO / MIX + ALGORITMO DE GUSTOS ════════════ */
function _feedback(t,action){
  if(!t||!t.id||String(t.id).indexOf('local:')===0)return;
  fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:t.id,action,artist:t.channel||t.artist||'',title:t.title||''})}).catch(()=>{});
}
function _skipFeedback(){
  if(MODE==='yt'&&curTrack&&MA.duration>0&&(MA.currentTime<10||MA.currentTime/MA.duration<0.15))
    _feedback(curTrack,'skip');
}
let _radioBusy=false;
async function _radioMore(){
  if(MODE!=='yt'||!radioOn||!curTrack||_radioBusy)return;
  _radioBusy=true;
  const _before=Q.length;
  try{
    let seed=Q[Q.length-1]||curTrack, added=0, tries=0;
    while(added===0 && tries<3){
      const d=await(await fetch('/api/radio?seed='+encodeURIComponent(seed.id)+'&artist='+encodeURIComponent(seed.channel||''))).json();
      const have=new Set(Q.map(t=>t.id));
      (d.tracks||[]).forEach(t=>{if(t.id&&!have.has(t.id)){Q.push(t);have.add(t.id);added++;}});
      if(added===0){ tries++; seed=Q[Math.max(0,Q.length-1-tries*6)]||curTrack; }  // cambia semilla si repitio
    }
    if(added){G('bnext').disabled=false;renderQueue();}
    if(NATIVE&&Q.length>_before)_natAppend(Q.slice(_before));
  }catch(e){}
  _radioBusy=false;
}
async function _ensureRadio(){
  if(MODE!=='yt'||!radioOn||!curTrack)return;
  if(Q.length-qi>2)return;
  try{
    const _before=Q.length;
    const d=await(await fetch('/api/radio?seed='+encodeURIComponent(curTrack.id)+'&artist='+encodeURIComponent(curTrack.channel||''))).json();
    const have=new Set(Q.map(t=>t.id));
    (d.tracks||[]).forEach(t=>{if(t.id&&!have.has(t.id)){Q.push(t);have.add(t.id);}});
    if(Q.length>1)G('bnext').disabled=false;
    if(NATIVE&&Q.length>_before)_natAppend(Q.slice(_before));
  }catch(e){}
}
async function _playNextYT(){
  if(MODE!=='yt'){if(LQ.length>1)playLocal((lqi+1)%LQ.length);return;}
  if(qi+1<Q.length){playYT(qi+1);return;}
  // fin de la cola: la radio arranca sola (incluso si la lista era finita)
  radioOn=true;
  if(curTrack){await _ensureRadio();if(qi+1<Q.length){playYT(qi+1);return;}}
  if(Q.length>1)playYT((qi+1)%Q.length);
}
async function startRadio(){
  if(!curTrack||MODE!=='yt'){toast('Reproduce una canción primero','err');return;}
  radioOn=true;toast('Creando mix de canciones…','dl');
  try{
    const d=await(await fetch('/api/radio?seed='+encodeURIComponent(curTrack.id)+'&artist='+encodeURIComponent(curTrack.channel||''))).json();
    const cur=Q[qi]||curTrack;
    const extra=(d.tracks||[]).filter(t=>t.id!==cur.id);
    Q=[cur,...extra];qi=0;
    G('bprev').disabled=false;G('bnext').disabled=Q.length<=1;
    toast('Mix de canciones listo','ok');
    openQueue();
  }catch(e){toast('No se pudo crear el mix','err');}
}

function startDl(t,card){return startDownload(t,null,card);}
async function _dlLegacy(t,card){
  const ovId='cov-'+t.id,pfId='cpf-'+t.id,ppId='cpp-'+t.id;
  const art=card.querySelector('.art');
  if(!document.getElementById(ovId)){
    art.insertAdjacentHTML('beforeend',
      `<div class="cov" id="${ovId}">
         <div class="cpb"><div class="cpf" id="${pfId}" style="width:0%"></div></div>
         <div class="cpp" id="${ppId}">0%</div>
       </div>`);
  }
  const resp=await(await fetch('/api/download',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:t.id})})).json();
  const ml=resp.mode==='audio'?'Audio':'Video';
  toast(`${ml}: ${t.title.slice(0,35)}`,'dl');
  if(pollers[t.id])clearInterval(pollers[t.id]);
  pollers[t.id]=setInterval(async()=>{
    try{
      const s=await(await fetch('/api/dl_status/'+t.id)).json();
      const pf=G(pfId),pp=G(ppId);
      if(pf)pf.style.width=Math.round(s.pct||0)+'%';
      if(pp)pp.textContent=Math.round(s.pct||0)+'%';
      if(s.status==='done'){
        clearInterval(pollers[t.id]);
        const ov=G(ovId);if(ov)ov.remove();
        card.querySelector('.bdd').style.color='var(--grn)';
        toast(ml+' '+TL('guardado')+': '+s.file,'ok');
        if(!G('panel-files').classList.contains('hidden'))loadFiles();
      }else if(s.status==='error'){
        clearInterval(pollers[t.id]);
        const ov=G(ovId);if(ov)ov.innerHTML='<span style="color:#e53935;font-size:10px;padding:4px">'+IC.x+'</span>';
        toast('Error al descargar','err');
      }
    }catch(e){clearInterval(pollers[t.id]);}
  },900);
}

/* ═══ TAB DESCARGAS ══════════════════════════════════ */
async function loadFiles(){
  const el=G('panel-files');
  el.innerHTML='<div class="swrap"><div class="spin"></div></div>';
  try{
    const files=await(await fetch('/api/files')).json();
    LQ=files;
    if(!files.length){
      el.innerHTML='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></div><p>Sin descargas aún.<br>Usa <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg> en cualquier canción.</p></div>';return;
    }
    let h=`<div id="dlActive"></div><div class="sch"><h2><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg> Descargas</h2><small>${files.length} archivos</small></div>`;
    const _row=(f,i)=>{
      const nm=f.title||f.name.replace(/\.[^.]+$/,'');
      const mt=[f.artist,f.dur&&f.dur!=='—'?f.dur:null,(f.size/1024/1024).toFixed(1)+' MB',f.ext.toUpperCase()].filter(Boolean).join(' · ');
      const playAction=f.is_video?`openLocalVideoByIdx(${i})`:`playLocal(${i})`;
      return `<div class="frow" id="fr-${i}">
        <div class="fart" onclick="${playAction}"><div class="fico-ph">${f.is_video?IC.film:IC.note}</div><img src="/api/cover/${encodeURIComponent(f.name)}" loading="lazy" decoding="async" onload="this.classList.add('ld')" onerror="this.remove()"></div>
        <div class="finfo" onclick="${playAction}"><div class="fname">${esc(nm)}</div><div class="fmeta">${esc(mt)}</div></div>
        <div class="fbtns">
          <button class="fbtn ${f.liked?'lk':''}" id="flk-${i}" onclick="toggleLocalLike(${i})">${IC.heart}</button>
          <a class="fdl" href="/sandbox/${encodeURIComponent(f.name)}" download onclick="event.stopPropagation()"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg></a>
        </div>
      </div>`;
    };
    const groups={},singles=[];
    files.forEach((f,i)=>{ if(f.album){(groups[f.album]=groups[f.album]||[]).push(i);} else singles.push(i); });
    Object.keys(groups).forEach(alb=>{
      const idxs=groups[alb],first=files[idxs[0]];
      h+=`<div class="alb-hd"><img class="alb-cv" src="/api/cover/${encodeURIComponent(first.name)}" loading="lazy" onerror="this.style.visibility='hidden'"><div class="alb-i"><div class="alb-n">${esc(alb)}</div><div class="alb-c">${idxs.length} pistas${first.artist?' · '+esc(first.artist):''}</div></div></div>`;
      idxs.forEach(i=>{h+=_row(files[i],i);});
    });
    if(singles.length){
      if(Object.keys(groups).length)h+=`<div class="sch ssep" style="margin-top:16px"><h2>Canciones</h2></div>`;
      singles.forEach(i=>{h+=_row(files[i],i);});
    }
    el.innerHTML=h;
    renderDlActive();
    files.forEach((f,i)=>{const row=G('fr-'+i);if(row)_lpAttach(row,{track:f,isLocal:true,idx:i});});
    if(MODE==='local'&&lqi>=0){const r=G('fr-'+lqi);if(r)r.classList.add('playing');}
  }catch(e){
    G('panel-files').innerHTML='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 20h20z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg></div><p>Error al cargar</p></div>';
  }
}

function openLocalVideoByIdx(i){
  const f=LQ[i];
  curTrack={id:'local:'+f.name,...f};
  MODE='local';lqi=i;
  setMeta(f.title||f.name.replace(/\.[^.]+$/,''),f.artist||'Local',null,true);
  setLikeBtn(f.name,'local');
  openLocalVideo();
}

async function toggleLocalLike(i){
  const f=LQ[i];
  const meta={title:f.title,artist:f.artist||'',dur:f.dur||''};
  const r=await(await fetch('/api/like/local',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:f.name,meta})})).json();
  const btn=G('flk-'+i);
  if(btn){btn.innerHTML=IC.heart;btn.classList.toggle('lk',r.liked);}
  toast(r.liked?'Me gusta':'Eliminado',r.liked?'like':'unlike');
  if(!G('panel-likes').classList.contains('hidden'))loadLikes();
}

/* ═══ TAB ME GUSTA ════════════════════════════════════ */
async function loadLikes(){
  const el=G('panel-likes');
  el.innerHTML='<div class="swrap"><div class="spin"></div></div>';
  try{
    const {yt=[],local=[]}=await(await fetch('/api/likes')).json();
    if(!yt.length&&!local.length){
      el.innerHTML='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 5.6a5 5 0 0 0-7.1 0L12 7.3l-1.7-1.7a5 5 0 0 0-7.1 7.1l1.7 1.7L12 21l7.1-7.1 1.7-1.7a5 5 0 0 0 0-7.1z"/></svg></div><p>Sin me gustas.<br>Usa <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 5.6a5 5 0 0 0-7.1 0L12 7.3l-1.7-1.7a5 5 0 0 0-7.1 7.1l1.7 1.7L12 21l7.1-7.1 1.7-1.7a5 5 0 0 0 0-7.1z"/></svg> mientras escuchas.</p></div>';return;
    }
    let h='';
    if(yt.length){
      h+=`<div class="sch"><h2>${IC.heart} YouTube</h2><small>${yt.length}</small></div>
          <div class="grid" id="grid-lk">${yt.map(t=>mkCard(t)).join('')}</div><div class="ssep"></div>`;
    }
    if(local.length){
      h+=`<div class="sch"><h2>${IC.save} Archivos locales</h2><small>${local.length}</small></div>`;
      local.forEach(([name,m])=>{
        const nm=m.title||name.replace(/\.[^.]+$/,'');
        h+=`<div class="frow" onclick="playLocalByName('${esc(name)}')">
          <div class="fico">${IC.note}</div>
          <div class="finfo">
            <div class="fname">${esc(nm)}</div>
            <div class="fmeta">${esc([m.artist,m.dur].filter(Boolean).join(' · '))}</div>
          </div>
        </div>`;
      });
    }
    el.innerHTML=h;
    if(yt.length)bindCards(el,yt,true);
  }catch(e){G('panel-likes').innerHTML='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 20h20z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg></div><p>Error</p></div>';}
}

function playLocalByName(name){
  const idx=LQ.findIndex(f=>f.name===name);
  if(idx>=0)playLocal(idx);else toast('Descarga el archivo primero');
}

/* ═══ TAB PLAYLISTS ══════════════════════════════════ */
async function loadPlaylists(openId){
  const el=G('panel-pl');
  el.innerHTML='<div class="swrap"><div class="spin"></div></div>';
  try{
    const pls=await(await fetch('/api/playlists')).json();
    if(openId){const pl=pls.find(p=>p.id===openId);if(pl){renderPlDetail(pl,el);return;}}
    activePlID=null;
    let h=`<div class="pl-hdr"><h2><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h13M3 12h13M3 18h9"/><circle cx="19" cy="16" r="2"/><path d="M21 16V8"/></svg> Playlists</h2><button class="new-pl" onclick="createPl()">+ Nueva</button></div>`;
    if(!pls.length){h+='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h13M3 12h13M3 18h9"/><circle cx="19" cy="16" r="2"/><path d="M21 16V8"/></svg></div><p>Sin playlists.<br>Crea tu primera.</p></div>';}
    pls.forEach(pl=>{
      h+=`<div class="pl-card" onclick="openPl('${esc(pl.id)}')">
        <h3><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg> ${esc(pl.name)}</h3>
        <small>${pl.tracks.length} canciones</small>
        <div class="pl-acts" onclick="event.stopPropagation()">
          <button class="pl-act" onclick="renamePl('${esc(pl.id)}','${esc(pl.name)}')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/></svg> Renombrar</button>
          <button class="pl-act del" onclick="deletePl('${esc(pl.id)}','${esc(pl.name)}')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg> Eliminar</button>
        </div>
      </div>`;
    });
    el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 20h20z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg></div><p>Error</p></div>';}
}

function renderPlDetail(pl,el){
  activePlID=pl.id;_curPl={name:pl.name,tracks:pl.tracks};
  let h=`<div class="sch">
    <div style="display:flex;align-items:center;gap:8px">
      <button style="color:var(--t2);font-size:20px;padding:2px" onclick="backToPl()">◀</button>
      <h2><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg> ${esc(pl.name)}</h2>
    </div>
    <button class="dla-btn" onclick="dlPlaylist()">${IC.download}<span>Descargar</span></button>
  </div>`;
  if(!pl.tracks.length){h+='<div class="splash"><div class="splash-ico"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div><p>Playlist vacía.<br>Añade canciones con <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h13M3 12h13M3 18h9"/><circle cx="19" cy="16" r="2"/><path d="M21 16V8"/></svg></p></div>';el.innerHTML=h;return;}
  h+=`<div class="grid">${pl.tracks.map(t=>mkCard(t)).join('')}</div>`;
  el.innerHTML=h;
  bindCards(el,pl.tracks,true);
  // Botón quitar de playlist en cada card
  pl.tracks.forEach(t=>{
    el.querySelectorAll(`[data-vid="${t.id}"] .ov`).forEach(ov=>{
      const btn=document.createElement('button');btn.className='bov';btn.textContent='✕';btn.title='Quitar';
      btn.onclick=e=>{e.stopPropagation();removePlTrack(pl.id,t.id);};ov.appendChild(btn);
    });
  });
}

async function createPl(){
  const name=await promptModal('Nueva playlist','Crear');if(!name)return;
  await fetch('/api/playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  toast('Playlist creada','ok');loadPlaylists();
}
async function deletePl(id,name){
  if(!confirm(TL('¿Eliminar')+' "'+name+'"?'))return;
  await fetch(`/api/playlists/${id}`,{method:'DELETE'});
  toast('Eliminada','trash');loadPlaylists();
}
async function renamePl(id,old){
  const name=await promptModal('Renombrar playlist','Guardar');if(!name||name===old)return;
  await fetch(`/api/playlists/${id}/rename`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  toast('Renombrada','ok');loadPlaylists(activePlID||undefined);
}
async function removePlTrack(plid,tid){
  await fetch(`/api/playlists/${plid}/tracks/${tid}`,{method:'DELETE'});
  toast('Quitado');loadPlaylists(plid);
}
function openPl(id){activePlID=id;loadPlaylists(id);}
function backToPl(){activePlID=null;loadPlaylists();}

/* ═══ TABS ════════════════════════════════════════════ */
function switchTab(t){
  ['home','music','files','likes','pl'].forEach(p=>{
    G('t-'+p)?.classList.toggle('on',p===t);
    G('panel-'+p)?.classList.toggle('hidden',p!==t);
  });
  if(t==='files')loadFiles();
  if(t==='likes')loadLikes();
  if(t==='pl')loadPlaylists(activePlID||null);
}

/* ═══ CONFIG ══════════════════════════════════════════ */
async function loadCfg(){
  try{CFG=await(await fetch('/api/config')).json();pendCFG={...CFG};syncCfgUI();}catch(e){}
}
function syncCfgUI(){
  document.querySelectorAll('.ob:not(.lang-opt)').forEach(b=>{
    const v=String(pendCFG[b.dataset.key]??CFG[b.dataset.key]??'');
    b.classList.toggle('on',b.dataset.val===v);
  });
}
document.querySelectorAll('.ob:not(.lang-opt)').forEach(b=>{
  b.onclick=()=>{
    b.parentElement.querySelectorAll('.ob').forEach(x=>x.classList.remove('on'));b.classList.add('on');
    let v=b.dataset.val;if(b.dataset.key==='search_per_page')v=parseInt(v);
    pendCFG[b.dataset.key]=v;
  };
});
async function saveCfg(){
  try{
    const r=await(await fetch('/api/config',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(pendCFG)})).json();
    if(r.ok){CFG={...r.config};pendCFG={...CFG};toast('Configuración guardada','ok');closeCfg();}
  }catch(e){toast('Error al guardar','err');}
}
function openCfg(){pendCFG={...CFG};syncCfgUI();G('cfgM').classList.add('show');}
function closeCfg(){G('cfgM').classList.remove('show');}
async function clearHistory(){
  if(!confirm(TL('¿Borrar historial?')))return;
  await fetch('/api/history/clear',{method:'POST'});
  toast('Historial borrado','trash');closeCfg();showDash();
}
G('btnCfg').onclick=openCfg;
G('cfgM').addEventListener('click',e=>{if(e.target===G('cfgM'))closeCfg();});

/* ═══ UTILS ═══════════════════════════════════════════ */
const IC={rep:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2l4 4-4 4"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>',rep1:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2l4 4-4 4"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/><text x="12" y="14.6" font-size="9" stroke="none" fill="currentColor" text-anchor="middle" font-weight="700">1</text></svg>',note:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',cog:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',home:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10l9-7 9 7"/><path d="M5 9v11h14V9"/><path d="M10 20v-6h4v6"/></svg>',search:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>',folder:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',heart:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 5.6a5 5 0 0 0-7.1 0L12 7.3l-1.7-1.7a5 5 0 0 0-7.1 7.1l1.7 1.7L12 21l7.1-7.1 1.7-1.7a5 5 0 0 0 0-7.1z"/></svg>',heartF:'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 5.6a5 5 0 0 0-7.1 0L12 7.3l-1.7-1.7a5 5 0 0 0-7.1 7.1l1.7 1.7L12 21l7.1-7.1 1.7-1.7a5 5 0 0 0 0-7.1z"/></svg>',list:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h13M3 12h13M3 18h9"/><circle cx="19" cy="16" r="2"/><path d="M21 16V8"/></svg>',monitor:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M9.5 8.5l5 2.5-5 2.5z" fill="currentColor" stroke="none"/><path d="M8 21h8M12 18v3"/></svg>',volume:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M16 9a3 3 0 0 1 0 6"/><path d="M19 6a7 7 0 0 1 0 12"/></svg>',film:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M17 9h4M3 15h4M17 15h4"/></svg>',save:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3h11l3 3v15H5z"/><path d="M8 3v5h7V3M8 21v-7h8v7"/></svg>',trash:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',edit:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/></svg>',check:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 6"/></svg>',x:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>',warn:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 20h20z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg>',headph:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14v-2a8 8 0 0 1 16 0v2"/><rect x="3" y="14" width="4.5" height="6" rx="1.2"/><rect x="16.5" y="14" width="4.5" height="6" rx="1.2"/></svg>',target:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/></svg>',flame:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2s4.5 3.3 4.5 8.5a4.5 4.5 0 0 1-9 0c0-1.7.8-3 1.5-3.9C9.8 8.6 11 7.5 12 2z"/></svg>',clock:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>',download:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg>',plus:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>',_t:{ok:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 6"/></svg>',err:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>',like:'<svg class="ic" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 5.6a5 5 0 0 0-7.1 0L12 7.3l-1.7-1.7a5 5 0 0 0-7.1 7.1l1.7 1.7L12 21l7.1-7.1 1.7-1.7a5 5 0 0 0 0-7.1z"/></svg>',unlike:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 5.6a5 5 0 0 0-7.1 0L12 7.3l-1.7-1.7a5 5 0 0 0-7.1 7.1l1.7 1.7L12 21l7.1-7.1 1.7-1.7a5 5 0 0 0 0-7.1z"/></svg>',dl:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 4 5-4"/><path d="M5 20h14"/></svg>',trash:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',warn:'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 20h20z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg>'}};
IC.play='<svg class="ic play-ic" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5.3c0-.8.86-1.3 1.54-.88l11 6.7c.65.4.65 1.36 0 1.76l-11 6.7A1.03 1.03 0 0 1 7 18.7z"/></svg>';IC.pause='<svg class="ic" viewBox="0 0 24 24" fill="currentColor"><rect x="6.5" y="5" width="4" height="14" rx="1.3"/><rect x="13.5" y="5" width="4" height="14" rx="1.3"/></svg>';IC.prev='<svg class="ic" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="2.4" height="14" rx="1"/><path d="M20 5.5v13c0 .82-.93 1.3-1.6.83l-9-6.5a1 1 0 0 1 0-1.66l9-6.5c.67-.48 1.6 0 1.6.83z"/></svg>';IC.next='<svg class="ic" viewBox="0 0 24 24" fill="currentColor"><rect x="16.6" y="5" width="2.4" height="14" rx="1"/><path d="M4 5.5v13c0 .82.93 1.3 1.6.83l9-6.5a1 1 0 0 0 0-1.66l-9-6.5C4.93 4.2 4 4.68 4 5.5z"/></svg>';IC.load='<svg class="ic spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.2-8.55"/></svg>';
const IC_QNEXT='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h11M4 12h8M4 17h8"/><path d="M15 15l5 3-5 3z" fill="currentColor" stroke="none"/></svg>';const IC_ARTIST='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/></svg>';
IC.vol='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M16 9a3 3 0 0 1 0 6"/><path d="M19 6a7 7 0 0 1 0 12"/></svg>';IC.mute='<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M22 9l-6 6M16 9l6 6"/></svg>';
function G(id){return document.getElementById(id);}
function fmt(s){if(!s||isNaN(s))return'0:00';const m=Math.floor(s/60),x=Math.floor(s%60);return m+':'+x.toString().padStart(2,'0');}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function toast(msg,type){const t=document.createElement('div');t.className='tst';if(type&&IC._t[type]){const i=document.createElement('span');i.className='tst-i';i.innerHTML=IC._t[type];t.appendChild(i);}const s=document.createElement('span');s.textContent=TL(msg);t.appendChild(s);G('toasts').appendChild(t);setTimeout(()=>t.remove(),3300);}

/* ═══ EVENTOS ═════════════════════════════════════════ */
G('qb').onclick=()=>doSearch(G('qi').value);
// Radio infinito: al acercarse al fondo de la cola, trae mas canciones
(function(){const ql=G('qList');if(ql)ql.addEventListener('scroll',()=>{
  if(ql.scrollTop+ql.clientHeight>=ql.scrollHeight-160)_radioMore();
},{passive:true});})();
G('qi').onkeydown=e=>{if(e.key==='Enter')doSearch(G('qi').value);};
document.addEventListener('keydown',e=>{
  if(e.key===' '&&e.target.tagName!=='INPUT'){e.preventDefault();G('bplay').click();}
  if(e.key==='Escape'){closeCtx();closeQueue();closeNP();closeKaraoke();closeVideo();closeCfg();G('inpM').classList.remove('show');}
});

function _setOffline(off){try{G('offbar').classList.toggle('show',!!off);}catch(e){}}
window.addEventListener('offline',()=>_setOffline(true));
window.addEventListener('online',()=>_setOffline(false));
try{_setOffline(!navigator.onLine);}catch(e){}

/* ═══ INIT ════════════════════════════════════════════ */
_i18nBoot();
loadCfg();
showDash();
</script>
</body>
</html>"""

if __name__ == "__main__":
    print(f"""
    [==============================================]
    [+]  BeatStream v8  ::  servidor arriba
    [==============================================]
    [+] Abre    : http://localhost:5000
    [+] Sandbox : {str(SANDBOX)[-30:]}
    [{'+' if FFMPEG_OK else '!'}] ffmpeg  : {'disponible' if FFMPEG_OK else 'NO encontrado  (pkg install ffmpeg)'}
    [==============================================]
""")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
