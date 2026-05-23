"""
Web player backend — parked, not wired up.

To activate, in app.py:
    import _webplayer.web_player as web_player

    # Register routes (copy from ROUTES section at bottom of this file)
    # Add scheduler job: scheduler.add_job(web_player.cleanup_idle_sessions, 'interval', minutes=15)

No DB migrations needed.
Uses existing virtual_items columns:
    source     = 'web_player'   (distinguishes browser tokens from jellyfin tokens)
    strm_path  = NULL           (no .strm file written for browser tokens)
    imdb_id / season / episode  (already present)
"""

import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import requests as req_lib

import catbox
import db
import health_cache
import settings as _settings
import torrentio
import zilean

log = logging.getLogger(__name__)

PLAYER_TMP_DIR        = Path("/tmp/mycelium-player")
SEGMENT_WAIT_COUNT    = 3     # segments before signalling ready
SEGMENT_WAIT_TIMEOUT  = 45    # seconds
SESSION_IDLE_CLEANUP  = 1800  # remove session after 30 min idle

_BROWSER_AUDIO_OK = {"aac", "vorbis", "opus"}
_TEXT_SUB_CODECS  = {"subrip", "ass", "ssa", "webvtt", "mov_text", "srt"}


# ── Torrent selection ──────────────────────────────────────────────────────────

def _web_score(stream: torrentio.TorrentioStream) -> int:
    """Score a release for browser compatibility. -1 = disqualified."""
    blob = f"{stream.name} {stream.title}"

    if torrentio._HEVC_RE.search(blob):   return -1  # H.265 — browser issues
    if stream.quality == "2160p":          return -1  # 4K
    if torrentio._DV_RE.search(blob):     return -1  # Dolby Vision

    score = 0
    if stream.quality == "1080p":                        score += 100
    elif stream.quality == "720p":                       score += 50
    if torrentio._WEBDL_RE.search(blob):                 score += 40
    if stream.seeders > 10:                              score += 10
    if 0 < stream.size_gb < 8:                           score += 5

    return score


def find_web_candidates(imdb_id: str, media_type: str,
                        season: int | None = None,
                        episode: int | None = None) -> list[torrentio.TorrentioStream]:
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
        ((s, _web_score(s)) for s in streams if _web_score(s) >= 0),
        key=lambda x: x[1], reverse=True
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
    job_id:     str
    imdb_id:    str
    media_type: str
    season:     int | None
    episode:    int | None
    status:     JobStatus = JobStatus.SEARCHING
    message:    str = ""
    token:      str | None = None
    stream_url: str | None = None
    file_info:  dict | None = None
    error:      str | None = None
    _thread:    threading.Thread = field(default=None, repr=False)


_jobs: dict[str, PrepareJob] = {}
_jobs_lock = threading.Lock()


def start_prepare_job(imdb_id: str, media_type: str,
                      season: int | None = None,
                      episode: int | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = PrepareJob(job_id=job_id, imdb_id=imdb_id, media_type=media_type,
                     season=season, episode=episode)
    with _jobs_lock:
        _jobs[job_id] = job
    t = threading.Thread(target=_run_job, args=(job,), daemon=True)
    job._thread = t
    t.start()
    return job_id


def get_job(job_id: str) -> PrepareJob | None:
    with _jobs_lock:
        return _jobs.get(job_id)


# ── Prepare pipeline ───────────────────────────────────────────────────────────

def _run_job(job: PrepareJob) -> None:
    try:
        # 1. Existing web-player token for this content?
        job.status  = JobStatus.SEARCHING
        job.message = "Zoeken naar web-compatibele versie…"

        existing = _db_get_web_player_token(job.imdb_id, job.season, job.episode)
        if existing:
            token = existing["token"]
            log.info("web_player: reusing token=%s for %s", token, job.imdb_id)
        else:
            # 2. Search for H.264 / WEB-DL release
            candidates = find_web_candidates(
                job.imdb_id, job.media_type, job.season, job.episode
            )
            if not candidates:
                job.status = JobStatus.ERROR
                job.error  = "Geen web-compatibele versie gevonden. Gebruik Jellyfin."
                return

            best = candidates[0]
            log.info("web_player: selected %r hash=%s for %s",
                     best.title, best.info_hash, job.imdb_id)

            # 3. Register catbox token (lazy — no TorBox add yet)
            token = catbox.register(
                info_hash  = best.info_hash,
                magnet     = best.magnet,
                title      = best.title,
                media_type = job.media_type,
                imdb_id    = job.imdb_id,
                quality    = best.quality,
                source     = "web_player",
                size_gb    = best.size_gb,
                season     = job.season,
                episode    = job.episode,
            )

        job.token = token

        # 4. Materialise — add to TorBox if needed, wait for cache
        job.status  = JobStatus.MATERIALIZING
        job.message = "Ophalen via TorBox…"

        cdn_url = catbox.materialize(token, allow_readd=True)
        if not cdn_url:
            job.status = JobStatus.ERROR
            job.error  = "TorBox kon het bestand niet ophalen."
            return

        # 5. Probe with ffprobe
        job.status  = JobStatus.PROBING
        job.message = "Bestandsinfo ophalen…"

        file_info = _probe(cdn_url)
        job.file_info = file_info
        log.info("web_player: probe token=%s video=%s audio=%s subs=%d",
                 token, file_info["video_codec"],
                 [t["codec"] for t in file_info["audio_tracks"]],
                 len(file_info["subtitle_tracks"]))

        # 6. Start FFmpeg HLS (video copy, audio → AAC where needed)
        job.status  = JobStatus.PREPARING
        job.message = "Voorbereiden voor afspelen…"

        tmp_dir = PLAYER_TMP_DIR / token
        tmp_dir.mkdir(parents=True, exist_ok=True)

        session = _start_hls(token, cdn_url, file_info, tmp_dir)

        # 7. Wait for first segments before signalling ready
        if not _wait_segments(tmp_dir, SEGMENT_WAIT_COUNT, SEGMENT_WAIT_TIMEOUT):
            session.proc.terminate()
            job.status = JobStatus.ERROR
            job.error  = "Timeout: FFmpeg genereerde geen segmenten."
            return

        # 8. Extract subtitles in background — non-blocking
        threading.Thread(
            target=_extract_subtitles,
            args=(cdn_url, file_info["subtitle_tracks"], token, tmp_dir),
            daemon=True,
        ).start()

        job.status     = JobStatus.READY
        job.message    = "Klaar"
        job.stream_url = f"/stream/{token}/hls/playlist.m3u8"

    except Exception:
        log.exception("web_player: prepare job %s crashed", job.job_id)
        job.status = JobStatus.ERROR
        job.error  = "Interne fout — zie server logs."


# ── FFprobe ────────────────────────────────────────────────────────────────────

def _probe(cdn_url: str) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", cdn_url],
        capture_output=True, timeout=20,
    )
    data    = json.loads(result.stdout)
    streams = data.get("streams", [])

    video = next((s for s in streams if s["codec_type"] == "video"), {})
    audio = [s for s in streams if s["codec_type"] == "audio"]
    subs  = [s for s in streams if s["codec_type"] == "subtitle"]

    def _tag(s, key, default=""):
        return s.get("tags", {}).get(key, default)

    return {
        "duration_s":   float(data.get("format", {}).get("duration", 0)),
        "video_codec":  video.get("codec_name", "unknown"),
        "width":        video.get("width"),
        "height":       video.get("height"),
        "audio_tracks": [
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


# ── HLS session ────────────────────────────────────────────────────────────────

@dataclass
class HLSSession:
    token:        str
    proc:         subprocess.Popen
    tmp_dir:      Path
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


def _start_hls(token: str, cdn_url: str, file_info: dict,
               tmp_dir: Path) -> HLSSession:
    audio_args: list[str] = []
    for i, track in enumerate(file_info["audio_tracks"]):
        audio_args += ["-map", f"0:a:{i}"]
        if track["codec"] in _BROWSER_AUDIO_OK:
            audio_args += [f"-c:a:{i}", "copy"]
        else:
            audio_args += [f"-c:a:{i}", "aac", f"-b:a:{i}", "192k"]

    cmd = [
        "ffmpeg", "-y",
        "-i", cdn_url,
        "-map", "0:v:0",
        *audio_args,
        "-c:v", "copy",
        "-hls_time", "6",
        "-hls_list_size", "0",
        "-hls_flags", "independent_segments",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", str(tmp_dir / "seg%05d.ts"),
        str(tmp_dir / "playlist.m3u8"),
    ]
    log.info("web_player: starting FFmpeg for token=%s", token)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    session = HLSSession(token=token, proc=proc, tmp_dir=tmp_dir)
    session.start_heartbeat()
    with _sessions_lock:
        _sessions[token] = session
    return session


def _wait_segments(tmp_dir: Path, count: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(list(tmp_dir.glob("seg*.ts"))) >= count:
            return True
        time.sleep(0.5)
    return False


# ── Subtitles ──────────────────────────────────────────────────────────────────

def _extract_subtitles(cdn_url: str, sub_tracks: list,
                       token: str, tmp_dir: Path) -> None:
    for track in sub_tracks:
        if track["codec"] not in _TEXT_SUB_CODECS:
            log.debug("web_player: skip image-based sub track %d", track["index"])
            continue
        lang = track["language"]
        idx  = track["index"]
        out  = tmp_dir / f"sub_{idx}_{lang}.vtt"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", cdn_url,
                 "-map", f"0:s:{idx}", "-c:s", "webvtt", str(out)],
                capture_output=True, timeout=120,
            )
            if out.exists():
                log.info("web_player: extracted sub %s lang=%s", out.name, lang)
        except Exception:
            log.warning("web_player: sub extract failed track=%d", idx)


def list_subtitles(token: str) -> list[dict]:
    session = get_session(token)
    if not session:
        return []
    return [
        {"language": p.stem.split("_")[-1], "label": p.stem,
         "url": f"/stream/{token}/subtitles/{p.name}"}
        for p in sorted(session.tmp_dir.glob("sub_*.vtt"))
    ]


# ── Cleanup ────────────────────────────────────────────────────────────────────

def cleanup_idle_sessions() -> None:
    """Call via APScheduler every 15 minutes."""
    cutoff = time.monotonic() - SESSION_IDLE_CLEANUP
    with _sessions_lock:
        stale = [t for t, s in _sessions.items() if s.last_request < cutoff]
    for token in stale:
        with _sessions_lock:
            session = _sessions.pop(token, None)
        if session:
            session.proc.terminate()
            shutil.rmtree(session.tmp_dir, ignore_errors=True)
            log.info("web_player: cleaned up idle session token=%s", token)


# ── Byte-range proxy (no FFmpeg) ───────────────────────────────────────────────

def proxy_range(cdn_url: str, range_header: str | None):
    """Stream raw bytes from TorBox CDN back to browser. Used for direct play."""
    headers = {"Range": range_header} if range_header else {}
    return req_lib.get(cdn_url, headers=headers, stream=True, timeout=30)


# ── DB lookup — no migration needed ───────────────────────────────────────────
# Uses existing virtual_items.source column ('web_player') and
# strm_path IS NULL (browser tokens never write a .strm file).

def _db_get_web_player_token(imdb_id: str,
                              season: int | None,
                              episode: int | None) -> dict | None:
    with db._conn() as c:
        return c.execute(
            "SELECT * FROM virtual_items "
            "WHERE imdb_id=? AND source='web_player' "
            "  AND (season IS ? OR season=?) "
            "  AND (episode IS ? OR episode=?) "
            "LIMIT 1",
            (imdb_id, season, season, episode, episode),
        ).fetchone()


# ── Flask routes (paste into app.py when activating) ──────────────────────────
#
# from flask import request, jsonify, abort, send_file, Response
# import _webplayer.web_player as web_player
#
# @app.post("/ui/api/web-player/prepare")
# def web_player_prepare():
#     d = request.json or {}
#     job_id = web_player.start_prepare_job(
#         imdb_id=d["imdb_id"], media_type=d["media_type"],
#         season=d.get("season"), episode=d.get("episode"),
#     )
#     return jsonify(job_id=job_id)
#
# @app.get("/ui/api/web-player/status/<job_id>")
# def web_player_status(job_id):
#     job = web_player.get_job(job_id)
#     if not job: abort(404)
#     return jsonify(status=job.status.value, message=job.message, token=job.token,
#                    stream_url=job.stream_url, file_info=job.file_info, error=job.error)
#
# @app.get("/stream/<token>/hls/playlist.m3u8")
# def stream_hls_playlist(token):
#     s = web_player.get_session(token)
#     if not s: abort(404)
#     s.touch()
#     return send_file(s.tmp_dir / "playlist.m3u8", mimetype="application/vnd.apple.mpegurl")
#
# @app.get("/stream/<token>/hls/<segment>")
# def stream_hls_segment(token, segment):
#     if "/" in segment or not segment.endswith(".ts"): abort(400)
#     s = web_player.get_session(token)
#     if not s: abort(404)
#     p = s.tmp_dir / segment
#     if not p.exists(): abort(404)
#     s.touch()
#     return send_file(p, mimetype="video/mp2t")
#
# @app.get("/stream/<token>/subtitles")
# def stream_subtitles_list(token):
#     return jsonify(subtitles=web_player.list_subtitles(token))
#
# @app.get("/stream/<token>/subtitles/<filename>")
# def stream_subtitle_file(token, filename):
#     if "/" in filename or not filename.endswith(".vtt"): abort(400)
#     s = web_player.get_session(token)
#     if not s: abort(404)
#     p = s.tmp_dir / filename
#     if not p.exists(): abort(404)
#     return send_file(p, mimetype="text/vtt")
#
# @app.post("/stream/<token>/position")
# @_csrf.exempt
# def stream_save_position(token):
#     d = request.json or {}
#     db.save_playback_position(flask_session["user_id"], token,
#                               float(d.get("position_s", 0)), d.get("duration_s"))
#     return jsonify(ok=True)
#
# # scheduler.add_job(web_player.cleanup_idle_sessions, "interval", minutes=15, id="wp_cleanup")
