"""RealDebrid client with same surface as torbox.py.

Provides check_cached() + add_magnet() so the multi-debrid layer can fall back
to RealDebrid when TorBox doesn't have a release cached. Disabled by default.
"""
import logging
import time

import requests

import settings as _settings
from config import (
    REALDEBRID_API_KEY as _REALDEBRID_API_KEY_DEFAULT,
    REALDEBRID_BASE_URL,
    TORBOX_POLL_INTERVAL_SEC,
    TORBOX_POLL_TIMEOUT_SEC,
)

log = logging.getLogger(__name__)


def _api_key() -> str:
    return _settings.get("REALDEBRID_API_KEY", _REALDEBRID_API_KEY_DEFAULT)


def is_configured() -> bool:
    return bool(_api_key())


def _headers() -> dict:
    return {"Authorization": f"Bearer {_api_key()}"}


def check_cached(hashes: list[str], timeout: int = 15) -> set[str]:
    """Return the subset of hashes RealDebrid has cached."""
    if not is_configured() or not hashes:
        return set()
    cached: set[str] = set()
    # RD wants hashes as a / separated path; chunk to keep URLs sane
    for i in range(0, len(hashes), 25):
        chunk = hashes[i : i + 25]
        url = f"{REALDEBRID_BASE_URL.rstrip('/')}/torrents/instantAvailability/{'/'.join(chunk)}"
        try:
            r = requests.get(url, headers=_headers(), timeout=timeout)
            r.raise_for_status()
            data = r.json() or {}
        except requests.RequestException as exc:
            log.warning("RealDebrid instantAvailability failed: %s", exc)
            continue
        for h, hosters in data.items():
            # hosters is a dict; non-empty means at least one cached variant
            if isinstance(hosters, dict) and hosters:
                cached.add(h.lower())
    log.info("RealDebrid cache check: %d/%d cached", len(cached), len(hashes))
    return cached


def add_magnet(magnet: str, timeout: int = 30) -> dict:
    """Add a magnet and auto-select all files. Returns the torrent dict."""
    if not is_configured():
        raise RuntimeError("RealDebrid not configured")
    url = f"{REALDEBRID_BASE_URL.rstrip('/')}/torrents/addMagnet"
    log.info("RealDebrid: adding magnet %s", magnet[:80])
    r = requests.post(url, headers=_headers(), data={"magnet": magnet}, timeout=timeout)
    r.raise_for_status()
    data = r.json() or {}
    rd_id = data.get("id")
    if not rd_id:
        raise RuntimeError(f"RealDebrid addMagnet returned no id: {data}")
    # Select all files so RD starts unrestricting
    sel = requests.post(
        f"{REALDEBRID_BASE_URL.rstrip('/')}/torrents/selectFiles/{rd_id}",
        headers=_headers(), data={"files": "all"}, timeout=timeout,
    )
    sel.raise_for_status()
    return {"id": rd_id, "hash": data.get("uri", "")}


def get_info(rd_id: str, timeout: int = 15) -> dict | None:
    try:
        r = requests.get(
            f"{REALDEBRID_BASE_URL.rstrip('/')}/torrents/info/{rd_id}",
            headers=_headers(), timeout=timeout,
        )
        r.raise_for_status()
        return r.json() or None
    except requests.RequestException as exc:
        log.debug("RealDebrid info failed: %s", exc)
        return None


def wait_until_ready(rd_id: str) -> dict | None:
    """Poll RealDebrid for completion."""
    deadline = time.monotonic() + TORBOX_POLL_TIMEOUT_SEC
    last_status: str | None = None
    while time.monotonic() < deadline:
        info = get_info(rd_id)
        if info:
            status = info.get("status") or ""
            if status != last_status:
                log.info("RealDebrid status: %s", status)
                last_status = status
            if status == "downloaded":
                return info
        time.sleep(TORBOX_POLL_INTERVAL_SEC)
    log.warning("RealDebrid: timed out waiting for %s", rd_id)
    return None


def unrestrict_link(link: str, timeout: int = 15) -> str | None:
    """Convert a RealDebrid hoster link to a direct streaming URL."""
    try:
        r = requests.post(
            f"{REALDEBRID_BASE_URL.rstrip('/')}/unrestrict/link",
            headers=_headers(), data={"link": link}, timeout=timeout,
        )
        r.raise_for_status()
        return (r.json() or {}).get("download")
    except Exception as exc:
        log.warning("RealDebrid unrestrict failed: %s", exc)
        return None


_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts", ".m2ts")


def _is_video(name: str) -> bool:
    n = (name or "").lower()
    return n.endswith(_VIDEO_EXTS)


def _selected_with_links(info: dict) -> list[tuple[dict, str]]:
    """Pair each selected file with its corresponding RD hoster link."""
    files = info.get("files") or []
    links = info.get("links") or []
    selected = [f for f in files if f.get("selected")]
    return [(f, links[idx]) for idx, f in enumerate(selected) if idx < len(links)]


def get_main_video_url(rd_id: str) -> str | None:
    """For a ready RD torrent, pick the largest video file and return an
    unrestricted CDN URL for it. Returns None if RD couldn't deliver."""
    info = get_info(rd_id)
    if not info:
        return None
    pairs = _selected_with_links(info)
    video_files = [(f, link) for f, link in pairs if _is_video(f.get("path") or f.get("name") or "")]
    if not video_files:
        return None
    # Skip obvious trailers (< 200 MB) when we have larger files
    big = [(f, link) for f, link in video_files if (f.get("bytes") or 0) >= 200 * 1024 * 1024]
    pool = big or video_files
    main = max(pool, key=lambda fl: fl[0].get("bytes") or 0)
    return unrestrict_link(main[1])


def get_video_files_with_urls(rd_id: str) -> list[tuple[dict, str]]:
    """For a ready RD torrent (typically a season pack), return (file_dict,
    unrestricted_url) for every video file. Used to fan out per-episode
    .strm files."""
    info = get_info(rd_id)
    if not info:
        return []
    pairs = _selected_with_links(info)
    out: list[tuple[dict, str]] = []
    for f, hoster_link in pairs:
        path = f.get("path") or f.get("name") or ""
        if not _is_video(path):
            continue
        # Skip tiny files (likely featurettes/extras)
        if (f.get("bytes") or 0) < 50 * 1024 * 1024:
            continue
        direct = unrestrict_link(hoster_link)
        if direct:
            out.append((f, direct))
    return out


def torrent_name(rd_id: str) -> str:
    info = get_info(rd_id) or {}
    return info.get("filename") or info.get("original_filename") or ""
