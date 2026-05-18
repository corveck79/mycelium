import logging
import time

import jellyfin
import torbox
import torrentio
import zilean
from config import JELLYFIN_REFRESH_DELAY_SEC, ZILEAN_ENABLED
from webhook_parser import MediaRequest

log = logging.getLogger(__name__)


def _rank(streams, prefer_season_pack: bool = False):
    return torrentio.rank_streams(streams, prefer_season_pack=prefer_season_pack)


def _fetch_movie_candidates(req: MediaRequest) -> list:
    if ZILEAN_ENABLED:
        streams = zilean.fetch_streams(req.imdb_id)
        candidates = _rank(streams)
        if candidates:
            log.info("Zilean found %d candidate(s) for movie %s", len(candidates), req.title)
            return candidates
        log.info("Zilean: no candidates for %s; falling back to Torrentio", req.title)
    streams = torrentio.fetch_streams("movie", req.imdb_id)
    return _rank(streams)


def _fetch_season_candidates(req: MediaRequest, season: int, episode: int, prefer_season_pack: bool = False) -> list:
    if ZILEAN_ENABLED:
        streams = zilean.fetch_streams(req.imdb_id, season=season, episode=episode)
        candidates = _rank(streams, prefer_season_pack=prefer_season_pack)
        if candidates:
            log.info("Zilean found %d candidate(s) for %s S%02dE%02d", len(candidates), req.title, season, episode)
            return candidates
        log.info("Zilean: no candidates for %s S%02dE%02d; falling back to Torrentio", req.title, season, episode)
    streams = torrentio.fetch_streams("series", req.imdb_id, season=season, episode=episode)
    return _rank(streams, prefer_season_pack=prefer_season_pack)


def _add_best_from(candidates: list, label: str) -> bool:
    """Check cache, prefer cached candidates, fall back to adding uncached if none cached."""
    cached_hashes = torbox.check_cached([s.info_hash for s in candidates])

    cached = [s for s in candidates if s.info_hash in cached_hashes]
    uncached = [s for s in candidates if s.info_hash not in cached_hashes]

    if cached:
        log.info("%d/%d candidate(s) cached for %s — using cached", len(cached), len(candidates), label)
    else:
        log.info("No cached candidates for %s — will add best uncached", label)

    # Try cached first (ranked order preserved); fall back to best uncached.
    for stream in (cached or uncached[:1]):
        try:
            torbox.add_magnet(stream.magnet)
            torbox.wait_until_ready(stream.info_hash)
            return True
        except Exception as exc:
            log.warning(
                "Failed to add %s (hash=%s quality=%s cached=%s): %s — trying next",
                label, stream.info_hash, stream.quality, stream.info_hash in cached_hashes, exc,
            )
    log.error("All candidate(s) failed for %s", label)
    return False


def _process_movie(req: MediaRequest) -> bool:
    candidates = _fetch_movie_candidates(req)
    if not candidates:
        log.error("No suitable stream for movie %s (%s)", req.title, req.imdb_id)
        return False
    log.info("Trying %d candidate(s) for %s", len(candidates), req.title)
    return _add_best_from(candidates, req.title)


def _process_season(req: MediaRequest, season: int) -> bool:
    pack_candidates = _fetch_season_candidates(req, season, episode=1, prefer_season_pack=True)

    if pack_candidates and pack_candidates[0].is_season_pack:
        log.info("Trying season pack(s) for %s S%02d", req.title, season)
        packs = [s for s in pack_candidates if s.is_season_pack]
        if _add_best_from(packs, f"{req.title} S{season:02d} pack"):
            return True
        log.info("Season pack(s) failed; falling back to per-episode")

    log.info("Going per-episode for %s S%02d", req.title, season)
    added = 0
    episode = 1
    while True:
        if episode == 1:
            candidates = [s for s in pack_candidates if not s.is_season_pack] or pack_candidates
        else:
            candidates = _fetch_season_candidates(req, season, episode=episode)
        if not candidates:
            log.info("No more episodes returned at S%02dE%02d", season, episode)
            break
        if _add_best_from(candidates, f"{req.title} S{season:02d}E{episode:02d}"):
            added += 1
        episode += 1
        if episode > 50:
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
            if JELLYFIN_REFRESH_DELAY_SEC > 0:
                log.info("Waiting %ds before triggering Jellyfin refresh", JELLYFIN_REFRESH_DELAY_SEC)
                time.sleep(JELLYFIN_REFRESH_DELAY_SEC)
            jellyfin.refresh_library()
        else:
            log.warning("No content added; skipping Jellyfin refresh for %s", req.title)
    return success
