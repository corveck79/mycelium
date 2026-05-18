"""On-startup catch-up: process approved Seerr requests that arrived while the service was down."""

import logging
import threading
import time

import processor
import seerr
import tmdb
import torbox
from config import CATCHUP_DELAY_SEC, CATCHUP_TAKE
from webhook_parser import MediaRequest

log = logging.getLogger(__name__)


def _build_request(item: dict) -> MediaRequest | None:
    media = item.get("media") or {}
    raw_type = (media.get("mediaType") or media.get("media_type") or "").lower()
    if raw_type == "movie":
        media_type = "movie"
    elif raw_type in ("tv", "series"):
        media_type = "series"
    else:
        log.debug("Catch-up: skipping request %s — unknown media type %r", item.get("id"), raw_type)
        return None

    imdb_id = media.get("imdbId") or media.get("imdb_id") or None
    if not imdb_id:
        tmdb_id = media.get("tmdbId") or media.get("tmdb_id")
        if tmdb_id:
            imdb_id = tmdb.tmdb_to_imdb(tmdb_id, media_type=raw_type if raw_type in ("movie", "tv") else "movie")
    if not imdb_id:
        log.debug("Catch-up: skipping request %s — no IMDB ID (tmdbId=%s)", item.get("id"), media.get("tmdbId"))
        return None

    title = media.get("title") or str(imdb_id)

    seasons: list[int] = []
    if media_type == "series":
        for s in item.get("seasons") or []:
            num = s.get("seasonNumber")
            if isinstance(num, int) and num > 0:
                seasons.append(num)
        if not seasons:
            seasons = [1]

    return MediaRequest(title=title, media_type=media_type, imdb_id=imdb_id, seasons=seasons)


def run() -> None:
    log.info("Catch-up: fetching up to %d approved requests from Seerr", CATCHUP_TAKE)
    try:
        items = seerr.list_approved_requests(take=CATCHUP_TAKE)
    except Exception as exc:
        log.error("Catch-up: failed to fetch Seerr requests: %s", exc)
        return

    log.info("Catch-up: %d approved request(s) to check", len(items))

    try:
        torbox_list = torbox.list_torrents()
    except Exception as exc:
        log.error("Catch-up: failed to fetch TorBox list: %s", exc)
        torbox_list = []

    torbox_names = {(item.get("name") or "").lower() for item in torbox_list}

    def _in_torbox(title: str) -> bool:
        needle = title.lower()
        return any(needle in name or name in needle for name in torbox_names)

    for item in items:
        req = _build_request(item)
        if req is None:
            continue
        if _in_torbox(req.title):
            log.info("Catch-up: '%s' already in TorBox — skipping", req.title)
            continue
        log.info("Catch-up: processing missed request '%s' (%s)", req.title, req.imdb_id)
        try:
            processor.process(req)
        except Exception as exc:
            log.error("Catch-up: error processing '%s': %s", req.title, exc)

    log.info("Catch-up complete")


def schedule() -> None:
    def _delayed():
        log.info("Catch-up: waiting %ds before starting", CATCHUP_DELAY_SEC)
        time.sleep(CATCHUP_DELAY_SEC)
        run()

    thread = threading.Thread(target=_delayed, name="catchup", daemon=True)
    thread.start()
