import logging

import jellyfin
import torbox
import torrentio
from webhook_parser import MediaRequest

log = logging.getLogger(__name__)


def _process_movie(req: MediaRequest) -> bool:
    streams = torrentio.fetch_streams("movie", req.imdb_id)
    best = torrentio.pick_best(streams)
    if not best:
        log.error("No suitable stream for movie %s (%s)", req.title, req.imdb_id)
        return False
    torbox.add_magnet(best.magnet)
    torbox.wait_until_ready(best.info_hash)
    return True


def _process_season(req: MediaRequest, season: int) -> bool:
    # First episode lookup to discover season packs.
    streams = torrentio.fetch_streams("series", req.imdb_id, season=season, episode=1)
    season_pack = torrentio.pick_best(streams, prefer_season_pack=True)
    if season_pack and season_pack.is_season_pack:
        log.info("Using season pack for %s S%02d", req.title, season)
        torbox.add_magnet(season_pack.magnet)
        torbox.wait_until_ready(season_pack.info_hash)
        return True

    log.info("No season pack for %s S%02d; falling back to per-episode", req.title, season)
    added = 0
    episode = 1
    # Walk episodes until Torrentio returns nothing.
    while True:
        ep_streams = (
            streams if episode == 1 else torrentio.fetch_streams("series", req.imdb_id, season=season, episode=episode)
        )
        if not ep_streams:
            log.info("No more episodes returned at S%02dE%02d", season, episode)
            break
        best = torrentio.pick_best(ep_streams)
        if best:
            torbox.add_magnet(best.magnet)
            torbox.wait_until_ready(best.info_hash)
            added += 1
        episode += 1
        if episode > 50:  # safety cap
            log.warning("Episode cap (50) reached for %s S%02d", req.title, season)
            break
    return added > 0


def process(req: MediaRequest) -> bool:
    log.info("Processing request: %s [%s] %s", req.title, req.media_type, req.imdb_id)
    success = False
    try:
        if req.is_movie:
            success = _process_movie(req)
        else:
            for season in req.seasons:
                if _process_season(req, season):
                    success = True
    finally:
        if success:
            jellyfin.refresh_library()
        else:
            log.warning("No content added; skipping Jellyfin refresh for %s", req.title)
    return success
