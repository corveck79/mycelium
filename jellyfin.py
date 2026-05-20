import logging

import requests

from config import JELLYFIN_API_KEY, JELLYFIN_URL

log = logging.getLogger(__name__)


def refresh_library(timeout: int = 30) -> bool:
    if not JELLYFIN_URL:
        log.warning("JELLYFIN_URL not set; skipping library refresh")
        return False
    url = f"{JELLYFIN_URL.rstrip('/')}/Library/Refresh"
    headers = {}
    if JELLYFIN_API_KEY:
        headers["X-Emby-Token"] = JELLYFIN_API_KEY
    log.info("Triggering Jellyfin library refresh: %s", url)
    resp = requests.post(url, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        log.error("Jellyfin refresh failed: %s %s", resp.status_code, resp.text[:200])
        return False
    log.info("Jellyfin library refresh accepted (%s)", resp.status_code)
    return True


def _jf_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if JELLYFIN_API_KEY:
        h["X-Emby-Token"] = JELLYFIN_API_KEY
    return h


def merge_duplicate_versions(timeout: int = 60) -> bool:
    """Find duplicate movies in Jellyfin and merge their versions."""
    if not JELLYFIN_URL:
        log.warning("JELLYFIN_URL not set; skipping MergeVersions")
        return False

    base = JELLYFIN_URL.rstrip("/")
    headers = _jf_headers()

    try:
        resp = requests.get(
            f"{base}/Items",
            headers=headers,
            params={"IncludeItemTypes": "Movie", "Recursive": "true",
                    "Fields": "ProviderIds", "Limit": 5000},
            timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json().get("Items") or []
    except Exception as exc:
        log.error("Jellyfin MergeVersions: could not fetch movies: %s", exc)
        return False

    # Group by IMDb/TMDB provider ID when available (most reliable — collapses
    # name variants, year mismatches, and 4K-vs-HD folders into one entry).
    # Fall back to normalised name only when an item carries no provider ID.
    import re as _re
    groups: dict[str, list[str]] = {}
    for item in items:
        provider = item.get("ProviderIds") or {}
        imdb = provider.get("Imdb") or provider.get("imdb")
        tmdb = provider.get("Tmdb") or provider.get("tmdb")
        if imdb:
            key = f"imdb:{imdb}"
        elif tmdb:
            key = f"tmdb:{tmdb}"
        else:
            key = "name:" + _re.sub(r"\s*\(\d{4}\)\s*$", "", item.get("Name") or "").strip().lower()
        groups.setdefault(key, []).append(item["Id"])

    merged = 0
    for name, ids in groups.items():
        if len(ids) < 2:
            continue
        try:
            r = requests.post(
                f"{base}/Videos/MergeVersions",
                headers=headers,
                params={"Ids": ",".join(ids)},
                timeout=timeout,
            )
            if r.status_code < 400:
                log.info("Merged %d versions of '%s'", len(ids), name)
                merged += 1
            else:
                log.debug("Merge failed for '%s': %s", name, r.status_code)
        except Exception as exc:
            log.debug("Merge error for '%s': %s", name, exc)

    log.info("Jellyfin MergeVersions: merged %d duplicate group(s)", merged)
    return True


def refresh_missing_images(timeout: int = 10) -> int:
    """Find movies and series in Jellyfin without a primary image and trigger a refresh."""
    if not JELLYFIN_URL:
        log.warning("JELLYFIN_URL not set; skipping refresh_missing_images")
        return 0

    base = JELLYFIN_URL.rstrip("/")
    headers = _jf_headers()

    try:
        resp = requests.get(
            f"{base}/Items",
            headers=headers,
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series",
                "Fields": "ImageTags",
                "Limit": 5000,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json().get("Items") or []
    except Exception as exc:
        log.error("refresh_missing_images: could not fetch items: %s", exc)
        return 0

    count = 0
    for item in items:
        if "Primary" in (item.get("ImageTags") or {}):
            continue
        item_id = item["Id"]
        try:
            r = requests.post(
                f"{base}/Items/{item_id}/Refresh",
                headers=headers,
                params={
                    "MetadataRefreshMode": "Default",
                    "ImageRefreshMode": "FullRefresh",
                    "ReplaceAllMetadata": "false",
                    "ReplaceAllImages": "false",
                },
                timeout=timeout,
            )
            if r.status_code < 400:
                log.info("Triggered image refresh for: %s", item.get("Name"))
                count += 1
            else:
                log.debug("Image refresh failed for %s: %s", item.get("Name"), r.status_code)
        except Exception as exc:
            log.debug("Image refresh error for %s: %s", item.get("Name"), exc)

    log.info("refresh_missing_images: triggered refresh for %d item(s) without poster", count)
    return count
