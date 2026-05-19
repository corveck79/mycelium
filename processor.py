import logging
import time
from typing import Optional

import db
import jellyfin
import monitor
import notify
import strm_generator
import torbox
import torrentio
import zilean
from config import ZILEAN_ENABLED
from torrentio import TorrentioStream
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


def _try_add_magnet(stream: TorrentioStream, label: str) -> bool:
    """Try adding a single magnet to TorBox. On 429 waits 15s and retries once."""
    for attempt in range(2):
        try:
            torbox.add_magnet(stream.magnet)
            torbox.wait_until_ready(stream.info_hash)
            return True
        except Exception as exc:
            resp = getattr(exc, "response", None)
            if resp is not None and resp.status_code == 429:
                if attempt == 0:
                    log.warning("Rate limited (429) adding %s — waiting 15s then retrying", label)
                    time.sleep(15)
                    continue
            log.warning("Failed to add %s (hash=%s): %s", label, stream.info_hash, exc)
            return False
    return False


def _add_best_from(candidates: list, label: str) -> tuple[bool, Optional[TorrentioStream]]:
    """Check cache, try best cached candidate first, fall back to second-best on failure.
    Returns (success, winning_stream).
    """
    cached_hashes = torbox.check_cached([s.info_hash for s in candidates])

    cached = [s for s in candidates if s.info_hash in cached_hashes]
    uncached = [s for s in candidates if s.info_hash not in cached_hashes]

    if cached:
        log.info("%d/%d candidate(s) cached for %s — trying best cached", len(cached), len(candidates), label)
        to_try = cached[:2]
    else:
        log.info("No cached candidates for %s — trying best uncached", label)
        to_try = uncached[:2]

    for i, stream in enumerate(to_try):
        if i > 0:
            time.sleep(2)
        if _try_add_magnet(stream, label):
            return True, stream
        log.warning("Candidate %d/%d failed for %s — %s", i + 1, len(to_try), label,
                    "trying next" if i + 1 < len(to_try) else "giving up")

    log.error("All candidate(s) failed for %s", label)
    return False, None


def _process_movie(req: MediaRequest) -> tuple[bool, Optional[TorrentioStream]]:
    candidates = _fetch_movie_candidates(req)
    if not candidates:
        log.error("No suitable stream for movie %s (%s)", req.title, req.imdb_id)
        return False, None
    log.info("Trying %d candidate(s) for %s", len(candidates), req.title)
    return _add_best_from(candidates, req.title)


def _process_season(req: MediaRequest, season: int) -> tuple[bool, Optional[TorrentioStream]]:
    pack_candidates = _fetch_season_candidates(req, season, episode=1, prefer_season_pack=True)

    if pack_candidates and pack_candidates[0].is_season_pack:
        log.info("Trying season pack(s) for %s S%02d", req.title, season)
        packs = [s for s in pack_candidates if s.is_season_pack]
        ok, winner = _add_best_from(packs, f"{req.title} S{season:02d} pack")
        if ok:
            return True, winner
        log.info("Season pack(s) failed; falling back to per-episode")

    log.info("Going per-episode for %s S%02d", req.title, season)
    added = 0
    first_winner: Optional[TorrentioStream] = None
    episode = 1
    while True:
        if episode == 1:
            candidates = [s for s in pack_candidates if not s.is_season_pack] or pack_candidates
        else:
            candidates = _fetch_season_candidates(req, season, episode=episode)
        if not candidates:
            log.info("No more episodes returned at S%02dE%02d", season, episode)
            break
        ok, winner = _add_best_from(candidates, f"{req.title} S{season:02d}E{episode:02d}")
        if ok:
            added += 1
            first_winner = first_winner or winner
        episode += 1
        if episode > 50:
            log.warning("Episode cap (50) reached for %s S%02d", req.title, season)
            break
    return added > 0, first_winner


def process(req: MediaRequest) -> bool:
    log.info("Processing request: %s [%s] %s", req.title, req.media_type, req.imdb_id)
    row_id = db.insert_request(req.title, req.imdb_id, req.media_type, req.seasons)
    success = False
    winner: Optional[TorrentioStream] = None
    try:
        if req.is_movie:
            success, winner = _process_movie(req)
        else:
            for season in req.seasons:
                ok, w = _process_season(req, season)
                if ok:
                    success = True
                    winner = winner or w
    except Exception as exc:
        log.exception("Unexpected error processing %s", req.title)
        db.update_request(row_id, "failed", error=str(exc))
        return False

    if success:
        db.update_request(
            row_id, "success",
            quality=winner.quality if winner else None,
            source=winner.name.split()[0] if winner else None,
            info_hash=winner.info_hash if winner else None,
        )
        if not req.is_movie:
            monitor.add_series(req.imdb_id, req.title, req.seasons)
        item = torbox.find_by_hash(winner.info_hash) if winner else None
        torrent_id = item.get('id') if item else None
        if torrent_id:
            strm_generator.create_strm_for_torrent(torrent_id, req.title, req.media_type)
        jellyfin.refresh_library()
        quality = winner.quality if winner else "?"
        db.log_activity("added", req.title, f"{req.media_type} · {quality}", True)
        notify.send(f"Added: {req.title}", f"{req.media_type} · {quality} · {req.imdb_id}", True)
    else:
        db.update_request(row_id, "failed")
        log.warning("No content added; skipping Jellyfin refresh for %s", req.title)
        db.log_activity("failed", req.title, f"no suitable stream found for {req.imdb_id}", False)
        notify.send(f"Failed: {req.title}", f"No suitable stream found · {req.imdb_id}", False)

    return success
