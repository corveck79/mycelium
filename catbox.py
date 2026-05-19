"""Catbox-style lazy materialization for TorBox.

When CATBOX_MODE is enabled, .strm files contain a proxy URL pointing to
/stream/<token>. On playback the webhook ensures the torrent is in TorBox
(re-adding from the cached magnet if it has been released), fetches a fresh
CDN URL, and 307-redirects the client.

After CATBOX_IDLE_MINUTES of inactivity an item is removed from TorBox to
stay within TorBox's 30-day cache retention policy. The virtual entry stays
in the DB so playback works again on the next request.
"""
import logging
import uuid
from datetime import datetime, timedelta

import db
import torbox
from config import CATBOX_HOST, CATBOX_IDLE_MINUTES

log = logging.getLogger(__name__)


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
    """Ensure the torrent is in TorBox and return a fresh stream URL."""
    item = db.get_virtual_item(token)
    if not item:
        log.warning("Catbox: unknown token %s", token)
        return None

    torbox_id = item["torbox_id"]
    if torbox_id:
        live = torbox.find_by_id(torbox_id)
        if not live or not torbox._is_ready(live):
            torbox_id = None

    if not torbox_id:
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
