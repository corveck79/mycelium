import logging
import threading
import time
from collections import deque

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


# ── createtorrent rate-limit visibility ───────────────────────────────────────
# TorBox limits POST /torrents/createtorrent to 60/hour per API token. We keep a
# rolling 1-hour log of every call (with the reason / caller) so the UI can show
# exactly what is consuming the quota.
_CREATETORRENT_LOG: deque = deque(maxlen=200)
_CREATETORRENT_LOCK = threading.Lock()


def _record_createtorrent(reason: str) -> None:
    now = time.time()
    with _CREATETORRENT_LOCK:
        _CREATETORRENT_LOG.append((now, reason))


def createtorrent_usage(window_sec: int = 3600) -> dict:
    """Return how many createtorrent calls happened in the last `window_sec`,
    broken down by reason. Used by the UI to explain rate-limit hits."""
    cutoff = time.time() - window_sec
    with _CREATETORRENT_LOCK:
        recent = [(ts, reason) for ts, reason in _CREATETORRENT_LOG if ts >= cutoff]
    by_reason: dict[str, int] = {}
    for _, reason in recent:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    oldest = min((ts for ts, _ in recent), default=None)
    return {
        "count": len(recent),
        "limit": 60,
        "window_sec": window_sec,
        "by_reason": by_reason,
        "oldest_ts": oldest,
        "resets_in_sec": int(oldest + window_sec - time.time()) if oldest else 0,
    }


_CREATETORRENT_LIMIT = 60  # TorBox: 60 createtorrent calls per hour per token


class RateLimited(Exception):
    """Raised (proactively) when the local createtorrent budget is exhausted, so
    we never even send a request we know TorBox will reject with 429."""


def add_magnet(magnet: str, timeout: int = 30, reason: str = "unknown") -> dict:
    url = f"{TORBOX_BASE_URL.rstrip('/')}/torrents/createtorrent"
    # Client-side guard: don't burn requests we know will be 429'd. Leave a
    # small headroom (58) so concurrent callers / clock skew don't overshoot.
    usage = createtorrent_usage()
    if usage["count"] >= _CREATETORRENT_LIMIT - 2:
        log.warning("createtorrent [%s] SKIPPED — local quota %d/%d reached (resets ~%ds)",
                    reason, usage["count"], _CREATETORRENT_LIMIT, usage["resets_in_sec"])
        raise RateLimited()
    _record_createtorrent(reason)
    log.info("createtorrent [%s] (%d/60 this hour): %s", reason, usage["count"] + 1, magnet[:80])
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
    """Return TorBox mylist (all pages), cached for ~45s."""
    import time as _t
    if not force_refresh:
        cached = _mylist_cache["items"]
        if cached is not None and (_t.monotonic() - _mylist_cache["ts"]) < _MYLIST_TTL_SECONDS:
            return cached
    url = f"{TORBOX_BASE_URL.rstrip('/')}/torrents/mylist"
    all_items: list[dict] = []
    seen_ids: set[int] = set()
    offset = 0
    limit = 1000
    for _ in range(20):  # max 20 pages = 20 000 items; guards against infinite loop
        resp = requests.get(url, headers=_headers(), timeout=timeout,
                            params={"limit": limit, "offset": offset})
        resp.raise_for_status()
        payload = resp.json() or {}
        page = payload.get("data", []) or []
        new = [t for t in page if t.get("id") not in seen_ids]
        if not new:
            break
        all_items.extend(new)
        seen_ids.update(t["id"] for t in new)
        if len(page) < limit:
            break
        offset += limit
    with _mylist_lock:
        _mylist_cache["items"] = all_items
        _mylist_cache["ts"] = _t.monotonic()
    return all_items


def invalidate_mylist_cache() -> None:
    """Drop the mylist cache so the next list_torrents() hits TorBox fresh."""
    with _mylist_lock:
        _mylist_cache["items"] = None
        _mylist_cache["ts"] = 0.0


def _matches_hash(item: dict, info_hash: str) -> bool:
    candidate = (item.get("hash") or "").lower()
    return candidate == info_hash.lower()


def find_by_hash(info_hash: str, force_refresh: bool = False) -> dict | None:
    for item in list_torrents(force_refresh=force_refresh):
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


def wait_until_ready(info_hash: str, timeout: int | None = None) -> dict | None:
    """Poll Torbox until the torrent reports completion or the timeout is reached.
    timeout defaults to TORBOX_POLL_TIMEOUT_SEC; pass a smaller value for
    latency-sensitive paths like on-play re-materialization."""
    limit = TORBOX_POLL_TIMEOUT_SEC if timeout is None else timeout
    deadline = time.monotonic() + limit
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
