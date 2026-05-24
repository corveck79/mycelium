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
import subtitles as _subtitles
import torrentio
import zilean

log = logging.getLogger(__name__)

PLAYER_TMP_DIR       = Path("/tmp/mycelium-player")
SEGMENT_WAIT_COUNT   = 3
SEGMENT_WAIT_TIMEOUT = 45
SESSION_IDLE_CLEANUP = 1800

_BROWSER_AUDIO_OK    = {"aac"}   # vorbis/opus not reliable in mpegts HLS
_AAC_SAMPLE_RATE     = "48000"   # browsers require consistent sample rate in TS
_TEXT_SUB_CODECS  = {"subrip", "ass", "ssa", "webvtt", "mov_text", "srt"}


# ── Torrent selection ──────────────────────────────────────────────────────────

def _web_score(stream: torrentio.TorrentioStream) -> int:
    blob = f"{stream.name} {stream.title}"
    if torrentio._HEVC_RE.search(blob):  return -1
    if stream.quality == "2160p":         return -1
    if torrentio._DV_RE.search(blob):    return -1
    score = 0
    if stream.quality == "1080p":                   score += 100
    elif stream.quality == "720p":                  score += 50
    if torrentio._WEBDL_RE.search(blob):            score += 40
    if stream.seeders > 10:                         score += 10
    if 0 < stream.size_gb < 8:                      score += 5
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


# ── Pipeline ───────────────────────────────────────────────────────────────────

def _run_job(job: PrepareJob) -> None:
    try:
        job.status  = JobStatus.SEARCHING
        job.message = "Looking for a web-compatible version…"

        existing = _db_get_web_player_token(job.imdb_id, job.season, job.episode)
        if existing:
            token = existing["token"]
        else:
            candidates = find_web_candidates(
                job.imdb_id, job.media_type, job.season, job.episode
            )
            if not candidates:
                job.status = JobStatus.ERROR
                job.error  = "No web-compatible version found. Use Jellyfin."
                return

            best = candidates[0]
            log.info("web_player: selected %r hash=%s for %s",
                     best.title, best.info_hash, job.imdb_id)

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

        job.status  = JobStatus.MATERIALIZING
        job.message = "Fetching via TorBox…"

        cdn_url = catbox.materialize(token, allow_readd=True)
        if not cdn_url:
            job.status = JobStatus.ERROR
            job.error  = "TorBox could not fetch the file."
            return

        job.status  = JobStatus.PROBING
        job.message = "Reading file info…"

        file_info = _probe(cdn_url)
        job.file_info = file_info

        job.status  = JobStatus.PREPARING
        job.message = "Preparing for playback…"

        tmp_dir = PLAYER_TMP_DIR / token
        tmp_dir.mkdir(parents=True, exist_ok=True)

        multi_audio = len(file_info["audio_tracks"]) > 1
        session = _start_hls(token, cdn_url, file_info, tmp_dir)

        # For multi-audio, _start_hls already waited for video segments.
        # For single-audio, wait here as before.
        if not multi_audio:
            if not _wait_segments(tmp_dir, SEGMENT_WAIT_COUNT, SEGMENT_WAIT_TIMEOUT):
                session.proc.terminate()
                job.status = JobStatus.ERROR
                job.error  = "Timeout: FFmpeg produced no segments."
                return

        threading.Thread(
            target=_subtitles_task,
            args=(cdn_url, file_info, job.imdb_id, job.media_type,
                  job.season, job.episode, token, tmp_dir),
            daemon=True,
        ).start()

        job.status     = JobStatus.READY
        job.message    = "Ready"
        job.stream_url = (f"/stream/{token}/hls/master.m3u8" if multi_audio
                          else f"/stream/{token}/hls/playlist.m3u8")

    except Exception:
        log.exception("web_player: prepare job %s crashed", job.job_id)
        job.status = JobStatus.ERROR
        job.error  = "Internal error — check server logs."


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
        "duration_s":      float(data.get("format", {}).get("duration", 0)),
        "video_codec":     video.get("codec_name", "unknown"),
        "width":           video.get("width"),
        "height":          video.get("height"),
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


def _aac_args(track_index: int) -> list[str]:
    """Encode audio track track_index as AAC-LC stereo.

    Uses `a:N` stream specifiers so the options correctly target audio even
    when video is mapped as output stream 0 before audio.
    """
    i = track_index
    return [
        f"-c:a:{i}",        "aac",
        f"-profile:a:{i}",  "aac_low",   # force AAC-LC; Chrome rejects HE/HEv2 in mpegts
        f"-ar:a:{i}",       _AAC_SAMPLE_RATE,
        f"-ac:a:{i}",       "2",          # downmix to stereo; Chrome won't play 5.1 AAC in mpegts
        f"-b:a:{i}",        "192k",
    ]


def _start_hls(token: str, cdn_url: str, file_info: dict, tmp_dir: Path) -> HLSSession:
    audio_tracks = file_info["audio_tracks"]
    multi_audio  = len(audio_tracks) > 1

    if multi_audio:
        # Multi-audio: video-only stream + separate audio stream per track,
        # stitched together via a master.m3u8 so Hls.js can switch languages.
        cmd = ["ffmpeg", "-y", "-i", cdn_url]

        # Output 0: video-only
        cmd += [
            "-map", "0:v:0", "-c:v", "copy",
            "-hls_time", "6", "-hls_list_size", "0",
            "-hls_flags", "independent_segments",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(tmp_dir / "seg_v%05d.ts"),
            str(tmp_dir / "video.m3u8"),
        ]

        # Output 1…N: one audio stream per track (single stream in each output,
        # so index 0 always refers to that audio stream).
        for i, track in enumerate(audio_tracks):
            codec_ok = track["codec"] in _BROWSER_AUDIO_OK and track.get("channels", 2) <= 2
            if codec_ok:
                a_enc = ["-c:a:0", "copy"]
            else:
                a_enc = [
                    "-c:a:0", "aac",
                    "-profile:a:0", "aac_low",
                    "-ar:a:0", _AAC_SAMPLE_RATE,
                    "-ac:a:0", "2",
                    "-b:a:0", "192k",
                ]
            cmd += [
                "-map", f"0:a:{i}", *a_enc,
                "-hls_time", "6", "-hls_list_size", "0",
                "-hls_flags", "independent_segments",
                "-hls_segment_type", "mpegts",
                "-hls_segment_filename", str(tmp_dir / f"seg_a{i}_%05d.ts"),
                str(tmp_dir / f"audio_{i}.m3u8"),
            ]

        log.info("web_player: starting FFmpeg (multi-audio) for token=%s", token)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        session = HLSSession(token=token, proc=proc, tmp_dir=tmp_dir)
        session.start_heartbeat()
        with _sessions_lock:
            _sessions[token] = session

        # Wait for video segments before writing master playlist
        _wait_segments_pattern(tmp_dir, "seg_v*.ts", SEGMENT_WAIT_COUNT, SEGMENT_WAIT_TIMEOUT)

        # Write master.m3u8
        lines = ["#EXTM3U"]
        default = "YES"
        for i, track in enumerate(audio_tracks):
            lang   = (track.get("language") or "und").lower()
            name   = track.get("title") or lang.upper()
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
        # Single audio: classic combined video+audio mpegts
        track = audio_tracks[0] if audio_tracks else None
        if track and track["codec"] in _BROWSER_AUDIO_OK and track.get("channels", 2) <= 2:
            a_codec_args = ["-c:a:0", "copy"]
        else:
            a_codec_args = _aac_args(0)

        cmd = [
            "ffmpeg", "-y", "-i", cdn_url,
            "-map", "0:v:0",
            *((["-map", "0:a:0"] + a_codec_args) if track else ["-an"]),
            "-c:v", "copy",
            "-hls_time", "6", "-hls_list_size", "0",
            "-hls_flags", "independent_segments",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(tmp_dir / "seg%05d.ts"),
            str(tmp_dir / "playlist.m3u8"),
        ]
        log.info("web_player: starting FFmpeg (single-audio) for token=%s", token)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        session = HLSSession(token=token, proc=proc, tmp_dir=tmp_dir)
        session.start_heartbeat()
        with _sessions_lock:
            _sessions[token] = session

    return session


def _wait_segments(tmp_dir: Path, count: int, timeout: float) -> bool:
    return _wait_segments_pattern(tmp_dir, "seg*.ts", count, timeout)


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
            log.info("web_player: cleaned up idle session token=%s", token)


# ── DB ─────────────────────────────────────────────────────────────────────────

def _db_get_web_player_token(imdb_id: str,
                              season: int | None,
                              episode: int | None) -> dict | None:
    with db._connect() as c:
        return c.execute(
            "SELECT * FROM virtual_items "
            "WHERE imdb_id=? AND source='web_player' "
            "  AND (season IS ? OR season=?) "
            "  AND (episode IS ? OR episode=?) "
            "LIMIT 1",
            (imdb_id, season, season, episode, episode),
        ).fetchone()
