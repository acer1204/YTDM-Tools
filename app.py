import os, json, shutil, threading, uuid, re, time
from pathlib import Path
from datetime import datetime
from difflib import SequenceMatcher
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, g
import yt_dlp
import psycopg2, psycopg2.extras
import jwt as pyjwt
import bcrypt

app = Flask(__name__, static_folder="static")

# ── Auth / DB ─────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
JWT_SECRET   = os.environ.get("JWT_SECRET", "ytdm-dev-secret-change-me")
JWT_EXPIRY_S = 7 * 24 * 3600   # 7 days
AUTH_ENABLED = bool(DATABASE_URL)

def _open_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def get_db():
    if not hasattr(g, "_db") or g._db.closed:
        g._db = _open_db()
    return g._db

@app.teardown_appcontext
def _close_db(e=None):
    db = g.pop("_db", None)
    if db and not db.closed:
        db.close()

def _init_db():
    conn = _open_db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id               SERIAL PRIMARY KEY,
                        username         VARCHAR(64) UNIQUE NOT NULL,
                        password_hash    TEXT NOT NULL,
                        is_admin         BOOLEAN DEFAULT FALSE,
                        can_add_channel  BOOLEAN DEFAULT FALSE,
                        created_at       TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_channel_access (
                        user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
                        channel_name TEXT NOT NULL,
                        PRIMARY KEY (user_id, channel_name)
                    )
                """)
                # Migration: add settings column for existing tables
                cur.execute("""
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS settings JSONB NOT NULL DEFAULT '{}'
                """)
                admin_pw  = os.environ.get("ADMIN_PASSWORD", "admin123")
                pw_hash   = bcrypt.hashpw(admin_pw.encode(), bcrypt.gensalt()).decode()
                cur.execute("""
                    INSERT INTO users (username, password_hash, is_admin, can_add_channel)
                    VALUES (%s, %s, TRUE, TRUE)
                    ON CONFLICT (username) DO NOTHING
                """, ("admin", pw_hash))
    finally:
        conn.close()

if AUTH_ENABLED:
    for _attempt in range(15):
        try:
            _init_db()
            print(f"[auth] DB ready (attempt {_attempt+1})")
            break
        except Exception as _auth_err:
            print(f"[auth] DB not ready (attempt {_attempt+1}): {_auth_err}")
            if _attempt < 14:
                time.sleep(2)
            else:
                print("[auth] DB init failed after 15 attempts — auth disabled")
                AUTH_ENABLED = False

def _make_token(user):
    return pyjwt.encode({
        "sub":             str(user["id"]),
        "username":        user["username"],
        "is_admin":        user["is_admin"],
        "can_add_channel": user["can_add_channel"],
        "exp":             int(time.time()) + JWT_EXPIRY_S,
    }, JWT_SECRET, algorithm="HS256")

def _decode_token(token):
    return pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])

def _current_user():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        return _decode_token(auth[7:])
    except Exception:
        return None

@app.before_request
def _auth_gate():
    # Always public
    if request.path in ("/", "/favicon.ico") or request.path.startswith("/static/") or request.path.startswith("/dl/"):
        return
    if request.path == "/api/auth/login":
        return
    if not AUTH_ENABLED:
        g.user = {"sub": 0, "username": "admin", "is_admin": True, "can_add_channel": True}
        return
    user = _current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    g.user = user

def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.get("user", {}).get("is_admin"):
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper

DATA_DIR      = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_FILE     = DATA_DIR / "jobs.json"
COOKIES_DIR   = DATA_DIR / "cookies"
SETTINGS_FILE = DATA_DIR / "settings.json"
COOKIES_DIR.mkdir(exist_ok=True)

jobs: dict = {}
jobs_lock    = threading.Lock()
_stop_flags: dict[str, threading.Event] = {}   # job_id → Event; set to stop download

# ── Channel concurrency queue ──────────────────────────────────────────────────
_job_queue: list       = []    # job_ids waiting for a free slot
_running_count: int    = 0     # channels currently downloading
_queue_lock            = threading.Lock()
_last_schedule_key: str | None = None   # prevents double-firing in same minute

# ── Settings ──────────────────────────────────────────────────────────────────

_DEFAULT_SETTINGS = {
    "download_dir":  os.environ.get("DOWNLOAD_DIR", str(Path("downloads").absolute())),
    "check_mode":    "full",   # "full" | "fast" | "recent"
    "recent_count":  50,
    "subtitle_langs": ["zh-Hant", "zh-Hans", "en", "ja"],
}

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
                # fill in any missing keys with defaults
                for k, v in _DEFAULT_SETTINGS.items():
                    s.setdefault(k, v)
                return s
        except Exception:
            pass
    return dict(_DEFAULT_SETTINGS)

def save_settings(s: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

settings: dict = load_settings()

def get_download_dir() -> Path:
    p = Path(settings["download_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p

# alias used throughout the file
DOWNLOAD_DIR = get_download_dir()

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv"}
AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".aac", ".ogg", ".flac", ".wav"}
IMG_EXTS   = {".jpg", ".jpeg", ".png", ".webp"}

# ── Media metadata cache ───────────────────────────────────────────────────────
MEDIA_CACHE_FILE  = DATA_DIR / "media_cache.json"
_media_cache: dict = {}          # cache_key → {"mtime": float, "entry": dict}
_media_cache_lock  = threading.Lock()

def _load_media_cache():
    global _media_cache
    if MEDIA_CACHE_FILE.exists():
        try:
            with open(MEDIA_CACHE_FILE, "r", encoding="utf-8") as f:
                _media_cache = json.load(f)
            return
        except Exception:
            pass
    _media_cache = {}

def _save_media_cache():
    with _media_cache_lock:
        data = dict(_media_cache)
    try:
        with open(MEDIA_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass

_load_media_cache()

_media_list: list = []          # full in-memory list of all entries (unfiltered/unsorted)
_media_list_lock  = threading.Lock()

def _build_list_from_cache():
    global _media_list
    with _media_list_lock:
        _media_list = [v["entry"] for v in _media_cache.values() if v.get("entry")]

def _media_list_upsert(entry: dict):
    """Insert or replace a single entry in _media_list by dir_rel."""
    dir_rel = entry.get("dir_rel", "")
    with _media_list_lock:
        for i, e in enumerate(_media_list):
            if e.get("dir_rel") == dir_rel:
                _media_list[i] = entry
                return
        _media_list.append(entry)

def _media_list_remove(dir_rel: str):
    """Remove an entry from _media_list by dir_rel."""
    with _media_list_lock:
        for i, e in enumerate(_media_list):
            if e.get("dir_rel") == dir_rel:
                del _media_list[i]
                return

_build_list_from_cache()


def _update_channel_media(channel_dir: Path):
    """Scan a single channel folder and update _media_list + _media_cache for changed entries only."""
    base = get_download_dir()
    if not channel_dir.exists():
        return
    dir_files: dict[Path, list[Path]] = {}
    try:
        for f in channel_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS:
                dir_files.setdefault(f.parent, []).append(f)
    except Exception:
        return
    dirty = False
    for d, files in dir_files.items():
        try:
            dir_mtime = d.stat().st_mtime
        except OSError:
            continue
        stem_groups: dict[str, list[Path]] = {}
        for f in files:
            clean = re.sub(r'\.f\d{2,4}$', '', f.stem)
            stem_groups.setdefault(clean, []).append(f)
        if len(stem_groups) == 1:
            cache_key = str(d.relative_to(base)).replace("\\", "/")
            entry, updated = _cached_entry(base, d, files, cache_key, dir_mtime, None)
            if updated and entry:
                _media_list_upsert(entry)
                dirty = True
        else:
            for stem, stem_files in stem_groups.items():
                cache_key = str(d.relative_to(base)).replace("\\", "/") + "\x00" + stem
                try:
                    stem_mtime = max(f.stat().st_mtime for f in stem_files)
                except OSError:
                    stem_mtime = dir_mtime
                entry, updated = _cached_entry(base, d, stem_files, cache_key, stem_mtime, stem)
                if updated and entry:
                    _media_list_upsert(entry)
                    dirty = True
    if dirty:
        _save_media_cache()


def _sync_video_title(output_dir: Path, vid_id: str, old_title: str, new_title: str, log_fn):
    """Rename the video folder and all files inside when a YouTube title has changed."""
    if not old_title or not new_title or old_title == new_title:
        return
    old_name = sanitize_dirname(old_title)
    new_name = sanitize_dirname(new_title)
    if old_name == new_name:
        return
    old_folder = output_dir / old_name
    new_folder = output_dir / new_name
    if not old_folder.exists() or not old_folder.is_dir():
        return
    if new_folder.exists():
        log_fn(f"[warn] 標題重新命名衝突：「{new_name}」已存在，略過 {vid_id}")
        return
    # Rename all files inside the folder whose name starts with the old folder name
    for f in list(old_folder.iterdir()):
        if f.is_file() and f.name.startswith(old_name):
            new_filename = new_name + f.name[len(old_name):]
            try:
                f.rename(old_folder / new_filename)
            except Exception as e:
                log_fn(f"[warn] 重新命名檔案失敗：{f.name} → {new_filename}：{e}")
    # Rename the folder itself
    try:
        old_folder.rename(new_folder)
        log_fn(f"[info] 影片標題已更新：「{old_name}」→「{new_name}」({vid_id})")
    except Exception as e:
        log_fn(f"[warn] 重新命名資料夾失敗：{old_name} → {new_name}：{e}")


# ── Persistence ───────────────────────────────────────────────────────────────

def load_jobs():
    if JOBS_FILE.exists():
        try:
            with open(JOBS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_jobs():
    with open(JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)


def _recover_interrupted_jobs(jobs_dict: dict):
    """On startup, any job still in running/prefetching was killed mid-download.
    Reset them to error so the user can re-trigger via 更新檢查."""
    for job in jobs_dict.values():
        # Queued jobs: _job_queue is not persisted, reset to pending so user can re-trigger
        if job.get("status") == "queued":
            job["status"] = "pending"
        if job.get("status") in ("running", "prefetching"):
            job["status"]      = "error"
            job["finished_at"] = datetime.now().isoformat()
            job["logs"]        = job.get("logs", []) + ["[warn] 服務重啟，下載中斷，請點「更新檢查」繼續"]
            # Reset stuck video states so progress is accurate on next run
            for v in job.get("videos", {}).values():
                if v.get("status") in ("downloading", "pending"):
                    v["status"]     = "error"
                    v["error_msg"]  = "服務重啟中斷"
                    v["error_i18n"] = {"key": "err_restart_interrupted", "args": []}
                    v["percent"]    = 0
                    v["speed"]      = None
    return jobs_dict


# Clean up any leftover temp cookie files from a previous interrupted run
for _f in COOKIES_DIR.glob("*.tmp_dl.txt"):
    try: _f.unlink()
    except Exception: pass

jobs = _recover_interrupted_jobs(load_jobs())
# Persist recovery immediately so restarts don't keep old stale state
with open(JOBS_FILE, "w", encoding="utf-8") as _f:
    json.dump(jobs, _f, ensure_ascii=False, indent=2)


# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize_dirname(name: str) -> str:
    result = re.sub(r'[\\/:*?"<>|#]', '_', name)
    return result.strip("._") or "unknown"


def fmt_duration(seconds):
    if not seconds:
        return None
    s = int(float(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_size(b):
    if b is None:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.2f} TB"


def fmt_speed(bps):
    if not bps:
        return None
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


def find_thumbnail(video_dir: Path):
    for ext in IMG_EXTS:
        for f in video_dir.glob("*" + ext):
            if ".info" not in f.stem:
                return f
    return None


def read_info_json(video_dir: Path) -> dict:
    for f in video_dir.glob("*.info.json"):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return {}


def normalize_title(title: str) -> str:
    t = re.sub(r'^\d{8}_', '', title)            # date prefix
    t = re.sub(r'\[[A-Za-z0-9_-]{6,12}\]', '', t)  # [videoId]
    t = re.sub(r'\([A-Za-z0-9_-]{6,12}\)', '', t)
    t = t.lower()
    t = re.sub(r'[^\w\s]', ' ', t, flags=re.UNICODE)
    return ' '.join(t.split())


# ── Download engine ───────────────────────────────────────────────────────────

def _is_channel_url(url: str) -> bool:
    """True if URL is a bare channel root (no /shorts /videos /streams suffix)."""
    return bool(re.search(
        r'youtube\.com/(@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)/?$',
        url
    ))


def _fetch_flat(url: str, opts: dict, timeout_s: int = 90) -> tuple[list, dict]:
    """Return (entries, info_dict) from a flat extraction.
    Runs in a daemon thread so we can enforce a wall-clock timeout — prevents
    certain channels (e.g. Shorts-heavy or geo-restricted) from hanging forever.
    """
    _result: list = [[], {}]

    def _worker():
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                return
            entries = info.get("entries") or []
            if not entries and info.get("id"):
                entries = [info]
            _result[0] = entries
            _result[1] = info
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        print(f"[warn] _fetch_flat timed out ({timeout_s}s): {url}", flush=True)
    return _result[0], _result[1]


def fetch_channel_info(url: str, cookies_file: str | None, max_items: int | None = None):
    """Return (channel_name, total_count, entries_list).
    For channel root URLs, fetches /videos and /shorts tabs separately and merges.
    Fetching the root URL only returns tab-level playlist stubs, not actual videos.
    max_items: if set, only fetch the most recent N items per tab (playlistend).
    """
    opts = {
        "quiet":          True,
        "extract_flat":   "in_playlist",
        "ignoreerrors":   True,
        "socket_timeout": 30,   # per-connection timeout (seconds)
    }
    if cookies_file:
        p = COOKIES_DIR / cookies_file
        if p.exists():
            opts["cookiefile"] = str(p)
    if max_items:
        opts["playlistend"] = max_items

    channel_name = None

    if _is_channel_url(url):
        base = url.rstrip("/")
        all_entries: list = []
        seen_ids: set = set()

        for tab in ("/videos", "/shorts"):
            kind = "short" if tab == "/shorts" else "video"
            # Pass a copy — yt-dlp mutates the opts dict in __init__,
            # so reusing the same dict for the second tab can corrupt extraction.
            tab_entries, tab_info = _fetch_flat(base + tab, dict(opts))
            if channel_name is None and tab_info:
                channel_name = (
                    tab_info.get("uploader") or tab_info.get("channel") or
                    tab_info.get("playlist_uploader") or tab_info.get("title")
                )
            for e in tab_entries:
                eid = e.get("id")
                if eid and eid not in seen_ids:
                    e["_kind"] = kind
                    all_entries.append(e)
                    seen_ids.add(eid)

        return channel_name, len(all_entries), all_entries

    # Non-channel URL (playlist, single video, etc.) — fetch directly
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None, 0, []
        entries = info.get("entries") or []
        if not entries and info.get("id"):
            entries = [info]
        channel_name = (
            info.get("uploader") or info.get("channel") or
            info.get("playlist_uploader") or info.get("title")
        )
        return channel_name, len(entries), entries
    except Exception:
        return None, 0, []


def _probe_max_height(url: str, cookies_file: str | None) -> int | None:
    """Return the highest video resolution (height) YouTube currently offers for
    a single video, or None on failure. Needs the JS runtime to surface the
    high-res VP9/AV1 formats — same config as the actual download."""
    opts = {
        "quiet":          True,
        "skip_download":  True,
        "socket_timeout": 30,
        "js_runtimes":       {"node": {}},
        "remote_components": ["ejs:github"],
    }
    if cookies_file:
        p = COOKIES_DIR / cookies_file
        if p.exists():
            opts["cookiefile"] = str(p)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        heights = [f.get("height") for f in (info.get("formats") or []) if f.get("height")]
        return max(heights) if heights else None
    except Exception:
        return None


def _remove_from_archive(archive_path: Path, vid_id: str):
    """Drop a single video ID from yt-dlp's download-archive so it re-downloads.
    Archive lines look like 'youtube <video_id>'."""
    if not archive_path.exists():
        return
    try:
        with open(archive_path, "r", encoding="utf-8") as af:
            lines = af.readlines()
        kept = [ln for ln in lines if ln.strip().split(" ")[-1] != vid_id]
        if len(kept) != len(lines):
            with open(archive_path, "w", encoding="utf-8") as af:
                af.writelines(kept)
    except Exception:
        pass


# Quality preset → max allowed video height. "best"/"audio_only" have no cap.
_QUALITY_CAP = {"4k": 2160, "2k": 1440, "1080p": 1080, "720p": 720, "480p": 480}

def _quality_cap(quality: str) -> int | None:
    return _QUALITY_CAP.get(quality)


def _stored_max_height(info: dict) -> int | None:
    """Highest video resolution recorded in a downloaded video's info.json
    (the formats YouTube offered at download time). None if unavailable."""
    heights = [f.get("height") for f in (info.get("formats") or []) if f.get("height")]
    return max(heights) if heights else None


def _enqueue_or_start(job_id: str):
    """Start the job immediately if a slot is free; otherwise add to queue."""
    global _running_count
    limit = int(settings.get("max_concurrent_channels", 0))
    with _queue_lock:
        if limit == 0 or _running_count < limit:
            _running_count += 1
            start_now = True
        else:
            if job_id not in _job_queue:
                _job_queue.append(job_id)
            with jobs_lock:
                if job_id in jobs and jobs[job_id]["status"] not in ("running", "prefetching"):
                    jobs[job_id]["status"] = "queued"
                    save_jobs()
            start_now = False
    if start_now:
        try:
            threading.Thread(target=_run_download_guarded, args=(job_id,), daemon=True).start()
        except Exception:
            with _queue_lock:
                _running_count = max(0, _running_count - 1)


def _try_start_next():
    """Called when a download finishes; start the next queued job if a slot opened up."""
    global _running_count
    limit = int(settings.get("max_concurrent_channels", 0))
    with _queue_lock:
        if not _job_queue:
            return
        if limit > 0 and _running_count >= limit:
            return
        next_id = _job_queue.pop(0)
        _running_count += 1
    try:
        threading.Thread(target=_run_download_guarded, args=(next_id,), daemon=True).start()
    except Exception:
        with _queue_lock:
            _running_count = max(0, _running_count - 1)


def _scheduler_loop():
    """Background thread: fires download queue on configured weekly schedule."""
    global _last_schedule_key
    time.sleep(60)  # skip first minute after restart to avoid spurious trigger
    while True:
        time.sleep(30)
        try:
            sched = settings.get("schedule", {})
            if not sched.get("enabled"):
                continue
            if int(settings.get("max_concurrent_channels", 0)) != 1:
                continue
            now = datetime.now()
            days = [str(d) for d in sched.get("days", [])]
            if str(now.weekday()) not in days:
                continue
            try:
                h, m = map(int, sched.get("time", "00:00").split(":"))
            except Exception:
                continue
            if now.hour != h or now.minute != m:
                continue
            trigger_key = f"{now.year}-{now.month}-{now.day}-{h}-{m}"
            if _last_schedule_key == trigger_key:
                continue
            _last_schedule_key = trigger_key
            with jobs_lock:
                ids = [jid for jid, j in jobs.items()
                       if j.get("status") not in ("running", "prefetching", "queued")]
            for jid in ids:
                _enqueue_or_start(jid)
        except Exception:
            pass


def run_download(job_id: str):
    # Register a stop flag for this run; cleared when done
    _stop_event = threading.Event()
    _stop_flags[job_id] = _stop_event

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            _stop_flags.pop(job_id, None)
            return
        job["status"] = "prefetching"
        job["logs"] = []
        job["downloaded"] = 0
        job["errors"] = 0
        # Preserve previous video list for fast/recent modes so it's not wiped;
        # full mode rebuilds from scratch via prefetch
        _prev_videos = dict(job.get("videos", {}))
        job["videos"] = {}
        job["is_update"] = False
        save_jobs()

    def log(msg):
        with jobs_lock:
            jobs[job_id]["logs"].append(msg)
            if len(jobs[job_id]["logs"]) > 600:
                jobs[job_id]["logs"] = jobs[job_id]["logs"][-600:]

    with jobs_lock:
        snap = dict(jobs[job_id])

    url          = snap["url"]
    filters      = snap.get("filters", {})
    cookies_file = snap.get("cookies_file")
    folder_name  = snap["folder_name"]
    base_dir     = get_download_dir()
    output_dir   = base_dir / folder_name

    # Make a read-only temp copy of the cookies file so yt-dlp cannot write back
    # updated tokens and corrupt the original.
    _tmp_cookie_path = None
    _src = None
    if cookies_file:
        _src = COOKIES_DIR / cookies_file
        if _src.exists():
            _candidate = _src.with_suffix(".tmp_dl.txt")
            # copyfile (not copy2) — we don't need the source's metadata, and
            # copy2's copystat step can raise FileNotFoundError on some mounts.
            try:
                shutil.copyfile(_src, _candidate)
                _tmp_cookie_path = _candidate
                cookies_file = _tmp_cookie_path.name   # point yt-dlp at the copy
            except Exception as _ce:
                # Fall back to the original cookies file rather than crashing the job
                log(f"[warn] 無法建立 cookies 暫存副本，改用原始檔：{_ce}")
                _tmp_cookie_path = None

    # Auto-migrate legacy folders whose name differs only by unsanitized chars
    # e.g. "Runway Chronicles#"  →  "Runway Chronicles"
    if not output_dir.exists():
        for existing in base_dir.iterdir():
            if existing.is_dir() and existing.name != folder_name:
                if sanitize_dirname(existing.name) == folder_name:
                    try:
                        existing.rename(output_dir)
                        log(f"[info] 已將舊資料夾 '{existing.name}' 重新命名為 '{folder_name}'")
                    except Exception as e:
                        log(f"[warn] 無法重新命名舊資料夾: {e}")
                    break

    # Read existing download archive to detect previously downloaded videos
    existing_archive_ids: set = set()
    archive_path = output_dir / ".ytdl_archive.txt"
    if archive_path.exists():
        try:
            with open(archive_path, "r", encoding="utf-8") as af:
                for line in af:
                    parts = line.strip().split(" ", 1)
                    if len(parts) == 2:
                        existing_archive_ids.add(parts[1])
        except Exception:
            pass
        if existing_archive_ids:
            with jobs_lock:
                jobs[job_id]["is_update"] = True
            log(f"[info] 發現已下載紀錄（{len(existing_archive_ids)} 部），將只下載新影片")

    # Check mode
    _s          = load_settings()
    check_mode  = _s.get("check_mode", "full")
    recent_count = int(_s.get("recent_count", 50))

    if check_mode == "fast":
        log("[info] 快速模式：略過清單讀取，直接下載（yt-dlp 以存檔紀錄自動略過已下載影片）")
        with jobs_lock:
            jobs[job_id]["status"]    = "running"
            jobs[job_id]["is_update"] = bool(existing_archive_ids)
            # Restore previous video list so the UI isn't blank
            jobs[job_id]["videos"]    = _prev_videos
            save_jobs()
    else:
        # Stage 1: prefetch video list
        if check_mode == "recent":
            log(f"[info] 最近模式：讀取最近 {recent_count} 部影片…")
            channel_name, total, entries = fetch_channel_info(url, cookies_file, max_items=recent_count)
        else:
            log("[info] 完整模式：正在讀取完整頻道清單…")
            channel_name, total, entries = fetch_channel_info(url, cookies_file)

    if check_mode in ("full", "recent"):
        # Sync titles: rename local folders/files for videos whose title changed
        renamed = 0
        for e in entries:
            vid_id    = e.get("id", "")
            new_title = e.get("title") or ""
            if vid_id and new_title and vid_id in _prev_videos:
                old_title = _prev_videos[vid_id].get("title", "")
                if old_title and old_title != new_title:
                    _sync_video_title(output_dir, vid_id, old_title, new_title, log)
                    renamed += 1
        if renamed:
            log(f"[info] 已同步 {renamed} 部影片的標題變更")

        with jobs_lock:
            jobs[job_id]["total_videos"] = total
            jobs[job_id]["channel_name"] = channel_name or jobs[job_id].get("channel_name")
            # For recent mode, also keep old videos not in the new slice
            if check_mode == "recent":
                for vid_id, v in _prev_videos.items():
                    if vid_id not in jobs[job_id]["videos"]:
                        jobs[job_id]["videos"][vid_id] = v
            for e in entries:
                vid_id = e.get("id", "")
                if vid_id:
                    init_status = "exists" if vid_id in existing_archive_ids else "pending"
                    jobs[job_id]["videos"][vid_id] = {
                        "id":        vid_id,
                        "title":     e.get("title") or vid_id,
                        "status":    init_status,
                        "error_msg": "",
                        "percent":   0,
                        "speed":     None,
                        "kind":      e.get("_kind", "video"),
                    }
            jobs[job_id]["status"] = "running"
            save_jobs()
        log(f"[info] 共 {total} 部影片，開始下載")

    # ── Resolution re-check (only after a quality change + update check) ──────────
    # When the channel's quality setting was changed, re-evaluate each already
    # downloaded video: if a higher, in-range resolution is obtainable than the
    # local file, delete it + drop it from the archive so Stage 2 re-downloads at
    # the new quality. Decision uses each video's stored info.json (the formats
    # recorded at download time) — no per-video network probing — falling back to
    # a probe only when that metadata is missing.
    _rc_quality = filters.get("quality", "best")
    if snap.get("quality_recheck") and _rc_quality != "audio_only":
        _cap = _quality_cap(_rc_quality)   # None = "best" → no upper limit
        with jobs_lock:
            _cand = [(vid, dict(v)) for vid, v in jobs[job_id]["videos"].items()
                     if len(vid) == 11]
        log(f"[info] 畫質重新檢查（目標：{_rc_quality}）：比對 {len(_cand)} 部影片…")
        _rechecked = 0
        for _vid_id, _v in _cand:
            if _stop_event.is_set():
                break
            _vid_folder = output_dir / sanitize_dirname(_v.get("title", ""))
            _info = read_info_json(_vid_folder)
            _local_h = _info.get("height")
            if not _local_h:
                continue   # not downloaded / no metadata → skip
            _avail_h = _stored_max_height(_info)
            if not _avail_h:
                _avail_h = _probe_max_height(f"https://www.youtube.com/watch?v={_vid_id}", cookies_file)
            if not _avail_h:
                continue
            _target = min(_cap, _avail_h) if _cap else _avail_h
            if _target <= _local_h:
                continue   # local already meets/exceeds the obtainable target → keep
            # A better in-range resolution exists → remove local copy + archive entry, re-queue
            try:
                if _vid_folder.exists():
                    shutil.rmtree(str(_vid_folder))
            except Exception as _e:
                log(f"[warn] 重抓時刪除失敗：{_vid_folder.name}：{_e}")
                continue
            _remove_from_archive(archive_path, _vid_id)
            try:
                _media_list_remove(str(_vid_folder.relative_to(base_dir)).replace("\\", "/"))
            except Exception:
                pass
            with jobs_lock:
                if _vid_id in jobs[job_id]["videos"]:
                    jobs[job_id]["videos"][_vid_id]["status"]    = "pending"
                    jobs[job_id]["videos"][_vid_id]["percent"]   = 0
                    jobs[job_id]["videos"][_vid_id]["error_msg"] = ""
            _rechecked += 1
            log(f"[info] 重抓畫質：{_v.get('title','')[:40]}（{_local_h}p → 目標 {_target}p）")
        if _rechecked:
            log(f"[info] 共 {_rechecked} 部影片將以新畫質重新下載")
        else:
            log("[info] 所有已下載影片皆已符合目標畫質，無需重抓")
        with jobs_lock:
            jobs[job_id]["quality_recheck"] = False
            save_jobs()

    # Stage 2: actual download
    exclude_kws = [k.strip() for k in filters.get("exclude_keywords", "").split(",") if k.strip()]
    require_kws = [k.strip() for k in filters.get("require_keywords", "").split(",") if k.strip()]
    min_dur   = int(filters["min_duration"]) if filters.get("min_duration") else None
    max_dur   = int(filters["max_duration"]) if filters.get("max_duration") else None
    min_views = int(filters["min_views"])    if filters.get("min_views")    else None

    finished_ids: set = set()
    _pp_timers:   dict = {}   # vid_id → threading.Timer for debounced per-video upsert

    def match_filter(info_dict, *, incomplete=False):
        vid_id = info_dict.get("id", "")
        title  = info_dict.get("title", "")
        with jobs_lock:
            if vid_id and vid_id not in jobs[job_id]["videos"]:
                jobs[job_id]["videos"][vid_id] = {
                    "id": vid_id, "title": title,
                    "status": "pending", "error_msg": "", "percent": 0, "speed": None,
                }
        # During the incomplete phase (playlist pre-scan), title is often empty.
        # Defer all text/metadata filters to the second call (incomplete=False)
        # when full info is available, so keyword checks are never applied to "".
        if incomplete:
            return None
        # Apply filters
        for kw in exclude_kws:
            if kw.lower() in title.lower():
                reason = f"跳過：標題含「{kw}」"
                _set_vid_status(vid_id, "skipped", reason, "err_exclude_kw", [kw])
                return reason
        if require_kws:
            if not any(kw.lower() in title.lower() for kw in require_kws):
                reason = f"跳過：標題未含必須關鍵字（{'、'.join(require_kws)}）"
                _set_vid_status(vid_id, "skipped", reason, "err_require_kw", ["、".join(require_kws)])
                return reason
        dur = info_dict.get("duration")
        if dur is not None:
            if min_dur and dur < min_dur:
                reason = f"跳過：時長 {int(dur)}s < {min_dur}s"
                _set_vid_status(vid_id, "skipped", reason, "err_too_short", [int(dur), min_dur]); return reason
            if max_dur and dur > max_dur:
                reason = f"跳過：時長 {int(dur)}s > {max_dur}s"
                _set_vid_status(vid_id, "skipped", reason, "err_too_long", [int(dur), max_dur]); return reason
        views = info_dict.get("view_count")
        if views is not None and min_views and views < min_views:
            reason = f"跳過：觀看數 {views} < {min_views}"
            _set_vid_status(vid_id, "skipped", reason, "err_too_few_views", [views, min_views]); return reason
        return None

    def _set_vid_status(vid_id, status, msg="", i18n_key=None, i18n_args=None):
        with jobs_lock:
            if vid_id and vid_id in jobs[job_id]["videos"]:
                jobs[job_id]["videos"][vid_id]["status"]    = status
                jobs[job_id]["videos"][vid_id]["error_msg"] = msg
                if i18n_key:
                    jobs[job_id]["videos"][vid_id]["error_i18n"] = {"key": i18n_key, "args": i18n_args or []}
                else:
                    jobs[job_id]["videos"][vid_id].pop("error_i18n", None)

    def on_progress(d):
        info_dict = d.get("info_dict", {})
        vid_id    = info_dict.get("id", "")

        if d["status"] == "downloading":
            total_b = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            dl_b    = d.get("downloaded_bytes", 0)
            pct     = int(dl_b / total_b * 100) if total_b else 0
            speed   = fmt_speed(d.get("speed"))
            with jobs_lock:
                if vid_id and vid_id in jobs[job_id]["videos"]:
                    v = jobs[job_id]["videos"][vid_id]
                    if v["status"] not in ("done", "error"):
                        v["status"]  = "downloading"
                        v["percent"] = pct
                        v["speed"]   = speed

        elif d["status"] == "finished":
            if vid_id and vid_id not in finished_ids:
                finished_ids.add(vid_id)
                with jobs_lock:
                    if vid_id in jobs[job_id]["videos"]:
                        jobs[job_id]["videos"][vid_id]["status"]  = "done"
                        jobs[job_id]["videos"][vid_id]["percent"] = 100
                        jobs[job_id]["videos"][vid_id]["speed"]   = None
                        jobs[job_id]["downloaded"] += 1

        elif d["status"] == "error":
            err = str(d.get("error", "未知錯誤"))
            with jobs_lock:
                jobs[job_id]["errors"] += 1
                if vid_id and vid_id in jobs[job_id]["videos"]:
                    jobs[job_id]["videos"][vid_id]["status"]    = "error"
                    jobs[job_id]["videos"][vid_id]["error_msg"] = err
                    jobs[job_id]["videos"][vid_id]["speed"]     = None

    def on_postprocess(d):
        """Fires after each yt-dlp postprocessor step (thumbnail, info.json, etc.).
        Debounced per vid_id: scans the video's folder 3 s after the LAST postprocessor
        finishes, so _media_list is updated incrementally without a full channel rescan."""
        if d["status"] != "finished":
            return
        info   = d.get("info_dict", {})
        vid_id = info.get("id", "")
        title  = info.get("title", "")
        if not vid_id or not title:
            return
        vid_dir = output_dir / sanitize_dirname(title)

        def _do_scan(vd=vid_dir):
            _pp_timers.pop(vid_id, None)
            try:
                _update_channel_media(vd)
            except Exception:
                pass

        prev = _pp_timers.pop(vid_id, None)
        if prev:
            prev.cancel()
        t = threading.Timer(3.0, _do_scan)
        t.daemon = True
        _pp_timers[vid_id] = t
        t.start()

    quality = filters.get("quality", "best")
    # Resolution-capped selection: take the highest-resolution video at or below
    # the chosen cap (best/4K/2K/1080p/480p); if only lower formats exist, take the
    # highest of those. YouTube only offers 4K/1440p as VP9 or AV1, never as
    # H.264/mp4 — so we must NOT restrict the video stream to ext=mp4 (that caps
    # quality at 1080p). Instead pick by height in any codec, then remux to mp4 via
    # format_sort/merge_output_format below. Audio prefers AAC (acodec^=mp4a) so the
    # merged mp4 plays everywhere; the acodec!=ec-3 fallback avoids EAC-3 / Dolby
    # Digital Plus which browsers cannot decode in HTML5 <video>.
    def _vfmt(cap):
        h = f"[height<={cap}]" if cap else ""
        return (f"bestvideo{h}+bestaudio[acodec^=mp4a]/"
                f"bestvideo{h}+bestaudio[acodec!=ec-3]/"
                f"bestvideo{h}+bestaudio/best{h}")
    format_map = {
        "best":       _vfmt(None),
        "4k":         _vfmt(2160),
        "2k":         _vfmt(1440),
        "1080p":      _vfmt(1080),
        "720p":       _vfmt(720),    # legacy jobs created before the option list changed
        "480p":       _vfmt(480),
        "audio_only": "bestaudio[acodec^=mp4a][ext=m4a]/bestaudio[ext=m4a]/bestaudio",
    }

    # Tracks the current video being downloaded so logger errors can be associated
    _cur = {"vid_id": None, "last_error": None}

    class MyLogger:
        def debug(self, msg):
            if not msg.startswith("[debug]"):
                log(msg)
        def info(self, msg):    log(msg)
        def warning(self, msg): log(f"[warning] {msg}")
        def error(self, msg):
            log(f"[error] {msg}")
            if _cur["vid_id"]:
                _cur["last_error"] = msg

    output_dir.mkdir(parents=True, exist_ok=True)

    sub_langs = settings.get("subtitle_langs", ["zh-Hant", "zh-Hans", "en", "ja"])
    dl_opts = {
        "format":            format_map.get(quality, format_map["best"]),
        # res first → always grab the highest resolution (incl. 4K). At the same
        # resolution prefer AV1, which lives natively in mp4 so the merge is clean.
        "format_sort":       ["res", "fps", "vcodec:av01"],
        "merge_output_format": "mp4",
        "outtmpl":           str(output_dir / "%(title)s" / "%(title)s.%(ext)s"),
        "match_filter":      match_filter,
        "ignoreerrors":      True,
        "logger":            MyLogger(),
        "progress_hooks":    [on_progress],
        "postprocessor_hooks": [on_postprocess],
        "download_archive":  str(output_dir / ".ytdl_archive.txt"),
        "writeinfojson":     True,
        "writethumbnail":    True,
        "writesubtitles":    bool(sub_langs),
        "writeautomaticsub": False,
        "subtitleslangs":    sub_langs if sub_langs else [],
        "subtitlesformat":   "vtt/best",
        "js_runtimes":       {"node": {}},
        "remote_components": ["ejs:github"],
    }

    after  = filters.get("date_after")
    before = filters.get("date_before")
    if after or before:
        dl_opts["daterange"] = yt_dlp.utils.DateRange(after or None, before or None)
    if filters.get("max_videos"):
        dl_opts["playlistend"] = int(filters["max_videos"])
    if cookies_file:
        p = COOKIES_DIR / cookies_file
        if p.exists():
            dl_opts["cookiefile"] = str(p)
            log(f"[info] 使用 cookies: {cookies_file}")

    # Build download URL list
    # Fast mode with existing list: reuse stored video IDs, retry errors
    # Fast mode without list: fall back to passing channel URL directly to yt-dlp
    # Full/Recent mode: use the pre-fetched per-video URLs
    if check_mode == "fast" and not _prev_videos:
        ordered_urls = [url]
    else:
        if check_mode == "fast":
            # Reset interrupted/errored videos so they get retried
            with jobs_lock:
                for v in jobs[job_id]["videos"].values():
                    if v.get("status") == "error":
                        v["status"]   = "pending"
                        v["error_msg"] = ""
                        v.pop("error_i18n", None)
        with jobs_lock:
            all_vid_items = list(jobs[job_id]["videos"].items())

        invalid_ids = [vid_id for vid_id, v in all_vid_items if len(vid_id) != 11]
        if invalid_ids:
            log(f"[info] 跳過 {len(invalid_ids)} 個無效 ID（非影片）：{', '.join(invalid_ids[:5])}")
            with jobs_lock:
                for vid_id in invalid_ids:
                    if vid_id in jobs[job_id]["videos"]:
                        jobs[job_id]["videos"][vid_id]["status"]     = "skipped"
                        jobs[job_id]["videos"][vid_id]["error_msg"]  = "非影片項目（頻道/播放清單 ID）"
                        jobs[job_id]["videos"][vid_id]["error_i18n"] = {"key": "err_invalid_id", "args": []}

        # Pre-filter by stored title before handing URLs to yt-dlp.
        # This avoids unnecessary API/auth calls (e.g. age-verification) for
        # videos that would be excluded by keyword filters anyway.
        for vid_id, v in all_vid_items:
            if len(vid_id) != 11 or v["status"] != "pending":
                continue
            title = v.get("title", "")
            for kw in exclude_kws:
                if kw.lower() in title.lower():
                    _set_vid_status(vid_id, "skipped", f"跳過：標題含「{kw}」", "err_exclude_kw", [kw])
                    break
            else:
                if require_kws and not any(kw.lower() in title.lower() for kw in require_kws):
                    _set_vid_status(vid_id, "skipped",
                                    f"跳過：標題未含必須關鍵字（{'、'.join(require_kws)}）",
                                    "err_require_kw", ["、".join(require_kws)])

        with jobs_lock:
            all_vid_items = list(jobs[job_id]["videos"].items())

        pending_ids = [vid_id for vid_id, v in all_vid_items
                       if len(vid_id) == 11 and v["status"] == "pending"]
        ordered_urls = [f"https://www.youtube.com/watch?v={vid_id}" for vid_id in pending_ids]

        if filters.get("max_videos"):
            ordered_urls = ordered_urls[:int(filters["max_videos"])]

    # Remove playlistend since we're controlling order/count ourselves
    dl_opts.pop("playlistend", None)

    dl_interval = max(5, int(settings.get("download_interval", 0))) \
                  if int(settings.get("download_interval", 0)) > 0 else 0

    try:
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            for i, video_url in enumerate(ordered_urls):
                if _stop_event.is_set():
                    log("[info] 使用者強制停止下載")
                    break
                # Interval between downloads (skip before first video)
                if dl_interval and i > 0:
                    log(f"[info] 等待 {dl_interval} 秒後繼續下載…")
                    for _ in range(dl_interval):
                        if _stop_event.is_set():
                            break
                        time.sleep(1)
                    if _stop_event.is_set():
                        log("[info] 使用者強制停止下載")
                        break
                vid_id = video_url.split("v=")[-1]
                _cur["vid_id"]    = vid_id
                _cur["last_error"] = None
                ydl.download([video_url])
                # If video is still "pending" after download, it silently failed
                # → use the captured logger error as the display message
                with jobs_lock:
                    v = jobs[job_id]["videos"].get(vid_id)
                    if v and v["status"] == "pending" and _cur["last_error"]:
                        v["status"]    = "error"
                        v["error_msg"] = _cur["last_error"][:200]
            _cur["vid_id"] = None
        # Read archive to distinguish truly-existing vs failed videos
        archive_ids: set = set()
        archive_path = output_dir / ".ytdl_archive.txt"
        if archive_path.exists():
            try:
                with open(archive_path, "r", encoding="utf-8") as af:
                    for line in af:
                        parts = line.strip().split(" ", 1)
                        if len(parts) == 2:
                            archive_ids.add(parts[1])
            except Exception:
                pass

        with jobs_lock:
            for v in jobs[job_id]["videos"].values():
                # Archive is the source of truth — rescue any "pending" or "error"
                # video that is actually recorded (e.g. error during info-fetch after
                # a successful prior download, or on_progress error hook mis-firing)
                if v["status"] in ("pending", "error"):
                    if v["id"] in archive_ids:
                        v["status"]    = "exists"
                        v["error_msg"] = ""
                    elif v["status"] == "pending":
                        v["status"]     = "error"
                        v["error_msg"]  = "格式不可用或下載失敗"
                        v["error_i18n"] = {"key": "err_format_unavailable", "args": []}
            jobs[job_id]["status"]      = "done"
            jobs[job_id]["finished_at"] = datetime.now().isoformat()
            save_jobs()
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"]      = "error"
            jobs[job_id]["logs"].append(f"[error] {e}")
            jobs[job_id]["finished_at"] = datetime.now().isoformat()
            save_jobs()
    finally:
        # Note: the concurrency-slot release + stop-flag cleanup + queue advance
        # live in _run_download_guarded's finally so they ALWAYS run, even if this
        # function crashes before reaching here (e.g. a prefetch/cookies error).
        # Update media list for this channel (only scans one channel folder, not all)
        try:
            _update_channel_media(output_dir)
        except Exception:
            pass
        # Copy refreshed tokens back to the original, then remove temp copy.
        # yt-dlp rotates OAuth tokens during download; discarding the temp file
        # would leave the original with stale tokens that YouTube rejects next time.
        if _tmp_cookie_path and _src and _tmp_cookie_path.exists():
            try:
                shutil.copyfile(_tmp_cookie_path, _src)
            except Exception:
                pass
            try:
                _tmp_cookie_path.unlink()
            except Exception:
                pass


def _run_download_guarded(job_id: str):
    """Always-release wrapper around run_download. The concurrency slot is taken by
    the caller before the thread starts; this guarantees it is given back (and the
    next queued job started) no matter how run_download exits — including an
    unhandled exception in the prefetch/cookies stage, which previously left the
    job stuck on 'prefetching' and deadlocked the queue forever."""
    global _running_count
    try:
        run_download(job_id)
    except Exception as e:
        with jobs_lock:
            j = jobs.get(job_id)
            if j:
                j["status"]      = "error"
                j.setdefault("logs", []).append(f"[error] 任務異常中止：{e}")
                j["finished_at"] = datetime.now().isoformat()
                save_jobs()
    finally:
        _stop_flags.pop(job_id, None)
        with _queue_lock:
            _running_count = max(0, _running_count - 1)
        _try_start_next()


# ── REST: Jobs ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    with jobs_lock:
        return jsonify(list(jobs.values()))


@app.route("/api/jobs", methods=["POST"])
def create_job():
    user = g.get("user", {})
    if not user.get("is_admin") and not user.get("can_add_channel"):
        return jsonify({"error": "forbidden"}), 403
    data = request.json or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    cookies_file = data.get("cookies_file") or None

    # Quick channel name fetch (just first entry)
    quick_opts = {"quiet": True, "skip_download": True,
                  "extract_flat": "in_playlist", "playlist_items": "1"}
    if cookies_file:
        p = COOKIES_DIR / cookies_file
        if p.exists():
            quick_opts["cookiefile"] = str(p)
    channel_name = None
    try:
        with yt_dlp.YoutubeDL(quick_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            channel_name = (
                info.get("uploader") or info.get("channel") or
                info.get("playlist_uploader") or info.get("title")
            )
    except Exception:
        pass

    folder_name = sanitize_dirname(channel_name or str(uuid.uuid4())[:8])
    job_id = str(uuid.uuid4())[:8]
    job = {
        "id":           job_id,
        "url":          url,
        "name":         data.get("name") or channel_name or url,
        "channel_name": channel_name,
        "folder_name":  folder_name,
        "filters":      data.get("filters") or {},
        "cookies_file": cookies_file,
        "status":       "pending",
        "created_at":   datetime.now().isoformat(),
        "finished_at":  None,
        "total_videos": None,
        "downloaded":   0,
        "errors":       0,
        "logs":         [],
        "videos":       {},
    }
    with jobs_lock:
        jobs[job_id] = job
        save_jobs()

    if data.get("start", True):
        _enqueue_or_start(job_id)

    return jsonify(job), 201


@app.route("/api/jobs/<job_id>", methods=["PATCH"])
def patch_job(job_id):
    data = request.json or {}
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        if "cookies_file" in data:
            job["cookies_file"] = data["cookies_file"] or None
        if "quality" in data:
            new_q = data["quality"] or "best"
            old_q = (job.get("filters") or {}).get("quality", "best")
            job.setdefault("filters", {})["quality"] = new_q
            # Flag so the next update check re-evaluates already-downloaded videos
            # against the new quality (see run_download resolution re-check).
            if new_q != old_q:
                job["quality_recheck"] = True
        save_jobs()
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.route("/api/jobs/<job_id>/start", methods=["POST"])
def start_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    if job["status"] in ("running", "prefetching", "queued"):
        return jsonify({"error": "already running or queued"}), 400
    _enqueue_or_start(job_id)
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/stop", methods=["POST"])
def stop_job(job_id):
    was_queued = False
    with _queue_lock:
        if job_id in _job_queue:
            _job_queue.remove(job_id)
            was_queued = True
    flag = _stop_flags.get(job_id)
    if flag:
        flag.set()
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            if was_queued or job["status"] == "queued":
                job["status"] = "pending"
            elif job["status"] in ("running", "prefetching"):
                job["status"]      = "error"
                job["finished_at"] = datetime.now().isoformat()
                job["logs"].append("[info] 使用者強制停止")
            save_jobs()
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    with _queue_lock:
        if job_id in _job_queue:
            _job_queue.remove(job_id)
    with jobs_lock:
        if job_id not in jobs:
            return jsonify({"error": "not found"}), 404
        del jobs[job_id]
        save_jobs()
    return jsonify({"ok": True})


# ── REST: Media browser ───────────────────────────────────────────────────────

def _cached_entry(base: Path, d: Path, files: list,
                  cache_key: str, mtime: float,
                  title_override: str | None) -> tuple[dict | None, bool]:
    """Return (entry, dirty) — dirty=True means cache was written."""
    with _media_cache_lock:
        cached = _media_cache.get(cache_key)
    if cached and cached.get("mtime") == mtime:
        return cached["entry"], False          # cache hit
    entry = _make_entry(base, d, files, title_override)
    if entry:
        with _media_cache_lock:
            _media_cache[cache_key] = {"mtime": mtime, "entry": entry}
    return entry, True                         # cache miss → recomputed


def _make_entry(base: Path, d: Path, files: list, title_override: str | None = None) -> dict | None:
    """Build a single result entry from a list of media files in directory d."""
    video_files = sorted([f for f in files if f.suffix.lower() in VIDEO_EXTS])
    audio_files = sorted([f for f in files if f.suffix.lower() in AUDIO_EXTS])
    merged      = [f for f in video_files if not re.search(r'\.f\d{2,4}$', f.stem)]
    best        = (merged or video_files or audio_files or [None])[0]
    if not best:
        return None

    try:
        rel_parts = d.relative_to(base).parts
    except ValueError:
        return None

    channel = rel_parts[0] if rel_parts else ""
    if title_override:
        title = title_override
    elif len(rel_parts) >= 2:
        title = rel_parts[-1]          # use innermost directory name
    else:
        title = re.sub(r'\.f\d{2,4}$', '', best.stem)

    thumb = find_thumbnail(d)
    info  = read_info_json(d)

    total_size = sum(f.stat().st_size for f in files if f.is_file() and f.exists())
    h = info.get("height")
    w = info.get("width")
    try:
        modified = datetime.fromtimestamp(best.stat().st_mtime).isoformat()
    except FileNotFoundError:
        return None

    # Detect subtitle files: yt-dlp names them "<title>.<lang>.vtt"
    subtitles: dict[str, str] = {}
    for vtt in sorted(d.glob("*.vtt")):
        parts = vtt.stem.rsplit(".", 1)
        if len(parts) == 2 and parts[1]:
            subtitles[parts[1]] = str(vtt.relative_to(base)).replace("\\", "/")

    return {
        "title":        title,
        "filename":     best.name,
        "ext":          best.suffix.lower(),
        "channel":      channel,
        "size":         total_size,
        "size_fmt":     fmt_size(total_size),
        "unmerged":     bool(video_files and audio_files and not merged),
        "modified":     modified,
        "upload_date":  info.get("upload_date", ""),
        "duration":     info.get("duration"),
        "duration_fmt": fmt_duration(info.get("duration")),
        "height":       h,
        "width":        w,
        "resolution":   f"{w}×{h}" if w and h else (f"{h}p" if h else None),
        "rel_path":     str(best.relative_to(base)).replace("\\", "/"),
        "dir_rel":      str(d.relative_to(base)).replace("\\", "/"),
        "thumbnail":    str(thumb.relative_to(base)).replace("\\", "/") if thumb else None,
        "subtitles":    subtitles,
    }


def scan_videos(folder_filter="", search="", sort_by="date", sort_asc=False):
    """
    Recursively scan the download directory for video/audio files.
    Handles all layouts:
      base/file.mp4                        → flat
      base/Channel/file.mp4                → channel-flat
      base/Channel/VideoTitle/file.mp4     → our app's 3-level structure
    """
    base        = get_download_dir()
    search_root = (base / folder_filter) if folder_filter else base
    if not search_root.exists():
        return []

    # Group media files by their containing directory
    dir_files: dict[Path, list[Path]] = {}
    for f in search_root.rglob("*"):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS:
            dir_files.setdefault(f.parent, []).append(f)

    results   = []
    seen_keys: set[str] = set()
    dirty     = False

    for d, files in sorted(dir_files.items()):
        try:
            dir_mtime = d.stat().st_mtime
        except OSError:
            continue

        # Group files within the same directory by "clean stem"
        # (strip format-IDs like .f299 so video+audio of same clip count as one)
        stem_groups: dict[str, list[Path]] = {}
        for f in files:
            clean = re.sub(r'\.f\d{2,4}$', '', f.stem)
            stem_groups.setdefault(clean, []).append(f)

        if len(stem_groups) == 1:
            # All files belong to one clip (our 3-level structure, or single-file dir)
            cache_key = str(d.relative_to(base)).replace("\\", "/")
            seen_keys.add(cache_key)
            entry, updated = _cached_entry(base, d, files, cache_key, dir_mtime, None)
            if updated:
                dirty = True
            if entry:
                if not search or search.lower() in entry["title"].lower():
                    results.append(entry)
        else:
            # Flat directory: multiple unrelated clips → one entry per unique stem
            for stem, stem_files in stem_groups.items():
                cache_key = str(d.relative_to(base)).replace("\\", "/") + "\x00" + stem
                seen_keys.add(cache_key)
                try:
                    stem_mtime = max(f.stat().st_mtime for f in stem_files)
                except OSError:
                    stem_mtime = dir_mtime
                entry, updated = _cached_entry(base, d, stem_files, cache_key, stem_mtime, stem)
                if updated:
                    dirty = True
                if entry:
                    if not search or search.lower() in entry["title"].lower():
                        results.append(entry)

    # Remove stale cache entries (directories that no longer exist) on full scans
    if not folder_filter:
        with _media_cache_lock:
            stale = [k for k in _media_cache if k not in seen_keys]
            for k in stale:
                del _media_cache[k]
            if stale:
                dirty = True

    if dirty:
        _save_media_cache()

    # Update in-memory list on full scans (no filter/search) so /api/media can serve instantly
    if not folder_filter and not search:
        with _media_list_lock:
            _media_list[:] = results

    return results


@app.route("/api/media", methods=["GET"])
def list_media():
    """Return in-memory list instantly. Triggers a full scan only on cold start (no cache)."""
    with _media_list_lock:
        has_data = bool(_media_list)
    if not has_data:
        scan_videos()
    with _media_list_lock:
        data = list(_media_list)
    user = g.get("user", {})
    if not user.get("is_admin") and AUTH_ENABLED:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT channel_name FROM user_channel_access WHERE user_id = %s", (user["sub"],))
            allowed = {r["channel_name"] for r in cur.fetchall()}
        data = [e for e in data if e.get("channel") in allowed]
    return jsonify(data)


@app.route("/api/media/refresh", methods=["POST"])
@require_admin
def refresh_media():
    """Force a full rglob scan, rebuild list and cache, return updated list."""
    scan_videos()
    with _media_list_lock:
        data = list(_media_list)
    return jsonify(data)


@app.route("/api/media/folders", methods=["GET"])
def list_folders():
    user = g.get("user", {})
    folders = [d.name for d in sorted(get_download_dir().iterdir()) if d.is_dir()]
    if not user.get("is_admin") and AUTH_ENABLED:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT channel_name FROM user_channel_access WHERE user_id = %s", (user["sub"],))
            allowed = {r["channel_name"] for r in cur.fetchall()}
        folders = [f for f in folders if f in allowed]
    return jsonify(folders)


STOP_WORDS = {
    "the","a","an","and","or","of","in","at","to","for","is","it","its",
    "with","this","that","was","are","be","been","by","from","on","as",
    "no","not","full","show","video","official","hd","4k","1080p","720p",
    "ep","episode","part","vol","ft","feat","feat",
}

def _word_tokens(title: str) -> frozenset:
    n = normalize_title(title)
    return frozenset(w for w in n.split() if len(w) >= 3 and w not in STOP_WORDS)


@app.route("/api/media/similar", methods=["GET"])
def similar_files():
    files = scan_videos(sort_by="name", sort_asc=True)
    n = len(files)
    if n < 2:
        return jsonify({"groups": [], "total_scanned": n})

    # Step 1 – tokenise every title
    tokens = [_word_tokens(f["title"]) for f in files]

    # Step 2 – inverted index: word → [file indices]
    inverted: dict[str, list[int]] = {}
    for i, ts in enumerate(tokens):
        for t in ts:
            inverted.setdefault(t, []).append(i)

    # Step 3 – candidate pairs that share ≥1 word
    # Skip words that appear in >5 % of files (too generic)
    max_freq = max(5, n // 20)
    candidate_pairs: set[tuple[int, int]] = set()
    for word, idxs in inverted.items():
        if len(idxs) > max_freq:
            continue
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                candidate_pairs.add((idxs[a], idxs[b]))

    # Step 4 – Jaccard similarity on candidates  (threshold ≥ 0.35)
    THRESHOLD = 0.35
    similar_pairs: list[tuple[int, int]] = []
    for i, j in candidate_pairs:
        t1, t2 = tokens[i], tokens[j]
        if not t1 or not t2:
            continue
        jaccard = len(t1 & t2) / len(t1 | t2)
        if jaccard >= THRESHOLD:
            similar_pairs.append((i, j))

    # Step 5 – Union-Find grouping (transitive closure)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i, j in similar_pairs:
        union(i, j)

    # Step 6 – collect groups of ≥2
    buckets: dict[int, list] = {}
    for i, f in enumerate(files):
        buckets.setdefault(find(i), []).append(f)

    groups = [g for g in buckets.values() if len(g) >= 2]
    groups.sort(key=lambda g: -len(g))          # largest groups first

    return jsonify({"groups": groups[:100], "total_scanned": n})


@app.route("/api/media/move", methods=["POST"])
def move_media():
    data       = request.json or {}
    rel_path   = data.get("rel_path", "")
    new_folder = sanitize_dirname(data.get("new_folder", "").strip())
    if not rel_path or not new_folder:
        return jsonify({"error": "rel_path and new_folder required"}), 400
    dl = get_download_dir()
    src = dl / rel_path.replace("/", os.sep)
    if not src.exists():
        return jsonify({"error": "file not found"}), 404
    # Move entire video directory to new channel folder
    vid_dir  = src.parent
    dest_dir = dl / new_folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / vid_dir.name
    if dest.exists():
        dest = dest_dir / (vid_dir.name + "_moved")
    old_dir_rel = str(vid_dir.relative_to(dl)).replace("\\", "/")
    shutil.move(str(vid_dir), str(dest))
    # Remove old entry; add new entry for the destination
    _media_list_remove(old_dir_rel)
    with _media_cache_lock:
        stale = [k for k in _media_cache if k == old_dir_rel or k.startswith(old_dir_rel + "\x00")]
        for k in stale:
            del _media_cache[k]
    _update_channel_media(dest)
    new_file = dest / src.name
    return jsonify({"ok": True, "new_path": str(new_file.relative_to(dl)).replace("\\", "/")})


@app.route("/api/media/delete", methods=["POST"])
def delete_media():
    data     = request.json or {}
    rel_path = data.get("rel_path", "")
    if not rel_path:
        return jsonify({"error": "rel_path required"}), 400
    path = get_download_dir() / rel_path.replace("/", os.sep)
    # Delete the entire video directory
    vid_dir = path.parent if path.is_file() else path
    dl      = get_download_dir()
    dir_rel = str(vid_dir.relative_to(dl)).replace("\\", "/")
    if vid_dir.exists():
        shutil.rmtree(str(vid_dir))
    _media_list_remove(dir_rel)
    with _media_cache_lock:
        stale = [k for k in _media_cache if k == dir_rel or k.startswith(dir_rel + "\x00")]
        for k in stale:
            del _media_cache[k]
    _save_media_cache()
    return jsonify({"ok": True})


@app.route("/api/media/rename", methods=["POST"])
def rename_media():
    data     = request.json or {}
    rel_path = data.get("rel_path", "")
    new_name = sanitize_dirname(data.get("new_name", "").strip())
    if not rel_path or not new_name:
        return jsonify({"error": "rel_path and new_name required"}), 400
    dl   = get_download_dir()
    path = dl / rel_path.replace("/", os.sep)
    if not path.exists():
        return jsonify({"error": "file not found"}), 404
    # Rename the video directory
    vid_dir     = path.parent
    old_dir_rel = str(vid_dir.relative_to(dl)).replace("\\", "/")
    new_dir     = vid_dir.parent / new_name
    vid_dir.rename(new_dir)
    _media_list_remove(old_dir_rel)
    with _media_cache_lock:
        stale = [k for k in _media_cache if k == old_dir_rel or k.startswith(old_dir_rel + "\x00")]
        for k in stale:
            del _media_cache[k]
    _update_channel_media(new_dir)
    new_file = new_dir / path.name
    return jsonify({"ok": True, "new_path": str(new_file.relative_to(dl)).replace("\\", "/")})


# ── REST: Cookies ─────────────────────────────────────────────────────────────

@app.route("/api/cookies", methods=["GET"])
def list_cookies():
    return jsonify([
        f.name for f in COOKIES_DIR.iterdir()
        if f.is_file() and f.suffix == ".txt" and not f.name.endswith(".tmp_dl.txt")
    ])


@app.route("/api/cookies", methods=["POST"])
def upload_cookies():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if not f.filename.endswith(".txt"):
        return jsonify({"error": "只接受 .txt 檔案"}), 400
    f.save(str(COOKIES_DIR / f.filename))
    return jsonify({"ok": True, "filename": f.filename})


@app.route("/api/cookies/<filename>", methods=["DELETE"])
def delete_cookies(filename):
    p = COOKIES_DIR / filename
    if p.exists():
        p.unlink()
    return jsonify({"ok": True})


# ── REST: Settings ───────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings_api():
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def update_settings():
    global settings
    data = request.json or {}

    new_dir = data.get("download_dir", "").strip()
    if not new_dir:
        return jsonify({"error": "download_dir is required"}), 400

    path = Path(new_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"無法建立資料夾：{e}"}), 400

    settings["download_dir"] = str(path.absolute())
    if "check_mode" in data and data["check_mode"] in ("full", "fast", "recent"):
        settings["check_mode"] = data["check_mode"]
    if "recent_count" in data:
        try:
            settings["recent_count"] = max(1, int(data["recent_count"]))
        except (ValueError, TypeError):
            pass
    if "download_interval" in data:
        try:
            v = int(data["download_interval"])
            settings["download_interval"] = max(5, v) if v > 0 else 0
        except (ValueError, TypeError):
            pass
    if "max_concurrent_channels" in data:
        try:
            v = int(data["max_concurrent_channels"])
            if v < 0:
                return jsonify({"error": "cannot be negative"}), 400
            if v > 0:
                with jobs_lock:
                    job_count = len(jobs)
                if job_count > 0 and v > job_count:
                    return jsonify({"error": f"cannot exceed channel count ({job_count})"}), 400
            settings["max_concurrent_channels"] = v
            # If limit relaxed, try to start queued jobs
            _try_start_next()
        except (ValueError, TypeError):
            pass
    if "schedule" in data:
        s = data["schedule"]
        if isinstance(s, dict):
            try:
                days = [int(d) for d in s.get("days", []) if 0 <= int(d) <= 6]
                settings["schedule"] = {
                    "enabled": bool(s.get("enabled", False)),
                    "days":    days,
                    "time":    s.get("time", "12:00"),
                }
            except (ValueError, TypeError):
                pass
    if "subtitle_langs" in data and isinstance(data["subtitle_langs"], list):
        settings["subtitle_langs"] = [str(l) for l in data["subtitle_langs"] if isinstance(l, str) and str(l).strip()]
    save_settings(settings)
    return jsonify({"ok": True, "settings": settings})


# ── Serve downloaded files (thumbnails etc.) ──────────────────────────────────

@app.route("/dl/<path:filepath>")
def serve_dl(filepath):
    return send_from_directory(str(get_download_dir().absolute()), filepath)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    if not AUTH_ENABLED:
        return jsonify({"error": "auth disabled"}), 503
    data     = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password =  data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "missing fields"}), 400
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return jsonify({"error": "帳號或密碼錯誤"}), 401
    return jsonify({
        "token": _make_token(user),
        "user": {
            "id":              user["id"],
            "username":        user["username"],
            "is_admin":        user["is_admin"],
            "can_add_channel": user["can_add_channel"],
        }
    })


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    return jsonify(g.user)


_ALLOWED_SETTING_KEYS = {"lang", "fpMode", "fpMute", "fpVolume", "fpEnabled", "subtitlePriority"}

@app.route("/api/auth/settings", methods=["GET"])
def get_user_settings():
    uid = g.user["sub"]
    db  = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT settings FROM users WHERE id = %s", (uid,))
        row = cur.fetchone()
    return jsonify(row["settings"] if row else {})


@app.route("/api/auth/settings", methods=["PUT"])
def put_user_settings():
    data = request.get_json() or {}
    # Only allow whitelisted keys
    clean = {k: v for k, v in data.items() if k in _ALLOWED_SETTING_KEYS}
    uid  = g.user["sub"]
    db   = get_db()
    with db.cursor() as cur:
        cur.execute("""
            UPDATE users
            SET settings = settings || %s::jsonb
            WHERE id = %s
        """, (json.dumps(clean), uid))
    db.commit()
    return jsonify({"ok": True})


# ── User management routes (admin only) ───────────────────────────────────────

@app.route("/api/users", methods=["GET"])
@require_admin
def list_users():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT id, username, is_admin, can_add_channel, created_at FROM users ORDER BY id")
        rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/users", methods=["POST"])
@require_admin
def create_user():
    data     = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password =  data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "missing fields"}), 400
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin, can_add_channel) VALUES (%s,%s,%s,%s) RETURNING id",
                (username, pw_hash, False, False)
            )
            new_id = cur.fetchone()["id"]
        db.commit()
        return jsonify({"id": new_id}), 201
    except psycopg2.errors.UniqueViolation:
        db.rollback()
        return jsonify({"error": "帳號已存在"}), 409


@app.route("/api/users/<int:uid>", methods=["PATCH"])
@require_admin
def update_user(uid):
    data = request.get_json() or {}
    db   = get_db()
    with db.cursor() as cur:
        if data.get("password"):
            pw_hash = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt()).decode()
            cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (pw_hash, uid))
        if "is_admin" in data:
            cur.execute("UPDATE users SET is_admin=%s WHERE id=%s", (bool(data["is_admin"]), uid))
        if "can_add_channel" in data:
            cur.execute("UPDATE users SET can_add_channel=%s WHERE id=%s", (bool(data["can_add_channel"]), uid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@require_admin
def delete_user(uid):
    if uid == g.user.get("sub"):
        return jsonify({"error": "無法刪除自己"}), 400
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id=%s", (uid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>/channels", methods=["GET"])
@require_admin
def get_user_channels(uid):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT channel_name FROM user_channel_access WHERE user_id=%s ORDER BY channel_name", (uid,))
        channels = [r["channel_name"] for r in cur.fetchall()]
    return jsonify(channels)


@app.route("/api/users/<int:uid>/channels", methods=["PUT"])
@require_admin
def set_user_channels(uid):
    channels = request.get_json()
    if not isinstance(channels, list):
        return jsonify({"error": "expected list"}), 400
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM user_channel_access WHERE user_id=%s", (uid,))
        for ch in channels:
            if ch:
                cur.execute(
                    "INSERT INTO user_channel_access (user_id, channel_name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (uid, ch)
                )
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
