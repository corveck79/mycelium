import logging

import requests

from config import SEERR_API_KEY, SEERR_URL

log = logging.getLogger(__name__)


def _headers() -> dict[str, str]:
    return {"X-Api-Key": SEERR_API_KEY} if SEERR_API_KEY else {}


def get_request(request_id: str | int, timeout: int = 10) -> dict:
    if not SEERR_URL:
        raise RuntimeError("SEERR_URL is not configured")
    url = f"{SEERR_URL.rstrip('/')}/api/v1/request/{request_id}"
    log.info("Fetching Seerr request: %s", url)
    resp = requests.get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json() or {}


def list_approved_requests(take: int = 20, skip: int = 0, timeout: int = 10) -> list[dict]:
    if not SEERR_URL:
        raise RuntimeError("SEERR_URL is not configured")
    url = f"{SEERR_URL.rstrip('/')}/api/v1/request"
    params = {"filter": "approved", "take": take, "skip": skip}
    log.info("Fetching approved Seerr requests (take=%d skip=%d)", take, skip)
    resp = requests.get(url, headers=_headers(), params=params, timeout=timeout)
    resp.raise_for_status()
    return (resp.json() or {}).get("results", [])
