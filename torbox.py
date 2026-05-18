import logging
import time

import requests

from config import (
    TORBOX_API_KEY,
    TORBOX_BASE_URL,
    TORBOX_POLL_INTERVAL_SEC,
    TORBOX_POLL_TIMEOUT_SEC,
)

log = logging.getLogger(__name__)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TORBOX_API_KEY}"}


def add_magnet(magnet: str, timeout: int = 30) -> dict:
    url = f"{TORBOX_BASE_URL.rstrip('/')}/torrents/createtorrent"
    log.info("Adding magnet to Torbox: %s", magnet[:80])
    resp = requests.post(url, headers=_headers(), data={"magnet": magnet}, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}
    if not payload.get("success", False):
        raise RuntimeError(f"Torbox add failed: {payload}")
    log.info("Torbox createtorrent response: %s", payload.get("detail") or payload.get("data"))
    return payload.get("data", {}) or {}


def list_torrents(timeout: int = 30) -> list[dict]:
    url = f"{TORBOX_BASE_URL.rstrip('/')}/torrents/mylist"
    resp = requests.get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}
    return payload.get("data", []) or []


def _matches_hash(item: dict, info_hash: str) -> bool:
    candidate = (item.get("hash") or "").lower()
    return candidate == info_hash.lower()


def find_by_hash(info_hash: str) -> dict | None:
    for item in list_torrents():
        if _matches_hash(item, info_hash):
            return item
    return None


def _is_ready(item: dict) -> bool:
    if item.get("download_finished"):
        return True
    state = (item.get("download_state") or "").lower()
    return state in ("cached", "completed", "uploading", "metaDL_done")


def wait_until_ready(info_hash: str) -> dict | None:
    """Poll Torbox until the torrent reports completion or the timeout is reached."""
    deadline = time.monotonic() + TORBOX_POLL_TIMEOUT_SEC
    last_state: str | None = None
    while time.monotonic() < deadline:
        item = find_by_hash(info_hash)
        if item is None:
            log.debug("Torrent %s not in mylist yet", info_hash)
        else:
            state = item.get("download_state") or ""
            progress = item.get("progress") or 0
            if state != last_state:
                log.info("Torbox state: %s (progress=%.2f%%)", state, float(progress) * 100)
                last_state = state
            if _is_ready(item):
                log.info("Torbox reports torrent ready: %s", info_hash)
                return item
        time.sleep(TORBOX_POLL_INTERVAL_SEC)
    log.warning("Timed out waiting for Torbox to make %s available", info_hash)
    return find_by_hash(info_hash)
