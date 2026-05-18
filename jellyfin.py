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
