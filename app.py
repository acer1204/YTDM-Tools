import os, json, shutil, threading, uuid, re
from pathlib import Path
from datetime import datetime
from difflib import SequenceMatcher
from flask import Flask, request, jsonify, send_from_directory
import yt_dlp

app = Flask(__name__, static_folder="static")

DATA_DIR      = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_FILE     = DATA_DIR / "jobs.json"
COOKIES_DIR   = DATA_DIR / "cookies"
SETTINGS_FILE = DATA_DIR / "settings.json"
COOKIES_DIR.mkdir(exist_ok=True)

jobs: dict = {}
jobs_lock = threading.Lock()

# ── Settings ──────────────────────────────────────────────────────────────────

_DEFAULT_SETTINGS = {
    "download_dir": os.environ.get("DOWNLOAD_DIR", str(Path("downloads").absolute())),
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


jobs = load_jobs()


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


def _fetch_flat(url: str, opts: dict) -> tuple[list, dict]:
    """Return (entries, info_dict) from a flat extraction."""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return [], {}
        entries = info.get("entries") or []
        if not entries and info.get("id"):
            entries = [info]
        return entries, info
    except Exception:
        return [], {}


def fetch_channel_info(url: str, cookies_file: str | None):
    """Return (channel_name, total_count, entries_list).
    For channel root URLs, fetches /videos and /shorts tabs separately and merges.
    Fetching the root URL only returns tab-level playlist stubs, not actual videos.
    """
    opts = {
        "quiet":        True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
    }
    if cookies_file:
        p = COOKIES_DIR / cookies_file
        if p.exists():
            opts["cookiefile"] = str(p)

    channel_name = None

    if _is_channel_url(url):
        base = url.rstrip("/")
        all_entries: list = []
        seen_ids: set = set()

        for tab in ("/videos", "/shorts"):
            kind = "short" if tab == "/shorts" else "video"
            tab_entries, tab_info = _fetch_flat(base + tab, opts)
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


def run_download(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["status"] = "prefetching"
        job["logs"] = []
        job["downloaded"] = 0
        job["errors"] = 0
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

    # Stage 1: prefetch video list
    log("[info] 正在讀取頻道資訊…")
    channel_name, total, entries = fetch_channel_info(url, cookies_file)

    with jobs_lock:
        jobs[job_id]["total_videos"] = total
        jobs[job_id]["channel_name"] = channel_name or jobs[job_id].get("channel_name")
        for e in entries:
            vid_id = e.get("id", "")
            if vid_id:
                # Mark immediately as exists if already in archive
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

    # Stage 2: actual download
    exclude_kws = [k.strip() for k in filters.get("exclude_keywords", "").split(",") if k.strip()]
    require_kws = [k.strip() for k in filters.get("require_keywords", "").split(",") if k.strip()]
    min_dur   = int(filters["min_duration"]) if filters.get("min_duration") else None
    max_dur   = int(filters["max_duration"]) if filters.get("max_duration") else None
    min_views = int(filters["min_views"])    if filters.get("min_views")    else None

    finished_ids: set = set()

    def match_filter(info_dict, *, incomplete=False):
        vid_id = info_dict.get("id", "")
        title  = info_dict.get("title", "")
        with jobs_lock:
            if vid_id and vid_id not in jobs[job_id]["videos"]:
                jobs[job_id]["videos"][vid_id] = {
                    "id": vid_id, "title": title,
                    "status": "pending", "error_msg": "", "percent": 0, "speed": None,
                }
        # Apply filters
        for kw in exclude_kws:
            if kw.lower() in title.lower():
                reason = f"跳過：標題含「{kw}」"
                _set_vid_status(vid_id, "skipped", reason)
                return reason
        if require_kws:
            if not any(kw.lower() in title.lower() for kw in require_kws):
                reason = f"跳過：標題未含必須關鍵字（{'、'.join(require_kws)}）"
                _set_vid_status(vid_id, "skipped", reason)
                return reason
        dur = info_dict.get("duration")
        if dur is not None:
            if min_dur and dur < min_dur:
                reason = f"跳過：時長 {int(dur)}s < {min_dur}s"
                _set_vid_status(vid_id, "skipped", reason); return reason
            if max_dur and dur > max_dur:
                reason = f"跳過：時長 {int(dur)}s > {max_dur}s"
                _set_vid_status(vid_id, "skipped", reason); return reason
        views = info_dict.get("view_count")
        if views is not None and min_views and views < min_views:
            reason = f"跳過：觀看數 {views} < {min_views}"
            _set_vid_status(vid_id, "skipped", reason); return reason
        return None

    def _set_vid_status(vid_id, status, msg=""):
        with jobs_lock:
            if vid_id and vid_id in jobs[job_id]["videos"]:
                jobs[job_id]["videos"][vid_id]["status"]    = status
                jobs[job_id]["videos"][vid_id]["error_msg"] = msg

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

    quality = filters.get("quality", "best")
    format_map = {
        "best":       "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "720p":       "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
        "480p":       "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
        "audio_only": "bestaudio[ext=m4a]/bestaudio",
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

    dl_opts = {
        "format":            format_map.get(quality, "best"),
        "outtmpl":           str(output_dir / "%(title)s" / "%(title)s.%(ext)s"),
        "match_filter":      match_filter,
        "ignoreerrors":      True,
        "logger":            MyLogger(),
        "progress_hooks":    [on_progress],
        "download_archive":  str(output_dir / ".ytdl_archive.txt"),
        "writeinfojson":     True,
        "writethumbnail":    True,
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

    # Build ordered URL list from prefetch
    # - Skip invalid IDs (not exactly 11 chars — channel/playlist IDs)
    # - Skip already-archived videos (marked "exists") to avoid redundant network checks
    with jobs_lock:
        all_vid_items = list(jobs[job_id]["videos"].items())

    invalid_ids = [vid_id for vid_id, v in all_vid_items if len(vid_id) != 11]
    if invalid_ids:
        log(f"[info] 跳過 {len(invalid_ids)} 個無效 ID（非影片）：{', '.join(invalid_ids[:5])}")
        with jobs_lock:
            for vid_id in invalid_ids:
                if vid_id in jobs[job_id]["videos"]:
                    jobs[job_id]["videos"][vid_id]["status"] = "skipped"
                    jobs[job_id]["videos"][vid_id]["error_msg"] = "非影片項目（頻道/播放清單 ID）"

    # Only queue "pending" videos — "exists" ones are already in archive, no need to re-check
    pending_ids = [vid_id for vid_id, v in all_vid_items
                   if len(vid_id) == 11 and v["status"] == "pending"]
    ordered_urls = [f"https://www.youtube.com/watch?v={vid_id}" for vid_id in pending_ids]

    # Apply max_videos limit on ordered list
    if filters.get("max_videos"):
        ordered_urls = ordered_urls[:int(filters["max_videos"])]

    # Remove playlistend since we're controlling order/count ourselves
    dl_opts.pop("playlistend", None)

    try:
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            for video_url in ordered_urls:
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
                        v["status"]    = "error"
                        v["error_msg"] = "格式不可用或下載失敗"
            jobs[job_id]["status"]      = "done"
            jobs[job_id]["finished_at"] = datetime.now().isoformat()
            save_jobs()
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"]      = "error"
            jobs[job_id]["logs"].append(f"[error] {e}")
            jobs[job_id]["finished_at"] = datetime.now().isoformat()
            save_jobs()


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
        threading.Thread(target=run_download, args=(job_id,), daemon=True).start()

    return jsonify(job), 201


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
    if job["status"] == "running":
        return jsonify({"error": "already running"}), 400
    threading.Thread(target=run_download, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    with jobs_lock:
        if job_id not in jobs:
            return jsonify({"error": "not found"}), 404
        del jobs[job_id]
        save_jobs()
    return jsonify({"ok": True})


# ── REST: Media browser ───────────────────────────────────────────────────────

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

    total_size = sum(f.stat().st_size for f in files if f.is_file())
    h = info.get("height")
    w = info.get("width")

    return {
        "title":        title,
        "filename":     best.name,
        "ext":          best.suffix.lower(),
        "channel":      channel,
        "size":         total_size,
        "size_fmt":     fmt_size(total_size),
        "unmerged":     bool(video_files and audio_files and not merged),
        "modified":     datetime.fromtimestamp(best.stat().st_mtime).isoformat(),
        "upload_date":  info.get("upload_date", ""),
        "duration":     info.get("duration"),
        "duration_fmt": fmt_duration(info.get("duration")),
        "height":       h,
        "width":        w,
        "resolution":   f"{w}×{h}" if w and h else (f"{h}p" if h else None),
        "rel_path":     str(best.relative_to(base)).replace("\\", "/"),
        "dir_rel":      str(d.relative_to(base)).replace("\\", "/"),
        "thumbnail":    str(thumb.relative_to(base)).replace("\\", "/") if thumb else None,
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

    results = []
    for d, files in sorted(dir_files.items()):
        # Group files within the same directory by "clean stem"
        # (strip format-IDs like .f299 so video+audio of same clip count as one)
        stem_groups: dict[str, list[Path]] = {}
        for f in files:
            clean = re.sub(r'\.f\d{2,4}$', '', f.stem)
            stem_groups.setdefault(clean, []).append(f)

        if len(stem_groups) == 1:
            # All files belong to one clip (our 3-level structure, or single-file dir)
            entry = _make_entry(base, d, files)
            if entry:
                if not search or search.lower() in entry["title"].lower():
                    results.append(entry)
        else:
            # Flat directory: multiple unrelated clips → one entry per unique stem
            for stem, stem_files in stem_groups.items():
                entry = _make_entry(base, d, stem_files, title_override=stem)
                if entry:
                    if not search or search.lower() in entry["title"].lower():
                        results.append(entry)

    if sort_by == "name":
        results.sort(key=lambda x: x["title"].lower(), reverse=not sort_asc)
    elif sort_by == "date":
        results.sort(key=lambda x: x["upload_date"] or x["modified"], reverse=not sort_asc)
    elif sort_by == "size":
        results.sort(key=lambda x: x["size"], reverse=not sort_asc)

    return results


@app.route("/api/media", methods=["GET"])
def list_media():
    folder   = request.args.get("folder", "")
    search   = request.args.get("search", "")
    sort_by  = request.args.get("sort", "date")
    sort_asc = request.args.get("asc", "false").lower() == "true"
    return jsonify(scan_videos(folder, search, sort_by, sort_asc))


@app.route("/api/media/folders", methods=["GET"])
def list_folders():
    folders = [d.name for d in sorted(get_download_dir().iterdir()) if d.is_dir()]
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
    shutil.move(str(vid_dir), str(dest))
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
    if vid_dir.exists():
        shutil.rmtree(str(vid_dir))
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
    vid_dir  = path.parent
    new_dir  = vid_dir.parent / new_name
    vid_dir.rename(new_dir)
    new_file = new_dir / path.name
    return jsonify({"ok": True, "new_path": str(new_file.relative_to(dl)).replace("\\", "/")})


# ── REST: Cookies ─────────────────────────────────────────────────────────────

@app.route("/api/cookies", methods=["GET"])
def list_cookies():
    return jsonify([f.name for f in COOKIES_DIR.iterdir() if f.is_file() and f.suffix == ".txt"])


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
    save_settings(settings)
    return jsonify({"ok": True, "settings": settings})


# ── Serve downloaded files (thumbnails etc.) ──────────────────────────────────

@app.route("/dl/<path:filepath>")
def serve_dl(filepath):
    return send_from_directory(str(get_download_dir().absolute()), filepath)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
