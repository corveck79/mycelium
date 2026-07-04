"""Light-weight cached service-health probe.

Other modules call is_up(name) to decide whether to skip a service. Caches
results for HEALTH_CACHE_SECONDS so we never block a hot path on a probe.
"""
import logging
import threading
import time

import requests

import settings as _settings
from config import (
    HEALTH_CACHE_SECONDS,
    TORRENTIO_BASE_URL,
    ZILEAN_URL as _ZILEAN_URL_DEFAULT,
)

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, tuple[bool, float]] = {}


def _probe(name: str) -> bool:
    try:
        if name == "zilean":
            zilean_url = _settings.get("ZILEAN_URL", _ZILEAN_URL_DEFAULT)
            if not zilean_url:
                return False
            r = requests.get(f"{zilean_url.rstrip('/')}/healthz", timeout=3)
            return r.status_code < 500
        if name == "torrentio":
            r = requests.get(f"{TORRENTIO_BASE_URL.rstrip('/')}/manifest.json", timeout=3)
            return r.status_code < 500
    except Exception as exc:
        log.debug("health probe %s failed: %s", name, exc)
        return False
    return True


def is_up(name: str) -> bool:
    if name == "zilean" and (
        not _settings.get("ZILEAN_ENABLED", False)
        or not _settings.get("ZILEAN_URL", _ZILEAN_URL_DEFAULT)
    ):
        return False
    now = time.monotonic()
    with _lock:
        cached = _cache.get(name)
        if cached and now - cached[1] < HEALTH_CACHE_SECONDS:
            return cached[0]
    ok = _probe(name)
    with _lock:
        _cache[name] = (ok, now)
    if not ok:
        log.warning("Service %s reported down; will skip for %ds", name, HEALTH_CACHE_SECONDS)
    return ok
