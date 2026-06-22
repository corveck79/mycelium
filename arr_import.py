"""Bulk import from existing Radarr/Sonarr instances into Mycelium.

For each Radarr movie: kick off processor.process() to find a release and add
to TorBox (skips if already in our request history).

For each Sonarr series: upsert into monitored_series so the regular monitor
will pull all (monitored) seasons + episodes.
"""
import logging
import threading
import time

import db
import processor
import radarr
import settings as _settings
import sonarr
import tmdb
from config import RADARR_API_KEY, RADARR_URL, SONARR_API_KEY, SONARR_URL
from webhook_parser import MediaRequest


def _radarr_url() -> str:
    return _settings.get("RADARR_URL", RADARR_URL) or ""


def _radarr_key() -> str:
    return _settings.get("RADARR_API_KEY", RADARR_API_KEY) or ""


def _sonarr_url() -> str:
    return _settings.get("SONARR_URL", SONARR_URL) or ""


def _sonarr_key() -> str:
    return _settings.get("SONARR_API_KEY", SONARR_API_KEY) or ""

log = logging.getLogger(__name__)

_import_lock = threading.Lock()
_status: dict = {"running": False, "kind": None, "total": 0, "done": 0,
                  "added": 0, "skipped": 0, "errors": 0, "started": None,
                  "finished": None, "message": ""}


def get_status() -> dict:
    return dict(_status)


def _set(**kw):
    _status.update(kw)


def import_radarr(only_monitored: bool = True, throttle_sec: float = 1.0) -> dict:
    """Pull all (monitored) Radarr movies and queue them for processing."""
    url, key = _radarr_url(), _radarr_key()
    if not (url and key):
        raise RuntimeError("RADARR_URL/RADARR_API_KEY not configured")

    with _import_lock:
        if _status["running"]:
            raise RuntimeError(f"Another import is already running: {_status['kind']}")
        _set(running=True, kind="radarr", total=0, done=0, added=0, skipped=0,
              errors=0, started=time.time(), finished=None, message="Fetching from Radarr…")

    try:
        movies = radarr.list_movies(url, key)
        if only_monitored:
            movies = [m for m in movies if m.get("monitored")]
        _set(total=len(movies), message=f"Importing {len(movies)} movie(s)")

        existing = {r["imdb_id"] for r in db.get_recent(5000)
                    if r.get("media_type") == "movie"}

        for idx, m in enumerate(movies, start=1):
            imdb_id = m.get("imdb_id") or ""
            title = m.get("title") or ""
            if not imdb_id and m.get("tmdb_id"):
                imdb_id = tmdb.tmdb_to_imdb(m["tmdb_id"], media_type="movie") or ""
            if not imdb_id:
                log.warning("Radarr import: no imdb_id for %s  -  skipping", title)
                _status["skipped"] += 1
                _status["done"] = idx
                continue
            if imdb_id in existing:
                log.debug("Radarr import: %s already known (%s)  -  skipping", title, imdb_id)
                _status["skipped"] += 1
                _status["done"] = idx
                continue
            try:
                req = MediaRequest(title=title, media_type="movie", imdb_id=imdb_id, seasons=[])
                processor.process(req)
                existing.add(imdb_id)
                _status["added"] += 1
            except Exception as exc:
                log.warning("Radarr import: processor failed for %s: %s", title, exc)
                _status["errors"] += 1
            _status["done"] = idx
            time.sleep(throttle_sec)

        _set(message=f"Done  -  added {_status['added']}, skipped {_status['skipped']},"
                       f" errors {_status['errors']}")
        return get_status()
    finally:
        _set(running=False, finished=time.time())


def import_sonarr(only_monitored: bool = True) -> dict:
    """Pull all (monitored) Sonarr series and add them to monitored_series."""
    url, key = _sonarr_url(), _sonarr_key()
    if not (url and key):
        raise RuntimeError("SONARR_URL/SONARR_API_KEY not configured")

    with _import_lock:
        if _status["running"]:
            raise RuntimeError(f"Another import is already running: {_status['kind']}")
        _set(running=True, kind="sonarr", total=0, done=0, added=0, skipped=0,
              errors=0, started=time.time(), finished=None, message="Fetching from Sonarr…")

    try:
        series = sonarr.list_series(url, key)
        if only_monitored:
            series = [s for s in series if s.get("monitored")]
        _set(total=len(series), message=f"Importing {len(series)} series")

        existing = {s["imdb_id"] for s in db.get_all_monitored_series()}

        for idx, s in enumerate(series, start=1):
            imdb_id = s.get("imdb_id") or ""
            title = s.get("title") or ""
            tmdb_id = s.get("tmdb_id")
            if not imdb_id and tmdb_id:
                imdb_id = tmdb.tmdb_to_imdb(tmdb_id, media_type="tv") or ""
            if not imdb_id:
                log.warning("Sonarr import: no imdb_id for %s  -  skipping", title)
                _status["skipped"] += 1
                _status["done"] = idx
                continue
            if imdb_id in existing:
                _status["skipped"] += 1
                _status["done"] = idx
                continue
            try:
                seasons = s.get("seasons") or []
                if not seasons:
                    if tmdb_id:
                        show = tmdb.get_show_info(tmdb_id) or {}
                        seasons = list(range(1, (show.get("number_of_seasons") or 1) + 1))
                    else:
                        seasons = [1]

                # --- FOOLPROOF TITLE INTERCEPT FOR SONARR ---
                import re
                if re.match(r'^tt\d{7,}$', title) and tmdb_id:
                    show_info = tmdb.get_show_info(tmdb_id) or {}
                    if show_info.get("name"):
                        title = show_info["name"]
                # --------------------------------------------

                db.upsert_monitored_series(imdb_id, tmdb_id, title, seasons)
                existing.add(imdb_id)
                _status["added"] += 1
            except Exception as exc:
                log.warning("Sonarr import: upsert failed for %s: %s", title, exc)
                _status["errors"] += 1
            _status["done"] = idx

        _set(message=f"Done  -  added {_status['added']}, skipped {_status['skipped']},"
                       f" errors {_status['errors']}")
        return get_status()
    finally:
        _set(running=False, finished=time.time())
