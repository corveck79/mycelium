import logging
import time
from typing import Optional

import blacklist
import db
import health_cache
import jellyfin
import locks
import monitor
import notify
import strm_generator
import torbox
import torrentio
import zilean
import settings as _settings
from torrentio import TorrentioStream
from webhook_parser import MediaRequest

log = logging.getLogger(__name__)

# Transient per-imdb failure reasons captured during a process() call, surfaced
# into the requests.error column so the UI can show why something failed.
_LAST_FAIL_REASON: dict[str, str] = {}


class RateLimited(Exception):
    """Raised when TorBox returns 429 and the short in-call retry is exhausted.
    Signals the caller to reschedule via the retry queue rather than marking
    the request permanently failed (and without blacklisting the torrent)."""


def _rank(streams, prefer_season_pack: bool = False, override: dict | None = None):
    return torrentio.rank_streams(streams, prefer_season_pack=prefer_season_pack, override=override)


def _fetch_movie_candidates(req: MediaRequest) -> list:
    override = db.get_show_override(req.imdb_id)
    if _settings.get("ZILEAN_ENABLED", False) and health_cache.is_up("zilean"):
        streams = zilean.fetch_streams(req.imdb_id)
        candidates = _rank(streams, override=override)
        if candidates:
            log.info("Zilean found %d candidate(s) for movie %s", len(candidates), req.title)
            return candidates
        log.info("Zilean: no candidates for %s; falling back to Torrentio", req.title)
    if not health_cache.is_up("torrentio"):
        log.warning("Torrentio appears down; no candidates")
        return []
    streams = torrentio.fetch_streams("movie", req.imdb_id)
    return _rank(streams, override=override)


def _fetch_season_candidates(req: MediaRequest, season: int, episode: int, prefer_season_pack: bool = False) -> list:
    override = db.get_show_override(req.imdb_id)
    if _settings.get("ZILEAN_ENABLED", False) and health_cache.is_up("zilean"):
        streams = zilean.fetch_streams(req.imdb_id, season=season, episode=episode)
        candidates = _rank(streams, prefer_season_pack=prefer_season_pack, override=override)
        if candidates:
            log.info("Zilean found %d candidate(s) for %s S%02dE%02d", len(candidates), req.title, season, episode)
            return candidates
        log.info("Zilean: no candidates for %s S%02dE%02d; falling back to Torrentio", req.title, season, episode)
    if not health_cache.is_up("torrentio"):
        log.warning("Torrentio appears down; no candidates")
        return []
    streams = torrentio.fetch_streams("series", req.imdb_id, season=season, episode=episode)
    return _rank(streams, prefer_season_pack=prefer_season_pack, override=override)


def _is_429(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    return "429" in str(exc)


def _try_add_magnet(stream: TorrentioStream, label: str) -> bool:
    """Add a single magnet to TorBox. Raises RateLimited (without blacklisting)
    when the hourly createtorrent budget is gone, so the request is rescheduled
    rather than wasting the quota or marking a good torrent bad. We do NOT retry
    a 429 inline — the hourly window won't reset in seconds."""
    # Skip createtorrent entirely if this hash is already in our TorBox library —
    # re-adding it would waste a 60/hour quota slot for content we already have.
    existing = torbox.find_by_hash(stream.info_hash)
    if existing and torbox._is_ready(existing):
        log.info("Already in TorBox library (id=%s) — skipping createtorrent for %s",
                 existing.get("id"), label)
        return True
    try:
        torbox.add_magnet(stream.magnet, reason="processor")
        torbox.wait_until_ready(stream.info_hash)
        return True
    except torbox.RateLimited:
        log.warning("createtorrent budget exhausted adding %s — will retry later", label)
        raise RateLimited()
    except Exception as exc:
        if _is_429(exc):
            log.warning("Rate limited (429) adding %s — will retry later", label)
            raise RateLimited()
        log.warning("Failed to add %s (hash=%s): %s", label, stream.info_hash, exc)
        blacklist.record_failure(stream.info_hash, str(exc)[:200])
        return False


def _add_best_from(candidates: list, label: str) -> tuple[bool, Optional[TorrentioStream]]:
    """Check cache, try best cached candidate first, fall back to second-best on failure.
    Returns (success, winning_stream).
    """
    candidates = blacklist.filter_candidates(candidates)
    if not candidates:
        log.warning("All candidates for %s are blacklisted", label)
        return False, None
    import debrid
    multi = debrid.check_cached_multi([s.info_hash for s in candidates])
    cached_hashes = multi.get("torbox", set())
    rd_only = (multi.get("realdebrid", set()) or set()) - cached_hashes
    if rd_only:
        log.info("Multi-debrid: %d candidate(s) cached on RealDebrid but not TorBox (informational)", len(rd_only))

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


def _lazy_register_movie(req: MediaRequest, candidates: list) -> Optional[TorrentioStream]:
    """Catbox lazy mode: pick the best cached candidate and register a virtual
    .strm WITHOUT adding to TorBox. createtorrent is deferred to first play.
    Returns the registered stream, or None to fall back to eager add."""
    candidates = blacklist.filter_candidates(candidates)
    if not candidates:
        return None
    import debrid
    cached_hashes = debrid.check_cached_multi([s.info_hash for s in candidates]).get("torbox", set())
    cached = [s for s in candidates if s.info_hash in cached_hashes]
    # Prefer a cached release so first playback is instant; otherwise best overall.
    winner = (cached or candidates)[0]
    year = strm_generator._extract_year(winner.name) or strm_generator._extract_year(winner.title)
    if not year:
        try:
            import tmdb
            info = tmdb._get(f"/find/{req.imdb_id}", params={"external_source": "imdb_id"})
            results = (info or {}).get("movie_results") or []
            if results:
                year = int((results[0].get("release_date") or "0000")[:4]) or None
        except Exception:
            year = None
    if strm_generator.create_lazy_movie_strm(winner.info_hash, winner.magnet, req.title, year):
        log.info("Lazy-registered movie %s (cached=%s) — createtorrent deferred to first play",
                 req.title, winner.info_hash in cached_hashes)
        return winner
    log.info("Lazy registration skipped for %s (strm exists?) — falling back to eager", req.title)
    return None


def _process_movie(req: MediaRequest) -> tuple[bool, Optional[TorrentioStream]]:
    candidates = _fetch_movie_candidates(req)
    if not candidates:
        log.error("No suitable stream for movie %s (%s)", req.title, req.imdb_id)
        _LAST_FAIL_REASON[req.imdb_id] = "no releases found on Zilean/Torrentio"
        return False, None
    log.info("Trying %d candidate(s) for %s", len(candidates), req.title)

    # Lazy materialization: register without spending a createtorrent slot.
    if _settings.get("CATBOX_MODE", False) and _settings.get("CATBOX_LAZY_ADD", False):
        winner = _lazy_register_movie(req, candidates)
        if winner:
            return True, winner

    try:
        ok, winner = _add_best_from(candidates, req.title)
    except RateLimited:
        # TorBox quota gone — try RealDebrid (no createtorrent limit) before
        # giving up, so we can still serve the title right now.
        log.info("TorBox rate limited for %s — trying RealDebrid fallback first", req.title)
        fallback = _try_realdebrid_fallback(req.title, candidates)
        if fallback:
            return True, fallback
        raise  # nothing on RD either — reschedule via retry queue
    if ok:
        return ok, winner
    # TorBox add failed (not rate limit). Try RD cached fallback.
    fallback = _try_realdebrid_fallback(req.title, candidates)
    if fallback:
        return True, fallback
    _LAST_FAIL_REASON[req.imdb_id] = (
        f"{len(candidates)} release(s) found but none could be added to TorBox or RealDebrid"
    )
    return False, None


def _try_realdebrid_fallback(title: str, candidates: list,
                              media_type: str = "movie",
                              season: int | None = None,
                              episode: int | None = None) -> Optional[TorrentioStream]:
    """Add the best RD-cached candidate via RealDebrid and write .strm file(s).

    media_type='movie'   : largest non-trailer video file -> single .strm
    media_type='series'  : assumes season pack, fans out per-episode .strm files
    media_type='episode' : single-episode torrent, requires season + episode args
    """
    try:
        import realdebrid
    except ImportError as exc:
        log.debug("RD fallback: realdebrid module not importable: %s", exc)
        return None
    if not _settings.get("MULTI_DEBRID_ENABLED", False) or not realdebrid.is_configured():
        return None
    candidates = blacklist.filter_candidates(candidates)
    rd_cached = realdebrid.check_cached([c.info_hash for c in candidates[:20]])
    rd_candidates = [c for c in candidates if c.info_hash in rd_cached]
    if not rd_candidates:
        log.info("RD fallback: no candidates cached on RealDebrid for %s", title)
        return None
    log.info("RD fallback (%s): %d cached on RD — trying best", media_type, len(rd_candidates))
    for cand in rd_candidates[:2]:
        try:
            added = realdebrid.add_magnet(cand.magnet)
            rd_id = added.get("id")
            if not rd_id:
                continue
            realdebrid.wait_until_ready(rd_id)
            if media_type == "movie":
                url = realdebrid.get_main_video_url(rd_id)
                if not url:
                    continue
                strm_generator.create_movie_strm_from_url(title, url)
            elif media_type == "episode":
                if season is None or episode is None:
                    log.error("RD fallback episode: season/episode missing")
                    continue
                url = realdebrid.get_main_video_url(rd_id)
                if not url:
                    continue
                strm_generator.create_episode_strm_from_url(title, season, episode, url)
            else:
                pairs = realdebrid.get_video_files_with_urls(rd_id)
                if not pairs:
                    log.warning("RD fallback: no video files for %s", title)
                    continue
                tname = realdebrid.torrent_name(rd_id) or cand.name
                written = strm_generator.create_series_strms_from_files(tname, pairs)
                if written == 0:
                    log.warning("RD fallback: 0 episodes parsed from %s", tname)
                    continue
                log.info("RD fallback: %d episode .strm(s) written for %s", written, title)
            log.info("RD fallback: served %s via RealDebrid (hash=%s)", title, cand.info_hash)
            return cand
        except Exception as exc:
            log.warning("RD fallback failed for %s (%s): %s", title, cand.info_hash, exc)
            blacklist.record_failure(cand.info_hash, f"rd: {exc}")
    return None


def _process_season(req: MediaRequest, season: int) -> tuple[bool, Optional[TorrentioStream]]:
    pack_candidates = _fetch_season_candidates(req, season, episode=1, prefer_season_pack=True)

    if pack_candidates and pack_candidates[0].is_season_pack:
        log.info("Trying season pack(s) for %s S%02d", req.title, season)
        packs = [s for s in pack_candidates if s.is_season_pack]
        try:
            ok, winner = _add_best_from(packs, f"{req.title} S{season:02d} pack")
        except RateLimited:
            log.info("TorBox rate limited — trying RD pack fallback for %s S%02d", req.title, season)
            rd_winner = _try_realdebrid_fallback(
                f"{req.title} S{season:02d}", packs, media_type="series",
            )
            if rd_winner:
                return True, rd_winner
            raise
        if ok:
            return True, winner
        rd_winner = _try_realdebrid_fallback(
            f"{req.title} S{season:02d}", packs, media_type="series",
        )
        if rd_winner:
            return True, rd_winner
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
        try:
            ok, winner = _add_best_from(candidates, f"{req.title} S{season:02d}E{episode:02d}")
        except RateLimited:
            # TorBox quota gone mid-season — try RD for this episode, then stop
            # adding more (leave the rest for the retry queue) to respect quota.
            rd_winner = _try_realdebrid_fallback(
                req.title, candidates,
                media_type="episode", season=season, episode=episode,
            )
            if rd_winner:
                added += 1
                first_winner = first_winner or rd_winner
            if added:
                return True, first_winner
            raise
        if not ok:
            rd_winner = _try_realdebrid_fallback(
                req.title, candidates,
                media_type="episode", season=season, episode=episode,
            )
            if rd_winner:
                ok = True
                winner = rd_winner
        if ok:
            added += 1
            first_winner = first_winner or winner
        episode += 1
        if episode > 50:
            log.warning("Episode cap (50) reached for %s S%02d", req.title, season)
            break
    return added > 0, first_winner


def process(req: MediaRequest, _retry_attempt: int = 0) -> bool:
    with locks.imdb_mutex(req.imdb_id, blocking=False) as got:
        if not got:
            # Another worker is already processing this imdb. Re-queue ourselves
            # for 60 seconds so we don't lose a webhook-triggered request that
            # collided with a retry-queue trigger (and vice versa).
            log.info("Skip: %s already in flight; re-queueing in 60s", req.imdb_id)
            try:
                db.enqueue_retry(
                    req.imdb_id, req.title, req.media_type, req.seasons,
                    _retry_attempt, delay_seconds=60,
                )
            except Exception:
                log.exception("Could not re-enqueue %s after mutex miss", req.imdb_id)
            return False
        return _process_locked(req, _retry_attempt)


def _process_locked(req: MediaRequest, _retry_attempt: int) -> bool:
    log.info("Processing request: %s [%s] %s (attempt %d)",
             req.title, req.media_type, req.imdb_id, _retry_attempt)
    started = time.monotonic()
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
    except RateLimited:
        # TorBox 429 — not a real failure. Reschedule and surface a clear status.
        _LAST_FAIL_REASON.pop(req.imdb_id, None)
        db.update_request(row_id, "rate_limited",
                          error="TorBox rate limit (60/hour) hit — will retry automatically")
        import retry_queue
        retry_queue.schedule(req, _retry_attempt)
        log.warning("Rate limited processing %s — rescheduled via retry queue", req.title)
        db.record_metric("request_rate_limited", req.media_type, value_int=1)
        return False
    except Exception as exc:
        log.exception("Unexpected error processing %s", req.title)
        db.update_request(row_id, "failed", error=str(exc))
        import retry_queue
        retry_queue.schedule(req, _retry_attempt)
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
        # RD fallback already wrote its .strm before returning; nothing to do here.
            # Best-effort subtitle fetch
            try:
                import subtitles
                from pathlib import Path
                from config import MEDIA_PATH
                if req.is_movie:
                    # Find newest .strm in movies for this title (rough match)
                    media = Path(MEDIA_PATH) / "movies"
                    for p in sorted(media.rglob("*.strm"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
                        subtitles.fetch_for(p, req.imdb_id, "movie")
            except Exception as exc:
                log.debug("Subtitle fetch skipped: %s", exc)
        jellyfin.refresh_library()
        quality = winner.quality if winner else "?"
        db.log_activity("added", req.title, f"{req.media_type} · {quality}", True)
        notify.send(f"Added: {req.title}", f"{req.media_type} · {quality} · {req.imdb_id}", True)
        # Metrics
        elapsed = time.monotonic() - started
        db.record_metric("latency_seconds", req.media_type, value_real=elapsed)
        try:
            import metrics_prom
            metrics_prom.requests_total.labels(media_type=req.media_type, status="success").inc()
            metrics_prom.request_duration_seconds.labels(media_type=req.media_type).observe(elapsed)
        except Exception as exc:
            log.debug("metrics_prom (success) failed: %s", exc)
        if winner:
            db.record_metric("quality_added", winner.quality, value_int=1)
            source = (winner.name.split()[0] if winner.name else "?").lower()
            db.record_metric("source_win", source, value_int=1)
            try:
                import metrics_prom
                metrics_prom.quality_added_total.labels(quality=winner.quality or "unknown").inc()
                metrics_prom.source_wins_total.labels(source=source).inc()
            except Exception as exc:
                log.debug("metrics_prom (quality) failed: %s", exc)
    else:
        reason = _LAST_FAIL_REASON.pop(req.imdb_id, None) or "no suitable stream found"
        db.update_request(row_id, "failed", error=reason)
        log.warning("No content added (%s); skipping Jellyfin refresh for %s", reason, req.title)
        db.log_activity("failed", req.title, f"{reason} ({req.imdb_id})", False)
        notify.send(f"Failed: {req.title}", f"No suitable stream found · {req.imdb_id}", False)
        db.record_metric("request_failed", req.media_type, value_int=1)
        try:
            import metrics_prom
            metrics_prom.requests_total.labels(media_type=req.media_type, status="failed").inc()
        except Exception as exc:
            log.debug("metrics_prom (failed) skipped: %s", exc)
        import retry_queue
        retry_queue.schedule(req, _retry_attempt)

    return success
