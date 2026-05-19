import logging
import time

import requests

from config import (
    TORBOX_BASE_URL,
    TORBOX_POLL_INTERVAL_SEC,
    TORBOX_POLL_TIMEOUT_SEC,
)

log = logging.getLogger(__name__)


def _headers() -> dict[str, str]:
    import settings
    return {"Authorization": f"Bearer {settings.get('TORBOX_API_KEY', '')}"}


def add_magnet(magnet: str, timeout: int = 30) -> dict:
    url = f"{TORBOX_BASE_URL.rstrip('/')}/torrents/createtorrent"
    log.info("Adding magnet to Torbox: %s", magnet[:80])
    resp = requests.post(url, headers=_headers(), data={"magnet": magnet}, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}
    if not payload.get("success", False):
        # DUPLICATE_ITEM means the torrent is already in TorBox — treat as success
        if payload.get("error") == "DUPLICATE_ITEM":
            log.info("Torbox: torrent already exists (DUPLICATE_ITEM), treating as success")
            invalidate_mylist_cache()
            return payload.get("data", {}) or {}
        raise RuntimeError(f"Torbox add failed: {payload}")
    log.info("Torbox createtorrent response: %s", payload.get("detail") or payload.get("data"))
    invalidate_mylist_cache()
    return payload.get("data", {}) or {}


_MYLIST_TTL_SECONDS = 45
_mylist_cache: dict = {"items": None, "ts": 0.0}
_mylist_lock = __import__("threading").Lock()


def list_torrents(timeout: int = 30, force_refresh: bool = False) -> list[dict]:
    """Return TorBox mylist, cached for ~45s. Use force_refresh=True after a
    successful add or delete to avoid serving a stale view."""
    import time as _t
    if not force_refresh:
        cached = _mylist_cache["items"]
        if cached is not None and (_t.monotonic() - _mylist_cache["ts"]) < _MYLIST_TTL_SECONDS:
            return cached
    url = f"{TORBOX_BASE_URL.rstrip('/')}/torrents/mylist"
    resp = requests.get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}
    items = payload.get("data", []) or []
    with _mylist_lock:
        _mylist_cache["items"] = items
        _mylist_cache["ts"] = _t.monotonic()
    return items


def invalidate_mylist_cache() -> None:
    """Drop the mylist cache so the next list_torrents() hits TorBox fresh."""
    with _mylist_lock:
        _mylist_cache["items"] = None
        _mylist_cache["ts"] = 0.0


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


def get_user_info(timeout: int = 10) -> dict | None:
    """Return TorBox user info (subscription, plan, etc) or None on failure."""
    url = f"{TORBOX_BASE_URL.rstrip('/')}/user/me"
    try:
        resp = requests.get(url, headers=_headers(), timeout=timeout)
        resp.raise_for_status()
        return (resp.json() or {}).get("data") or {}
    except Exception as exc:
        log.debug("TorBox user info failed: %s", exc)
        return None


def get_usage_summary() -> dict:
    """Derived usage info: torrent count, total bytes, active-state breakdown."""
    items = list_torrents()
    total_bytes = sum(t.get("size") or 0 for t in items)
    states: dict[str, int] = {}
    for t in items:
        s = (t.get("download_state") or "unknown").lower()
        states[s] = states.get(s, 0) + 1
    return {
        "torrent_count": len(items),
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / 1e9, 1),
        "states": states,
    }


# Track last warning to avoid spamming
_last_quota_warn: dict[str, float] = {}


def check_quota_and_warn(threshold_count: int = 200, threshold_gb: int = 4000) -> None:
    """Notify if torrent count or total size approaches the configured threshold.
    Re-warns at most once every 6 hours per metric."""
    import time
    import db
    import notify
    summary = get_usage_summary()
    now = time.monotonic()
    for metric, value, limit, fmt in (
        ("count", summary["torrent_count"], threshold_count, "%d torrents"),
        ("size", summary["total_gb"], threshold_gb, "%.1f GB"),
    ):
        if value < limit * 0.8:
            continue
        if now - _last_quota_warn.get(metric, 0) < 6 * 3600:
            continue
        _last_quota_warn[metric] = now
        msg = f"TorBox usage approaching limit: {fmt % value} (threshold {limit})"
        log.warning(msg)
        db.log_activity("quota_warn", "TorBox", msg, False)
        notify.send("TorBox quota warning", msg, success=False)


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
        invalidate_mylist_cache()
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
