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


def find_by_id(torrent_id: int) -> dict | None:
    for item in list_torrents():
        if item.get('id') == torrent_id:
            return item
    return None


def delete_torrent(torrent_id: int, timeout: int = 15) -> bool:
    url = f"{TORBOX_BASE_URL.rstrip('/')}/torrents/controltorrent"
    try:
        resp = requests.post(
            url, headers=_headers(),
            json={"torrent_id": torrent_id, "operation": "delete"},
            timeout=timeout,
        )
        resp.raise_for_status()
        log.info("Deleted TorBox torrent %s", torrent_id)
        return True
    except Exception as exc:
        log.warning("Delete torrent %s failed: %s", torrent_id, exc)
        return False


def check_cached(hashes: list[str], timeout: int = 15) -> set[str]:
    """Return the subset of hashes that TorBox has cached (instant download available)."""
    if not hashes:
        return set()
    url = f"{TORBOX_BASE_URL.rstrip('/')}/torrents/checkcached"
    params = {"hash": ",".join(hashes), "format": "object"}
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("TorBox checkcached failed: %s", exc)
        return set()
    data = (resp.json() or {}).get("data") or {}
    cached = {h.lower() for h in data.keys()}
    log.info("TorBox cache check: %d/%d hashes cached", len(cached), len(hashes))
    return cached


def title_exists(title: str) -> bool:
    """Return True if any torrent in mylist appears to match the given title."""
    needle = title.lower()
    for item in list_torrents():
        name = (item.get("name") or "").lower()
        if needle in name or name in needle:
            return True
    return False


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
