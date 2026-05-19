"""Catbox-style lazy materialization for TorBox.

When CATBOX_MODE is enabled, .strm files contain a proxy URL pointing to
/stream/<token>. On playback the webhook ensures the torrent is in TorBox
(re-adding from the cached magnet if it has been released), fetches a fresh
CDN URL, and 307-redirects the client.

After CATBOX_IDLE_MINUTES of inactivity an item is removed from TorBox to
stay within TorBox's 30-day cache retention policy. The virtual entry stays
in the DB so playback works again on the next request.

Resolved CDN URLs are cached in-memory per token to avoid hammering TorBox's
60/hour createtorrent + 300/min general rate limits when Jellyfin sends
multiple probe/seek requests for the same item in quick succession.
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta

import db
import torbox
from config import CATBOX_HOST, CATBOX_IDLE_MINUTES

log = logging.getLogger(__name__)


_URL_CACHE_TTL_SEC = 1800  # 30 minutes — well within TorBox CDN URL validity
_url_cache: dict[str, tuple[str, float]] = {}
_url_cache_lock = threading.Lock()

_token_locks: dict[str, threading.Lock] = {}
_token_locks_lock = threading.Lock()


def _token_lock(token: str) -> threading.Lock:
    with _token_locks_lock:
        lock = _token_locks.get(token)
        if lock is None:
            lock = threading.Lock()
            _token_locks[token] = lock
        return lock


def _cache_get(token: str) -> str | None:
    with _url_cache_lock:
        entry = _url_cache.get(token)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        if entry:
            del _url_cache[token]
    return None


def _cache_put(token: str, url: str) -> None:
    with _url_cache_lock:
        _url_cache[token] = (url, time.monotonic() + _URL_CACHE_TTL_SEC)


def invalidate_url_cache(token: str | None = None) -> None:
    with _url_cache_lock:
        if token is None:
            _url_cache.clear()
        else:
            _url_cache.pop(token, None)


def proxy_url(token: str) -> str:
    return f"{CATBOX_HOST.rstrip('/')}/stream/{token}"


def register(info_hash: str, magnet: str, title: str, media_type: str,
             strm_path: str | None = None, torbox_id: int | None = None,
             file_id: int | None = None) -> str:
    token = uuid.uuid4().hex[:16]
    db.insert_virtual_item(token, info_hash, magnet, title, media_type,
                            strm_path=strm_path, torbox_id=torbox_id, file_id=file_id)
    return token


def materialize(token: str) -> str | None:
    """Ensure the torrent is in TorBox and return a fresh stream URL.
    Cached URLs are served for up to 5 minutes to absorb Jellyfin's probe/seek
    bursts without spending TorBox createtorrent rate-limit slots."""
    cached = _cache_get(token)
    if cached:
        db.touch_virtual_item(token)
        return cached

    with _token_lock(token):
        cached = _cache_get(token)
        if cached:
            db.touch_virtual_item(token)
            return cached
        url = _materialize_locked(token)
        if url:
            _cache_put(token, url)
        return url


def _materialize_locked(token: str) -> str | None:
    item = db.get_virtual_item(token)
    if not item:
        log.warning("Catbox: unknown token %s", token)
        try:
            import metrics_prom
            metrics_prom.catbox_stream_total.labels(result="failed").inc()
        except Exception:
            pass
        return None

    torbox_id = item["torbox_id"]
    rematerialized = False
    if torbox_id:
        live = torbox.find_by_id(torbox_id)
        if not live or not torbox._is_ready(live):
            torbox_id = None
            rematerialized = True

    if not torbox_id:
        rematerialized = True
        log.info("Catbox: re-adding %s (%s)", item["title"], item["info_hash"])
        try:
            torbox.add_magnet(item["magnet"])
            live = torbox.wait_until_ready(item["info_hash"])
            if not live:
                log.error("Catbox: torrent never became ready: %s", item["info_hash"])
                return None
            torbox_id = live["id"]
            db.update_virtual_torbox_id(token, torbox_id)
        except Exception as exc:
            log.error("Catbox: add_magnet failed for %s: %s", token, exc)
            return None

    file_id = item["file_id"]
    if not file_id:
        live = torbox.find_by_id(torbox_id)
        if live:
            import strm_generator
            if item["media_type"] == "movie":
                main = strm_generator._pick_main_movie_file(live.get("files") or [])
            else:
                videos = [f for f in (live.get("files") or [])
                          if strm_generator._is_video(f.get("name") or "")
                          and not strm_generator._is_trailer(f)]
                main = max(videos, key=lambda f: f.get("size") or 0) if videos else None
            if main:
                file_id = main["id"]
                db.update_virtual_file_id(token, file_id)

    if not file_id:
        log.error("Catbox: no playable file found for %s", token)
        return None

    import strm_generator
    url = strm_generator._get_stream_url(torbox_id, file_id)
    if url:
        db.touch_virtual_item(token)
        try:
            import metrics_prom
            metrics_prom.catbox_stream_total.labels(
                result="rematerialized" if rematerialized else "ok",
            ).inc()
        except Exception:
            pass
    else:
        try:
            import metrics_prom
            metrics_prom.catbox_stream_total.labels(result="failed").inc()
        except Exception:
            pass
    return url


def release_idle() -> int:
    """Remove TorBox items idle longer than CATBOX_IDLE_MINUTES. Returns count released."""
    cutoff = datetime.utcnow() - timedelta(minutes=CATBOX_IDLE_MINUTES)
    cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    items = db.get_idle_virtual_items(cutoff_iso)
    released = 0
    for item in items:
        if torbox.delete_torrent(item["torbox_id"]):
            db.update_virtual_torbox_id(item["token"], None)
            log.info("Catbox: released idle torrent %s (%s)", item["torbox_id"], item["title"])
            released += 1
    if released:
        log.info("Catbox: released %d idle torrent(s)", released)
    return released
