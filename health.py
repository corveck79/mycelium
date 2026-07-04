import logging

import requests

import config
import settings
from config import TORRENTIO_BASE_URL

log = logging.getLogger(__name__)


def _s(key: str) -> str:
    """Settings DB value with env/config fallback, trimmed."""
    return (settings.get(key, getattr(config, key, "")) or "").strip()


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
        f"{_s('TORBOX_BASE_URL').rstrip('/')}/torrents/mylist",
        headers={"Authorization": f"Bearer {settings.get('TORBOX_API_KEY', '')}"},
    ))
    if settings.get("ZILEAN_ENABLED", False):
        services.append(_ping("Zilean", f"{_s('ZILEAN_URL').rstrip('/')}/healthz"))
    else:
        services.append({"name": "Zilean", "status": "disabled"})
    services.append(_ping("Torrentio", f"{TORRENTIO_BASE_URL.rstrip('/')}/manifest.json"))
    tmdb_api_key = _s("TMDB_API_KEY")
    if tmdb_api_key:
        services.append(_ping(
            "TMDB",
            "https://api.themoviedb.org/3/configuration",
            headers={"Authorization": f"Bearer {tmdb_api_key}", "Accept": "application/json"},
        ))
    else:
        services.append({"name": "TMDB", "status": "disabled"})
    jellyfin_url = _s("JELLYFIN_URL")
    if jellyfin_url:
        jellyfin_key = _s("JELLYFIN_API_KEY")
        services.append(_ping(
            "Jellyfin",
            f"{jellyfin_url.rstrip('/')}/System/Info/Public",
            headers={"X-Emby-Token": jellyfin_key} if jellyfin_key else {},
        ))
    seerr_url = _s("SEERR_URL")
    if seerr_url:
        seerr_key = _s("SEERR_API_KEY")
        services.append(_ping(
            "Seerr",
            f"{seerr_url.rstrip('/')}/api/v1/status",
            headers={"X-Api-Key": seerr_key} if seerr_key else {},
        ))
    return services
