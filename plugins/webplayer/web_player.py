import json
import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import requests as req_lib

import db
import health_cache
import settings as _settings
import subtitles as _subtitles
import torbox
import torrentio
import zilean

log = logging.getLogger(__name__)

PLAYER_TMP_DIR       = Path("/tmp/mycelium-player")
SEGMENT_WAIT_COUNT   = 3
SEGMENT_WAIT_TIMEOUT = 90
SESSION_IDLE_CLEANUP = 1800
CDN_URL_MAX_AGE_S    = 1800   # refresh TorBox signed URL after 30 minutes

_BROWSER_AUDIO_OK    = {"aac"}   # vorbis/opus not reliable in mpegts HLS
_NO_BROWSER_VIDEO_RE = re.compile(r"\b(av1|vp9|vp8)\b", re.IGNORECASE)
_HDR_NAME_RE         = re.compile(r"\bhdr10\+|\bhdr10plus\b|\bhdr10\b|\bhdr\b|\bhlg\b|\bpq10\b", re.IGNORECASE)
_HEVC_NAME_RE        = re.compile(r"\b(x265|hevc|h\.?265)\b", re.IGNORECASE)
_AAC_SAMPLE_RATE     = "48000"   # browsers require consistent sample rate in TS
_H264_RE             = re.compile(r"\bx264\b|\bh\.?264\b|\bavc\b", re.IGNORECASE)
_AAC_NAME_RE         = re.compile(r"\baac\b", re.IGNORECASE)
_BAD_AUDIO_RE        = re.compile(r"\bdts\b|\btruehd\b|\batmos\b|\bdts.?hd\b|\bdts.?ma\b", re.IGNORECASE)
_TEXT_SUB_CODECS  = {"subrip", "ass", "ssa", "webvtt", "mov_text", "srt"}


def _parse_browser_caps(user_agent: str) -> dict:
    """Derive codec capabilities from User-Agent string.

    Used to tune candidate scoring per browser:
    - Firefox has no HEVC playback support (always needs server transcode).
    - Chrome/Edge on desktop support HEVC via hardware, but not guaranteed on Linux.
    - Safari supports HEVC natively.
    """
    ua = (user_agent or "").lower()
    is_firefox = "firefox/" in ua
    # Chromium on Linux rarely has HEVC hardware decode; desktop Windows/Mac usually does.
    is_linux   = "linux" in ua and "android" not in ua
    is_chrome  = ("chrome/" in ua or "chromium/" in ua) and "edg" not in ua
    hevc_ok    = not is_firefox and not (is_chrome and is_linux)
    return {"hevc_ok": hevc_ok}


# ── Torrent selection ──────────────────────────────────────────────────────────

def _web_score(stream: torrentio.TorrentioStream,
               caps: dict | None = None) -> int:
    caps = caps or {}
    blob = f"{stream.name} {stream.title}"
    if torrentio._DV_RE.search(blob):      return -1  # Dolby Vision: browser-incompatible
    if _NO_BROWSER_VIDEO_RE.search(blob):  return -1  # AV1/VP9/VP8: no browser HLS support
    if _HDR_NAME_RE.search(blob):          return -1  # HDR: browsers can't tone-map

    max_gb = _settings.get("WEB_PLAYER_MAX_SIZE_GB", 15) or 15
    if 0 < stream.size_gb > max_gb:
        return -1

    score = 0
    if stream.quality == "1080p":   score += 100
    elif stream.quality == "2160p": return -1   # 4K = altijd HEVC + groot, niet geschikt voor web
    elif stream.quality == "720p":  score += 50

    if torrentio._WEBDL_RE.search(blob): score += 40

    is_h264 = bool(_H264_RE.search(blob))
    is_aac  = bool(_AAC_NAME_RE.search(blob))
    if is_h264:            score += 100  # direct play in every browser
    if is_aac:             score += 50   # direct play audio, no transcode needed
    if is_h264 and is_aac: score += 50   # perfect combo: zero server work

    # Browser-specific: penalise HEVC when the browser can't hardware-decode it.
    # Firefox has no HEVC at all; Chrome on Linux usually lacks hardware decode.
    if not caps.get("hevc_ok", True) and _HEVC_NAME_RE.search(blob):
        score -= 200  # still selectable as last resort, but strongly avoided

    if _BAD_AUDIO_RE.search(blob): score -= 150  # DTS/TrueHD/Atmos: no browser support, always transcode
    if stream.seeders > 10:        score += 10

    # Smaller = faster initial buffering.
    if   0 < stream.size_gb < 0.5: score += 55
    elif stream.size_gb     < 2:   score += 40
    elif stream.size_gb     < 4:   score += 30
    elif stream.size_gb     < 8:   score += 18
    elif stream.size_gb     < 12:  score += 8

    return score


def find_web_candidates(imdb_id: str, media_type: str,
                        season: int | None = None,
                        episode: int | None = None,
                        browser_caps: dict | None = None) -> list[torrentio.TorrentioStream]:
    streams: list[torrentio.TorrentioStream] = []
    seen: set[str] = set()

    if _settings.get("ZILEAN_ENABLED", False) and health_cache.is_up("zilean"):
        for s in zilean.fetch_streams(imdb_id, season=season, episode=episode):
            if s.info_hash not in seen:
                seen.add(s.info_hash)
                streams.append(s)

    if health_cache.is_up("torrentio"):
        kind = "movie" if media_type == "movie" else "series"
        for s in torrentio.fetch_streams(kind, imdb_id, season=season, episode=episode):
            if s.info_hash not in seen:
                seen.add(s.info_hash)
                streams.append(s)

    scored = sorted(
        ((s, _web_score(s, browser_caps)) for s in streams
         if _web_score(s, browser_caps) >= 0),
        key=lambda x: x[1], reverse=True,
    )
    return [s for s, _ in scored]


# ── Job lifecycle ──────────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    SEARCHING     = "searching"
    MATERIALIZING = "materializing"
    PROBING       = "probing"
    PREPARING     = "preparing"
    READY         = "ready"
    ERROR         = "error"


@dataclass
class PrepareJob:
    job_id:       str
    imdb_id:      str
    media_type:   str
    season:       int | None
    episode:      int | None
    browser_caps: dict = field(default_factory=dict)
    status:       JobStatus = JobStatus.SEARCHING
    message:      str = ""
    token:        str | None = None
    stream_url:   str | None = None
    stream_type:  str = "hls"
    cdn_url:      str | None = None
    file_info:    dict | None = None
    error:        str | None = None
    _thread:      threading.Thread = field(default=None, repr=False)


_jobs: dict[str, PrepareJob] = {}
_jobs_lock = threading.Lock()


def start_prepare_job(imdb_id: str, media_type: str,
                      season: int | None = None,
                      episode: int | None = None,
                      user_agent: str = "") -> str:
    job_id = uuid.uuid4().hex[:12]
    job = PrepareJob(job_id=job_id, imdb_id=imdb_id, media_type=media_type,
                     season=season, episode=episode,
                     browser_caps=_parse_browser_caps(user_agent))
    with _jobs_lock:
        _jobs[job_id] = job
    t = threading.Thread(target=_run_job, args=(job,), daemon=True)
    job._thread = t
    t.start()
    return job_id


def get_job(job_id: str) -> PrepareJob | None:
    with _jobs_lock:
        return _jobs.get(job_id)


# ── Pipeline ───────────────────────────────────────────────────────────────────

def _get_cdn_url(stream: torrentio.TorrentioStream,
                 ) -> tuple[str | None, int | None, int | None]:
    """Resolve a TorrentioStream to (cdn_url, torrent_id, file_id).

    The caller guarantees that either:
    - the hash is already in the user's TorBox library, OR
    - TorBox has it cached (instant add).

    We never wait for a full download here.
    """
    item = torbox.find_by_hash(stream.info_hash)

    if item is None:
        # Not in library yet  -  add it (instant because caller verified cache).
        log.info("web_player: adding cached magnet hash=%s", stream.info_hash)
        try:
            result     = torbox.add_magnet(stream.magnet, reason="web_player")
            torrent_id = (result or {}).get("torrent_id") or (result or {}).get("id")
        except torbox.RateLimited:
            log.warning("web_player: TorBox rate-limited on add_magnet")
            return None, None, None
        item = torbox.wait_until_ready(stream.info_hash, timeout=60,
                                       torrent_id=torrent_id)
    elif not torbox._is_ready(item):
        item = torbox.wait_until_ready(stream.info_hash, timeout=60,
                                       torrent_id=item.get("id"))

    if not item:
        return None, None, None

    torrent_id = item.get("id")
    files      = item.get("files") or []
    if not files:
        fresh = torbox.find_by_id(torrent_id)
        files = (fresh or {}).get("files") or []
    if not files:
        return None, None, None

    _VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts"}
    videos  = [f for f in files
               if Path(f.get("name") or "").suffix.lower() in _VIDEO_EXT] or files
    main    = max(videos, key=lambda f: f.get("size") or 0)
    file_id = main.get("id")

    url = _request_dl(torrent_id, file_id)
    return url, torrent_id, file_id


def _request_dl(torrent_id: int, file_id: int) -> str | None:
    """Call TorBox requestdl and return the CDN URL."""
    import config as _config
    base = (_settings.get("TORBOX_BASE_URL") or _config.TORBOX_BASE_URL).rstrip("/")
    url  = f"{base}/torrents/requestdl"
    params = {
        "token":      _settings.get("TORBOX_API_KEY") or _config.TORBOX_API_KEY,
        "torrent_id": torrent_id,
        "file_id":    file_id,
        "zip_link":   "false",
    }
    try:
        resp = req_lib.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return (resp.json() or {}).get("data") or None
    except Exception as exc:
        log.warning("web_player: requestdl failed: %s", exc)
        return None


def _run_job(job: PrepareJob) -> None:
    try:
        job.status  = JobStatus.SEARCHING
        job.message = "Looking for a web-compatible version…"

        candidates = find_web_candidates(
            job.imdb_id, job.media_type, job.season, job.episode,
            browser_caps=job.browser_caps,
        )
        if not candidates:
            job.status = JobStatus.ERROR
            job.error  = "No web-compatible version found. Use Jellyfin."
            return

        # Priority 1: already in user's TorBox library (instant CDN URL).
        best = None
        for c in candidates:
            if torbox.find_by_hash(c.info_hash):
                best = c
                log.info("web_player: found in TorBox library hash=%s", c.info_hash)
                break

        # Priority 2: TorBox has it cached (instant add, no download wait).
        if best is None:
            hashes      = [c.info_hash for c in candidates]
            cached_set  = torbox.check_cached(hashes)
            by_hash     = {c.info_hash: c for c in candidates}
            # Pick the highest-scored cached candidate (candidates already sorted).
            for c in candidates:
                if c.info_hash in cached_set:
                    best = c
                    log.info("web_player: TorBox-cached hash=%s", c.info_hash)
                    break

        if best is None:
            job.status = JobStatus.ERROR
            job.error  = "No instantly available version found. Use Jellyfin."
            return

        # Probe each candidate; skip any that turn out to be HDR after all
        # (name-based filter may miss unlabelled HDR releases).
        cdn_url     = None
        file_info   = None
        session_key = None
        torrent_id  = None
        file_id     = None
        for candidate in ([best] + [c for c in candidates if c is not best]):
            # Only use instantly-available content.
            _hash = candidate.info_hash
            if not torbox.find_by_hash(_hash):
                hashes = [_hash]
                if _hash not in torbox.check_cached(hashes):
                    continue

            log.info("web_player: selected %r hash=%s", candidate.title, _hash)

            # Reuse an active direct session (always prefer direct play).
            with _direct_lock:
                existing_direct = _direct_sessions.get(_hash)
            if existing_direct:
                age = time.monotonic() - existing_direct.started_at
                if age > CDN_URL_MAX_AGE_S:
                    log.info("web_player: CDN URL stale (%.0fs), refreshing hash=%s",
                             age, _hash)
                    _refresh_direct_cdn_url(existing_direct)
                else:
                    log.info("web_player: reusing active direct session hash=%s", _hash)
                job.token       = _hash
                job.file_info   = existing_direct.file_info
                job.cdn_url     = None
                job.stream_type = "direct"
                job.stream_url  = f"/stream/{_hash}/direct"
                job.status      = JobStatus.READY
                job.message     = "Ready"
                return

            # Reuse an active HLS session without re-probing (skip if HDR).
            with _sessions_lock:
                existing = _sessions.get(_hash)
            if existing and existing.proc.poll() is None:
                if existing.file_info.get("is_hdr"):
                    log.warning("web_player: skipping HDR cached session hash=%s", _hash)
                    continue
                log.info("web_player: reusing active HLS session hash=%s", _hash)
                multi_audio   = len(existing.file_info.get("audio_tracks", [])) > 1
                job.file_info = existing.file_info
                job.cdn_url   = existing.cdn_url
                job.status    = JobStatus.READY
                job.message   = "Ready"
                job.stream_url = (f"/stream/{_hash}/hls/master.m3u8" if multi_audio
                                  else f"/stream/{_hash}/hls/playlist.m3u8")
                return

            job.status  = JobStatus.MATERIALIZING
            job.message = "Fetching via TorBox…"
            _cdn, _torrent_id, _file_id = _get_cdn_url(candidate)
            if not _cdn:
                log.warning("web_player: requestdl failed for hash=%s, skipping", _hash)
                continue

            # Skip probe — serve directly and let the browser decide.
            # ffprobe runs lazily only if HLS fallback is triggered.
            cdn_url     = _cdn
            torrent_id  = _torrent_id
            file_id     = _file_id
            file_info   = _file_info_from_candidate(candidate)
            session_key = _hash
            break

        if not cdn_url or not file_info or not session_key:
            job.status = JobStatus.ERROR
            job.error  = "No instantly available version found. Use Jellyfin."
            return

        job.cdn_url   = cdn_url
        job.file_info = file_info

        job.status  = JobStatus.PREPARING
        job.message = "Preparing for playback…"

        _start_direct(session_key, file_info, cdn_url,
                      torrent_id=torrent_id, file_id=file_id)
        job.token       = session_key
        job.stream_type = "direct"
        job.stream_url  = f"/stream/{session_key}/direct"
        job.status      = JobStatus.READY
        job.message     = "Ready"

    except Exception:
        log.exception("web_player: prepare job %s crashed", job.job_id)
        job.status = JobStatus.ERROR
        job.error  = "Internal error  -  check server logs."


# ── FFprobe ────────────────────────────────────────────────────────────────────

def _probe(cdn_url: str) -> dict | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", cdn_url],
            capture_output=True, timeout=20,
        )
        data    = json.loads(result.stdout)
    except Exception as exc:
        log.warning("web_player: ffprobe failed: %s", exc)
        return None

    streams = data.get("streams", [])

    video = next((s for s in streams if s["codec_type"] == "video"), {})
    audio = [s for s in streams if s["codec_type"] == "audio"]
    subs  = [s for s in streams if s["codec_type"] == "subtitle"]

    def _tag(s, key, default=""):
        return s.get("tags", {}).get(key, default)

    # HDR detection: PQ (HDR10/HDR10+) or HLG transfer functions signal HDR.
    _HDR_TRANSFERS = {"smpte2084", "arib-std-b67", "smpte428"}
    color_transfer  = video.get("color_transfer", "")
    is_hdr          = color_transfer in _HDR_TRANSFERS
    fmt_name        = data.get("format", {}).get("format_name", "")
    return {
        "duration_s":      float(data.get("format", {}).get("duration", 0)),
        "video_codec":     video.get("codec_name", "unknown"),
        "width":           video.get("width"),
        "height":          video.get("height"),
        "is_hdr":          is_hdr,
        "color_transfer":  color_transfer,
        "container":       fmt_name,
        "audio_tracks":    [
            {"index": i, "codec": t["codec_name"],
             "language": _tag(t, "language", "und"),
             "title":    _tag(t, "title"),
             "channels": t.get("channels", 2)}
            for i, t in enumerate(audio)
        ],
        "subtitle_tracks": [
            {"index": i, "codec": t["codec_name"],
             "language": _tag(t, "language", "und"),
             "title":    _tag(t, "title")}
            for i, t in enumerate(subs)
        ],
    }


# ── Direct play ───────────────────────────────────────────────────────────────

def _content_type_for(file_info: dict) -> str:
    fmt = file_info.get("container", "")
    if "matroska" in fmt or "webm" in fmt:
        return "video/x-matroska"
    return "video/mp4"


@dataclass
class DirectSession:
    token:        str
    content_type: str
    cdn_url:      str
    file_info:    dict
    torrent_id:   int | None = None
    file_id:      int | None = None
    converting:   bool  = False   # True once HLS fallback has been triggered
    started_at:   float = field(default_factory=time.monotonic)
    last_request: float = field(default_factory=time.monotonic)
    _hb:          threading.Thread = field(default=None, repr=False)

    def touch(self):
        self.last_request = time.monotonic()

    def start_heartbeat(self):
        def _beat():
            while True:
                time.sleep(60)
                if time.monotonic() - self.last_request > SESSION_IDLE_CLEANUP:
                    break
                db.touch_virtual_item(self.token)
        self._hb = threading.Thread(target=_beat, daemon=True)
        self._hb.start()


_direct_sessions: dict[str, DirectSession] = {}
_direct_lock = threading.Lock()


def get_direct_session(token: str) -> DirectSession | None:
    with _direct_lock:
        return _direct_sessions.get(token)


def _refresh_direct_cdn_url(session: DirectSession) -> None:
    """Fetch a fresh TorBox signed URL for an existing direct session in-place."""
    if session.torrent_id is None or session.file_id is None:
        return
    new_url = _request_dl(session.torrent_id, session.file_id)
    if new_url:
        session.cdn_url    = new_url
        session.started_at = time.monotonic()
        log.info("web_player: refreshed CDN URL for token=%s", session.token)
    else:
        log.warning("web_player: CDN URL refresh failed for token=%s", session.token)


def _start_direct(token: str, file_info: dict, cdn_url: str,
                  torrent_id: int | None = None,
                  file_id:    int | None = None) -> DirectSession:
    session = DirectSession(token=token, content_type=_content_type_for(file_info),
                            cdn_url=cdn_url, file_info=file_info,
                            torrent_id=torrent_id, file_id=file_id)
    session.start_heartbeat()
    with _direct_lock:
        _direct_sessions[token] = session
    log.info("web_player: direct play session started token=%s", token)
    return session


def start_hls_conversion(token: str) -> bool:
    """Trigger HLS pipeline for an active direct session (browser couldn't play it)."""
    s = get_direct_session(token)
    if not s:
        return False
    if s.converting:
        return True  # already in progress — idempotent
    s.converting = True
    threading.Thread(target=_do_hls_conversion, args=(token, s.file_info),
                     daemon=True).start()
    return True


def _file_info_from_candidate(candidate) -> dict:
    """Minimal file_info derived from torrent name — no ffprobe needed."""
    blob = f"{candidate.name} {candidate.title}".lower()
    codec = 'unknown'
    if 'x265' in blob or 'hevc' in blob or 'h265' in blob:
        codec = 'hevc'
    elif 'x264' in blob or 'h264' in blob or 'avc' in blob:
        codec = 'h264'
    height = {'2160p': 2160, '1080p': 1080, '720p': 720, '480p': 480}.get(
        candidate.quality or '', 0)
    container = 'matroska' if ('.mkv' in blob or blob.split().count('mkv') > 0) else 'mp4'
    return {
        'video_codec':      codec,
        'height':           height,
        'width':            0,
        'duration_s':       0,
        'is_hdr':           False,
        'color_transfer':   '',
        'container':        container,
        'audio_tracks':     [],
        'subtitle_tracks':  [],
    }


def _do_hls_conversion(token: str, file_info: dict) -> None:
    tmp_dir = PLAYER_TMP_DIR / token
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        s = get_direct_session(token)
        cdn_url = s.cdn_url if s else None
        if not cdn_url:
            log.warning("web_player: HLS fallback — no CDN URL for token=%s", token)
            (tmp_dir / "hls_error.txt").write_text("Session expired — please reopen the player")
            return
        # Probe now if we skipped it during direct play (lazy path).
        if not file_info.get('audio_tracks'):
            log.info("web_player: lazy ffprobe for HLS fallback token=%s", token)
            probed = _probe(cdn_url)
            if probed is None:
                log.warning("web_player: ffprobe failed for token=%s", token)
                (tmp_dir / "hls_error.txt").write_text("Could not read file info — use Jellyfin")
                return
            file_info = probed
            s = get_direct_session(token)
            if s:
                s.file_info = file_info
        session = _start_hls(token, cdn_url, file_info, tmp_dir)
        if not _wait_segments(tmp_dir, SEGMENT_WAIT_COUNT, SEGMENT_WAIT_TIMEOUT):
            session.proc.terminate()
            log.warning("web_player: HLS fallback timed out token=%s", token)
            (tmp_dir / "hls_error.txt").write_text("FFmpeg timed out — use Jellyfin for this file")
            return
        multi_audio = len(file_info.get("audio_tracks", [])) > 1
        playlist = "master.m3u8" if multi_audio else "playlist.m3u8"
        (tmp_dir / "hls_ready.txt").write_text(playlist)
        threading.Thread(
            target=_extract_subtitles,
            args=(cdn_url, file_info.get("subtitle_tracks", []), token, tmp_dir),
            daemon=True,
        ).start()
        log.info("web_player: HLS fallback ready token=%s playlist=%s", token, playlist)
    except Exception:
        log.exception("web_player: HLS fallback crashed token=%s", token)
        try:
            (tmp_dir / "hls_error.txt").write_text("Internal error during conversion")
        except Exception:
            pass


# ── HLS session ────────────────────────────────────────────────────────────────

@dataclass
class HLSSession:
    token:        str
    proc:         subprocess.Popen
    tmp_dir:      Path
    cdn_url:      str  = ""
    file_info:    dict = field(default_factory=dict)
    started_at:   float = field(default_factory=time.monotonic)
    last_request: float = field(default_factory=time.monotonic)
    _hb:          threading.Thread = field(default=None, repr=False)

    def touch(self):
        self.last_request = time.monotonic()

    def start_heartbeat(self):
        def _beat():
            while self.proc.poll() is None:
                db.touch_virtual_item(self.token)
                time.sleep(60)
        self._hb = threading.Thread(target=_beat, daemon=True)
        self._hb.start()


_sessions: dict[str, HLSSession] = {}
_sessions_lock = threading.Lock()


def get_session(token: str) -> HLSSession | None:
    with _sessions_lock:
        return _sessions.get(token)


def seek_session(token: str, position_s: float) -> str | None:
    """Kill the running FFmpeg, wipe segments, restart at position_s.
    Returns the (unchanged) stream URL so the frontend can reload Hls.js."""
    with _sessions_lock:
        session = _sessions.get(token)
    if not session:
        return None

    multi_audio = len(session.file_info.get("audio_tracks", [])) > 1

    # Stop current process
    session.proc.terminate()
    try:
        session.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        session.proc.kill()
        session.proc.wait(timeout=1)

    tmp_dir = session.tmp_dir

    # Remove old segments and playlists.
    for pattern in ("seg*.ts", "seg*.m4s", "seg_*_*.ts", "seg_*_*.m4s", "init*.mp4"):
        for f in tmp_dir.glob(pattern):
            f.unlink(missing_ok=True)
    for name in (["playlist.m3u8", "video.m3u8", "master.m3u8"]
                 + [f"audio_{i}.m3u8" for i in range(10)]):
        (tmp_dir / name).unlink(missing_ok=True)

    # Restart at new position (registers updated session under same token)
    _start_hls(token, session.cdn_url, session.file_info, tmp_dir,
               seek_offset=position_s)

    # Wait for first segments (multi-audio already waits inside _start_hls)
    if not multi_audio:
        _wait_segments(tmp_dir, SEGMENT_WAIT_COUNT, SEGMENT_WAIT_TIMEOUT)

    return (f"/stream/{token}/hls/master.m3u8" if multi_audio
            else f"/stream/{token}/hls/playlist.m3u8")



def _audio_copy_or_transcode(track: dict, out_index: int) -> list[str]:
    """Return FFmpeg args for a single audio output track.

    Compatible codecs (AAC stereo) are copied as-is; everything else
    (TrueHD, DTS, multichannel, Opus, Vorbis) is transcoded to AAC-LC stereo.
    """
    codec_ok = track["codec"] in _BROWSER_AUDIO_OK and track.get("channels", 2) <= 2
    if codec_ok:
        return [f"-c:a:{out_index}", "copy"]
    return [
        f"-c:a:{out_index}", "aac",
        f"-profile:a:{out_index}", "aac_low",
        f"-ar:a:{out_index}", _AAC_SAMPLE_RATE,
        f"-ac:a:{out_index}", "2",
        f"-b:a:{out_index}", "192k",
    ]


def _start_hls(token: str, cdn_url: str, file_info: dict, tmp_dir: Path,
               seek_offset: float = 0.0) -> HLSSession:
    """Start (or restart) HLS segmentation.

    Strategy:
    - H.264: mpegts segments, video copy, audio copy-or-aac. Near-zero NAS CPU.
    - HEVC:  fMP4 segments, video copy, audio copy-or-aac. Near-zero NAS CPU.
             Safari plays HEVC natively; Chrome/Edge use hardware decoding.
    - AV1/VP9/VP8: transcode to H.264 mpegts (rare, heavy but unavoidable).

    seek_offset > 0 = fast keyframe seek in input before generating segments.
    """
    audio_tracks = file_info["audio_tracks"]
    multi_audio  = len(audio_tracks) > 1

    # Video encoding strategy:
    # H.264:      copy into mpegts (zero CPU, universally supported).
    # HEVC/other: transcode to H.264 mpegts (Firefox has no HEVC, fMP4 unreliable).
    #             preset fast + crf 22 gives good quality at moderate CPU cost.
    video_codec = (file_info.get("video_codec") or "h264").lower()
    _H264_OK    = {"h264", "avc"}
    use_fmp4    = False
    seg_type    = "mpegts"
    seg_ext     = "ts"

    if video_codec in _H264_OK:
        v_enc      = ["-c:v", "copy"]
        mode_label = "ts-copy"
    else:
        v_enc      = ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"]
        mode_label = f"ts-x264(from {video_codec})"

    # -ss BEFORE -i = fast keyframe seek.
    input_args = (["-ss", f"{seek_offset:.3f}"] if seek_offset > 0 else []) + ["-i", cdn_url]

    if multi_audio:
        # Multi-audio: video-only output + one output per audio track,
        # bound together by a master.m3u8.
        cmd = ["ffmpeg", "-y"] + input_args

        # Output 0: video only
        cmd += [
            "-map", "0:v:0", *v_enc,
            "-hls_time", "2", "-hls_list_size", "0",
            "-hls_flags", "independent_segments",
            "-hls_segment_type", seg_type,
            "-hls_segment_filename", str(tmp_dir / f"seg_v%05d.{seg_ext}"),
        ]
        cmd.append(str(tmp_dir / "video.m3u8"))

        # Outputs 1…N: one audio stream each
        for i, track in enumerate(audio_tracks):
            cmd += [
                "-map", f"0:a:{i}", *_audio_copy_or_transcode(track, 0),
                "-hls_time", "2", "-hls_list_size", "0",
                "-hls_flags", "independent_segments",
                "-hls_segment_type", seg_type,
                "-hls_segment_filename", str(tmp_dir / f"seg_a{i}_%05d.{seg_ext}"),
            ]
            cmd.append(str(tmp_dir / f"audio_{i}.m3u8"))

        log.info("web_player: FFmpeg multi-audio/%s token=%s seek=%.1f",
                 mode_label, token, seek_offset)
        stderr_log = open(tmp_dir / "ffmpeg.log", "w")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_log)

        session = HLSSession(token=token, proc=proc, tmp_dir=tmp_dir,
                             cdn_url=cdn_url, file_info=file_info)
        session.start_heartbeat()
        with _sessions_lock:
            _sessions[token] = session

        # Wait for first video segments before writing master playlist
        _wait_segments_pattern(tmp_dir, f"seg_v*.{seg_ext}",
                               SEGMENT_WAIT_COUNT, SEGMENT_WAIT_TIMEOUT)

        # Write master.m3u8
        lines  = ["#EXTM3U"]
        default = "YES"
        for i, track in enumerate(audio_tracks):
            lang  = (track.get("language") or "und").lower()
            name  = track.get("title") or lang.upper()
            lines.append(
                f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",'
                f'NAME="{name}",DEFAULT={default},LANGUAGE="{lang}",'
                f'URI="audio_{i}.m3u8"'
            )
            default = "NO"
        lines.append('#EXT-X-STREAM-INF:BANDWIDTH=4000000,AUDIO="audio"')
        lines.append("video.m3u8")
        (tmp_dir / "master.m3u8").write_text("\n".join(lines) + "\n")

    else:
        # Single audio: combined video+audio output
        track = audio_tracks[0] if audio_tracks else None
        a_args = (_audio_copy_or_transcode(track, 0) if track else [])

        cmd = [
            "ffmpeg", "-y", *input_args,
            "-map", "0:v:0",
            *((["-map", "0:a:0"] + a_args) if track else ["-an"]),
            *v_enc,
            "-hls_time", "2", "-hls_list_size", "0",
            "-hls_flags", "independent_segments",
            "-hls_segment_type", seg_type,
            "-hls_segment_filename", str(tmp_dir / f"seg%05d.{seg_ext}"),
        ]
        cmd.append(str(tmp_dir / "playlist.m3u8"))

        log.info("web_player: FFmpeg single-audio/%s token=%s seek=%.1f",
                 mode_label, token, seek_offset)
        stderr_log = open(tmp_dir / "ffmpeg.log", "w")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_log)

        session = HLSSession(token=token, proc=proc, tmp_dir=tmp_dir,
                             cdn_url=cdn_url, file_info=file_info)
        session.start_heartbeat()
        with _sessions_lock:
            _sessions[token] = session

    return session


def _wait_segments(tmp_dir: Path, count: int, timeout: float) -> bool:
    """Wait until at least `count` segments exist (either .ts or .m4s)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = len(list(tmp_dir.glob("seg*.ts"))) + len(list(tmp_dir.glob("seg*.m4s")))
        if found >= count:
            return True
        time.sleep(0.5)
    return False


def _wait_segments_pattern(tmp_dir: Path, pattern: str, count: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(list(tmp_dir.glob(pattern))) >= count:
            return True
        time.sleep(0.5)
    return False


# ── Subtitles ──────────────────────────────────────────────────────────────────

def _extract_subtitles(cdn_url: str, sub_tracks: list,
                       token: str, tmp_dir: Path) -> None:
    for track in sub_tracks:
        if track["codec"] not in _TEXT_SUB_CODECS:
            continue
        lang = track["language"]
        out  = tmp_dir / f"sub_{track['index']}_{lang}.vtt"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", cdn_url,
                 "-map", f"0:s:{track['index']}", "-c:s", "webvtt", str(out)],
                capture_output=True, timeout=120,
            )
        except Exception:
            log.warning("web_player: sub extract failed track=%d", track["index"])


def _subtitles_task(cdn_url: str, file_info: dict,
                    imdb_id: str, media_type: str,
                    season: int | None, episode: int | None,
                    token: str, tmp_dir: Path) -> None:
    """Extract embedded subs; fall back to OpenSubtitles when none found."""
    _extract_subtitles(cdn_url, file_info["subtitle_tracks"], token, tmp_dir)

    # If embedded extraction produced at least one VTT, we're done.
    if list(tmp_dir.glob("sub_*.vtt")):
        return

    _fetch_external_subtitles(imdb_id, media_type, season, episode, tmp_dir)


def _fetch_external_subtitles(imdb_id: str, media_type: str,
                               season: int | None, episode: int | None,
                               tmp_dir: Path) -> None:
    """Download subtitles from OpenSubtitles and convert to WebVTT."""
    api_key = _settings.get("OPENSUBTITLES_API_KEY", "")
    if not api_key:
        return

    langs = _settings.get("OPENSUBTITLES_LANGUAGES") or []
    if isinstance(langs, str):
        langs = [l.strip() for l in langs.split(",") if l.strip()]
    if not langs:
        langs = ["en"]

    for lang in langs:
        vtt_path = tmp_dir / f"sub_ext_{lang}.vtt"
        if vtt_path.exists():
            continue

        results = _subtitles._search(imdb_id, season, episode, lang)
        if not results:
            log.info("web_player: no external subtitles for %s lang=%s", imdb_id, lang)
            continue

        top   = results[0]
        files = (top.get("attributes") or {}).get("files") or []
        if not files:
            continue
        file_id = files[0].get("file_id")
        if not file_id:
            continue

        url = _subtitles._request_download_url(file_id)
        if not url:
            continue

        try:
            resp = req_lib.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("web_player: subtitle download failed lang=%s: %s", lang, exc)
            continue

        # Write raw file, then let ffmpeg normalise to WebVTT.
        raw_path = tmp_dir / f"sub_ext_{lang}.raw"
        raw_path.write_bytes(resp.content)
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw_path), str(vtt_path)],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0:
                log.info("web_player: external subtitle saved lang=%s token=%s", lang, tmp_dir.name)
            else:
                log.warning("web_player: ffmpeg vtt conversion failed lang=%s: %s",
                            lang, result.stderr[-300:])
        except Exception as exc:
            log.warning("web_player: vtt conversion error lang=%s: %s", lang, exc)
        finally:
            raw_path.unlink(missing_ok=True)


def list_subtitles(token: str) -> list[dict]:
    s = get_session(token)
    if not s:
        return []
    out = []
    for p in sorted(s.tmp_dir.glob("sub_*.vtt")):
        parts = p.stem.split("_")   # sub_0_eng  or  sub_ext_nl
        lang  = parts[-1]
        source = "External" if "ext" in parts else "Embedded"
        out.append({
            "language": lang,
            "label":    source,
            "url":      f"/stream/{token}/subtitles/{p.name}",
        })
    return out


# ── Cleanup ────────────────────────────────────────────────────────────────────

def cleanup_idle_sessions() -> None:
    cutoff = time.monotonic() - SESSION_IDLE_CLEANUP

    with _sessions_lock:
        stale = [t for t, s in _sessions.items() if s.last_request < cutoff]
    for token in stale:
        with _sessions_lock:
            session = _sessions.pop(token, None)
        if session:
            session.proc.terminate()
            shutil.rmtree(session.tmp_dir, ignore_errors=True)
            log.info("web_player: cleaned up idle HLS session token=%s", token)

    with _direct_lock:
        stale_direct = [t for t, s in _direct_sessions.items() if s.last_request < cutoff]
    for token in stale_direct:
        with _direct_lock:
            _direct_sessions.pop(token, None)
        log.info("web_player: cleaned up idle direct session token=%s", token)

