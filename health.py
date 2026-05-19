import logging

import requests

from config import (
    JELLYFIN_API_KEY,
    JELLYFIN_URL,
    SEERR_API_KEY,
    SEERR_URL,
    TMDB_API_KEY,
    TORBOX_API_KEY,
    TORBOX_BASE_URL,
    TORRENTIO_BASE_URL,
    ZILEAN_ENABLED,
    ZILEAN_URL,
)

log = logging.getLogger(__name__)


def _ping(name: str, url: str, headers: dict | None = None, timeout: int = 5) -> dict:
    try:
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        ok = r.status_code < 500
        return {"name": name, "status": "ok" if ok else "down", "code": r.status_code}
    except Exception as exc:
        return {"name": name, "status": "down", "error": str(exc)[:80]}


def check_all() -> list[dict]:
    services = []
    services.append(_ping(
        "TorBox",
        f"{TORBOX_BASE_URL.rstrip('/')}/torrents/mylist",
        headers={"Authorization": f"Bearer {TORBOX_API_KEY}"},
    ))
    if ZILEAN_ENABLED:
        services.append(_ping("Zilean", f"{ZILEAN_URL.rstrip('/')}/healthz"))
    else:
        services.append({"name": "Zilean", "status": "disabled"})
    services.append(_ping("Torrentio", f"{TORRENTIO_BASE_URL.rstrip('/')}/manifest.json"))
    if TMDB_API_KEY:
        services.append(_ping(
            "TMDB",
            "https://api.themoviedb.org/3/configuration",
            headers={"Authorization": f"Bearer {TMDB_API_KEY}", "Accept": "application/json"},
        ))
    else:
        services.append({"name": "TMDB", "status": "disabled"})
    if JELLYFIN_URL:
        services.append(_ping(
            "Jellyfin",
            f"{JELLYFIN_URL.rstrip('/')}/System/Info/Public",
            headers={"X-Emby-Token": JELLYFIN_API_KEY} if JELLYFIN_API_KEY else {},
        ))
    if SEERR_URL:
        services.append(_ping(
            "Seerr",
            f"{SEERR_URL.rstrip('/')}/api/v1/status",
            headers={"X-Api-Key": SEERR_API_KEY} if SEERR_API_KEY else {},
        ))
    return services
