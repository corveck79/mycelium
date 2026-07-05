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

# imdb_ids that failed because no acceptable-quality release exists yet  -  these
# go to the wanted_movies list and are rechecked periodically rather than
# being marked permanently failed.
_WANTED: dict[str, str] = {}


class RateLimited(Exception):
    """Raised when TorBox returns 429 and the short in-call retry is exhausted.
    Signals the caller to reschedule via the retry queue rather than marking
    the request permanently failed (and without blacklisting the torrent)."""


def _rank(streams, prefer_season_pack: bool = False, override: dict | None = None):
    return torrentio.rank_streams(streams, prefer_season_pack=prefer_season_pack, override=override)


def _movie_runtime_minutes(imdb_id: str) -> float | None:
    import tmdb
    try:
        sec = tmdb.get_movie_runtime_sec(imdb_id)
        return sec / 60.0 if sec else None
    except Exception:
        return None


def _episode_runtime_minutes(imdb_id: str, season: int, episode: int) -> float | None:
    import tmdb
    try:
        sec = tmdb.get_episode_runtime_sec(imdb_id, season, episode)
        return sec / 60.0 if sec else None
    except Exception:
        return None


def _fetch_movie_candidates(req: MediaRequest) -> list:
    override = dict(db.get_show_override(req.imdb_id) or {})
    override["runtime_minutes"] = _movie_runtime_minutes(req.imdb_id)
    streams: list[TorrentioStream] = []
    seen_hashes: set[str] = set()
    if _settings.get("ZILEAN_ENABLED", False) and health_cache.is_up("zilean"):
        for s in zilean.fetch_streams(req.imdb_id):
            if s.info_hash not in seen_hashes:
                seen_hashes.add(s.info_hash)
                streams.append(s)
    if health_cache.is_up("torrentio"):
        for s in torrentio.fetch_streams("movie", req.imdb_id):
            if s.info_hash not in seen_hashes:
                seen_hashes.add(s.info_hash)
                streams.append(s)
    if streams:
        log.info("Combined %d unique streams for movie %s (zilean+torrentio)", len(streams), req.title)
    return _rank(streams, override=override)


def _fetch_season_candidates(req: MediaRequest, season: int, episode: int, prefer_season_pack: bool = False) -> list:
    override = dict(db.get_show_override(req.imdb_id) or {})
    override["runtime_minutes"] = _episode_runtime_minutes(req.imdb_id, season, episode)
    streams: list[TorrentioStream] = []
    seen_hashes: set[str] = set()
    if _settings.get("ZILEAN_ENABLED", False) and health_cache.is_up("zilean"):
        for s in zilean.fetch_streams(req.imdb_id, season=season, episode=episode):
            if s.info_hash not in seen_hashes:
                seen_hashes.add(s.info_hash)
                streams.append(s)
    if health_cache.is_up("torrentio"):
        for s in torrentio.fetch_streams("series", req.imdb_id, season=season, episode=episode):
            if s.info_hash not in seen_hashes:
                seen_hashes.add(s.info_hash)
                streams.append(s)
    if streams:
        log.info("Combined %d unique streams for %s S%02dE%02d (zilean+torrentio)", len(streams), req.title, season, episode)
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
    a 429 inline  -  the hourly window won't reset in seconds."""
    # Skip createtorrent entirely if this hash is already in our TorBox library  -
    # re-adding it would waste a 60/hour quota slot for content we already have.
    try:
        existing = torbox.find_by_hash(stream.info_hash)
    except Exception as exc:
        log.warning("Library lookup failed for %s, proceeding as not-yet-added: %s", label, exc)
        existing = None
    if existing and torbox._is_ready(existing):
        log.info("Already in TorBox library (id=%s)  -  skipping createtorrent for %s",
                 existing.get("id"), label)
        return True
    try:
        torbox.add_magnet(stream.magnet, reason="processor")
        item = torbox.wait_until_ready(stream.info_hash)
        if not item or not torbox._is_ready(item):
            # wait_until_ready() timed out without TorBox ever reporting the
            # torrent ready. Don't create a .strm for a file that isn't there
            # yet: skip this candidate now, it'll be picked up again (already
            # in the library, likely finished by then) on the next retry/recheck.
            log.warning("Torbox still downloading %s after the poll timeout  -  "
                        "not creating a .strm yet, will recheck later", label)
            return False
        return True
    except torbox.RateLimited:
        log.warning("createtorrent budget exhausted adding %s  -  will retry later", label)
        raise RateLimited()
    except Exception as exc:
        if _is_429(exc):
            log.warning("Rate limited (429) adding %s  -  will retry later", label)
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
    try:
        multi = debrid.check_cached_multi([s.info_hash for s in candidates])
    except Exception as exc:
        # A network/API error here must not propagate: for series it would
        # bubble through the season loop in _process_locked and mark the
        # WHOLE request failed. Treat it like "nothing cached yet" instead.
        log.warning("Cache-check failed for %s, treating as uncached: %s", label, exc)
        multi = {}
    cached_hashes = multi.get("torbox", set())
    rd_only = (multi.get("realdebrid", set()) or set()) - cached_hashes
    if rd_only:
        log.info("Multi-debrid: %d candidate(s) cached on RealDebrid but not TorBox (informational)", len(rd_only))

    cached = [s for s in candidates if s.info_hash in cached_hashes]
    uncached = [s for s in candidates if s.info_hash not in cached_hashes]

    if cached:
        log.info("%d/%d candidate(s) cached for %s  -  trying best cached", len(cached), len(candidates), label)
        to_try = cached[:2]
    else:
        log.info("No cached candidates for %s  -  trying best uncached", label)
        to_try = uncached[:2]

    for i, stream in enumerate(to_try):
        if i > 0:
            time.sleep(2)
        if _try_add_magnet(stream, label):
            return True, stream
        log.warning("Candidate %d/%d failed for %s  -  %s", i + 1, len(to_try), label,
                    "trying next" if i + 1 < len(to_try) else "giving up")

    log.error("All candidate(s) failed for %s", label)
    return False, None


def _lazy_register_movie(req: MediaRequest, candidates: list) -> Optional[TorrentioStream]:
    """Catbox lazy mode: pick the best CACHED candidate and register a virtual
    .strm WITHOUT adding to TorBox. createtorrent is deferred to first play.
    Returns the registered stream, or None if nothing is cached yet."""
    candidates = blacklist.filter_candidates(candidates)
    if not candidates:
        return None
    import debrid
    try:
        cached_hashes = debrid.check_cached_multi([s.info_hash for s in candidates]).get("torbox", set())
    except Exception as exc:
        log.warning("Lazy cache-check failed for %s: %s", req.title, exc)
        return None
    cached = [s for s in candidates if s.info_hash in cached_hashes]
    if not cached:
        log.info("Lazy: no cached release for %s  -  will wait in wanted", req.title)
        return None
    winner = cached[0]
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
    if strm_generator.create_lazy_movie_strm(
        winner.info_hash, winner.magnet, req.title, year,
        imdb_id=req.imdb_id, tmdb_id=getattr(req, 'tmdb_id', None),
        quality=winner.quality, source=winner.source, size_gb=winner.size_gb,
    ):
        log.info("Lazy-registered movie %s (cached, %s)  -  createtorrent deferred to first play",
                 req.title, winner.quality)
        return winner
    # strm already exists  -  still a success, don't mark as wanted
    log.info("Lazy registration skipped for %s (strm already exists)  -  treating as success", req.title)
    return winner


def _process_movie(req: MediaRequest) -> tuple[bool, Optional[TorrentioStream]]:
    candidates = _fetch_movie_candidates(req)
    if not candidates:
        # No usable release yet (nothing found, or only cam rejected by
        # STRICT_NO_CAM). Mark "wanted" so we keep watching for an acceptable
        # release instead of failing permanently.
        reason = "no acceptable-quality release yet  -  watching for one"
        log.info("No usable stream for movie %s (%s)  -  marking wanted", req.title, req.imdb_id)
        _LAST_FAIL_REASON[req.imdb_id] = reason
        _WANTED[req.imdb_id] = reason
        return False, None
    log.info("Trying %d candidate(s) for %s", len(candidates), req.title)

    # Lazy materialization: register without spending a createtorrent slot.
    # In catbox mode we ONLY accept cached releases  -  uncached means we wait
    # in wanted until TorBox has it cached, so playback is always instant.
    if _settings.get("CATBOX_MODE", False) and _settings.get("CATBOX_LAZY_ADD", False):
        winner = _lazy_register_movie(req, candidates)
        if winner:
            return True, winner
        reason = "no cached release yet  -  waiting for TorBox to cache it"
        log.info("Catbox: no cached release for %s  -  marking wanted", req.title)
        _LAST_FAIL_REASON[req.imdb_id] = reason
        _WANTED[req.imdb_id] = reason
        return False, None

    try:
        ok, winner = _add_best_from(candidates, req.title)
    except RateLimited:
        # TorBox quota gone  -  try RealDebrid (no createtorrent limit) before
        # giving up, so we can still serve the title right now.
        log.info("TorBox rate limited for %s  -  trying RealDebrid fallback first", req.title)
        fallback = _try_realdebrid_fallback(req.title, candidates)
        if fallback:
            return True, fallback
        raise  # nothing on RD either  -  reschedule via retry queue
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
    log.info("RD fallback (%s): %d cached on RD  -  trying best", media_type, len(rd_candidates))
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


def _get_season_episode_count(imdb_id: str, season: int) -> int:
    """Ask TMDB how many episodes a season has. Returns 0 on failure."""
    try:
        import tmdb
        tmdb_id = tmdb.find_by_imdb(imdb_id, kind="tv")
        if not tmdb_id:
            return 0
        episodes = tmdb.get_season_episodes(tmdb_id, season)
        return len(episodes)
    except Exception:
        return 0


def _lazy_register_season(req: MediaRequest, season: int) -> tuple[bool, Optional[TorrentioStream]]:
    """Catbox lazy mode for series. Tries a cached season pack first, then falls
    back to per-episode cached registration. Returns (any_written, first_stream)."""
    import debrid
    try:
        pack_candidates = _fetch_season_candidates(req, season, episode=1, prefer_season_pack=True)
    except Exception as exc:
        # Must not propagate: it would bubble through the `for season in
        # req.seasons` loop in _process_locked and mark the WHOLE request
        # failed, discarding any earlier season's already-registered strms.
        log.warning("Lazy fetch failed for %s S%02d: %s", req.title, season, exc)
        return False, None
    pack_candidates = blacklist.filter_candidates(pack_candidates)
    if not pack_candidates:
        log.info("Lazy series: no candidates for %s S%02d  -  marking wanted", req.title, season)
        return False, None

    hashes = [s.info_hash for s in pack_candidates]
    try:
        cached_hashes = debrid.check_cached_multi(hashes).get("torbox", set())
    except Exception as exc:
        log.warning("Lazy cache-check failed for %s S%02d: %s", req.title, season, exc)
        return False, None

    # --- Try season pack first ---
    packs = [s for s in pack_candidates if s.is_season_pack and s.info_hash in cached_hashes]
    if packs:
        pack = packs[0]
        ep_count = _get_season_episode_count(req.imdb_id, season)
        if ep_count == 0:
            ep_count = 24  # safe upper bound when TMDB unavailable
        log.info("Lazy: cached season pack for %s S%02d (%d ep), registering %d episode(s)",
                 req.title, season, ep_count, ep_count)
        written = 0
        preload_done = False
        for ep in range(1, ep_count + 1):
            if strm_generator.create_lazy_episode_strm(
                pack.info_hash, pack.magnet, req.title, season, ep,
                imdb_id=req.imdb_id,
                quality=pack.quality,
                source=pack.source,
                size_gb=pack.size_gb,
                preload_first=not preload_done,
            ):
                written += 1
                preload_done = True
        if written:
            log.info("Lazy season pack: %d .strm(s) registered for %s S%02d", written, req.title, season)
            return True, pack
        log.info("Lazy season pack: all strms already existed for %s S%02d", req.title, season)
        return False, None

    # --- Fall back to per-episode cached registration ---
    log.info("Lazy: no cached season pack for %s S%02d  -  trying per-episode", req.title, season)
    added = 0
    first_winner: Optional[TorrentioStream] = None
    preload_done = False
    episode = 1
    while True:
        if episode == 1:
            ep_candidates = [s for s in pack_candidates if s.info_hash in cached_hashes]
        else:
            try:
                ep_cands_raw = _fetch_season_candidates(req, season, episode=episode)
            except Exception as exc:
                # Same reasoning as the pack-candidates fetch above: stop this
                # season here rather than letting the error abort the whole
                # multi-season request.
                log.warning("Lazy fetch failed for %s S%02dE%02d: %s", req.title, season, episode, exc)
                break
            ep_cands_raw = blacklist.filter_candidates(ep_cands_raw)
            if not ep_cands_raw:
                break
            ep_hashes = [s.info_hash for s in ep_cands_raw]
            try:
                ep_cached = debrid.check_cached_multi(ep_hashes).get("torbox", set())
            except Exception as exc:
                log.warning("Lazy cache-check failed for %s S%02dE%02d: %s", req.title, season, episode, exc)
                break
            ep_candidates = [s for s in ep_cands_raw if s.info_hash in ep_cached]

        if not ep_candidates:
            if episode == 1:
                log.info("Lazy: no cached per-episode for %s S%02dE%02d  -  stopping", req.title, season, episode)
            break

        winner = ep_candidates[0]
        if strm_generator.create_lazy_episode_strm(
            winner.info_hash, winner.magnet, req.title, season, episode,
            imdb_id=req.imdb_id,
            quality=winner.quality,
            source=winner.source,
            size_gb=winner.size_gb,
            preload_first=not preload_done,
        ):
            added += 1
            first_winner = first_winner or winner
            preload_done = True

        episode += 1
        if episode > 50:
            log.warning("Episode cap (50) reached for %s S%02d", req.title, season)
            break

    if added:
        log.info("Lazy per-episode: %d .strm(s) registered for %s S%02d", added, req.title, season)
        return True, first_winner

    reason = "no cached episodes available yet  -  waiting for TorBox cache"
    log.info("Catbox: %s", reason)
    _LAST_FAIL_REASON[req.imdb_id] = reason
    _WANTED[req.imdb_id] = reason
    return False, None


def _process_season(req: MediaRequest, season: int) -> tuple[bool, Optional[TorrentioStream]]:
    if _settings.get("CATBOX_MODE", False) and _settings.get("CATBOX_LAZY_ADD", False):
        ok, winner = _lazy_register_season(req, season)
        if ok:
            return True, winner
        if req.imdb_id in _WANTED:
            return False, None
        # _lazy_register_season set neither ok nor _WANTED → treat as failed
        return False, None

    try:
        pack_candidates = _fetch_season_candidates(req, season, episode=1, prefer_season_pack=True)
    except Exception as exc:
        # Must not propagate: it would bubble through the `for season in
        # req.seasons` loop in _process_locked and mark the WHOLE request
        # failed, discarding any earlier season's already-added torrents.
        log.warning("Fetch failed for %s S%02d pack candidates: %s", req.title, season, exc)
        pack_candidates = []

    if pack_candidates and pack_candidates[0].is_season_pack:
        log.info("Trying season pack(s) for %s S%02d", req.title, season)
        packs = [s for s in pack_candidates if s.is_season_pack]
        try:
            ok, winner = _add_best_from(packs, f"{req.title} S{season:02d} pack")
        except RateLimited:
            log.info("TorBox rate limited  -  trying RD pack fallback for %s S%02d", req.title, season)
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
            try:
                candidates = _fetch_season_candidates(req, season, episode=episode)
            except Exception as exc:
                # Same reasoning as the pack-candidates fetch above: stop
                # this season here (keeping whatever was already added) rather
                # than letting the error abort the whole multi-season request.
                log.warning("Fetch failed for %s S%02dE%02d: %s", req.title, season, episode, exc)
                break
        if not candidates:
            log.info("No more episodes returned at S%02dE%02d", season, episode)
            break
        try:
            ok, winner = _add_best_from(candidates, f"{req.title} S{season:02d}E{episode:02d}")
        except RateLimited:
            # TorBox quota gone mid-season  -  try RD for this episode, then stop
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
    row_id = db.insert_request(req.title, req.imdb_id, req.media_type, req.seasons,
                                tmdb_id=req.tmdb_id)

    # Never accept a release for a movie that isn't out yet: pre-release
    # blockbusters attract fake/junk cached torrents (mislabeled CAMs, scam
    # uploads) that would otherwise get a .strm and show in the library playing
    # garbage. Mark it upcoming and recheck once it has actually released.
    if req.is_movie:
        try:
            import datetime
            import tmdb
            rd = tmdb.release_date(req.imdb_id, getattr(req, "tmdb_id", None), "movie")
            if rd and rd > datetime.date.today().isoformat():
                db.update_request(row_id, "upcoming", error=f"not released yet (releases {rd})")
                log.info("Skip %s: not released until %s  -  marked upcoming", req.title, rd)
                return False
        except Exception as exc:
            log.debug("release-date guard skipped for %s: %s", req.title, exc)

    success = False
    winner: Optional[TorrentioStream] = None
    season_winners: list[TorrentioStream] = []
    try:
        if req.is_movie:
            success, winner = _process_movie(req)
            if winner:
                season_winners.append(winner)
        else:
            for season in req.seasons:
                ok, w = _process_season(req, season)
                if ok:
                    success = True
                    if w:
                        season_winners.append(w)
            winner = season_winners[0] if season_winners else None
    except RateLimited:
        # TorBox 429  -  not a real failure. Reschedule and surface a clear status.
        _LAST_FAIL_REASON.pop(req.imdb_id, None)
        db.update_request(row_id, "rate_limited",
                          error="TorBox rate limit (60/hour) hit  -  will retry automatically")
        import retry_queue
        retry_queue.schedule(req, _retry_attempt)
        log.warning("Rate limited processing %s  -  rescheduled via retry queue", req.title)
        db.record_metric("request_rate_limited", req.media_type, value_int=1)
        return False
    except Exception as exc:
        log.exception("Unexpected error processing %s", req.title)
        err_str = str(exc)
        status = "upcoming" if "403" in err_str else "failed"
        db.update_request(row_id, status, error=err_str)
        import retry_queue
        retry_queue.schedule(req, _retry_attempt)
        return False

    if success:
        _WANTED.pop(req.imdb_id, None)
        db.remove_wanted_movie(req.imdb_id)
        db.update_request(
            row_id, "success",
            quality=winner.quality if winner else None,
            source=winner.source if winner else None,
            info_hash=winner.info_hash if winner else None,
        )
        if not req.is_movie:
            monitor.add_series(req.imdb_id, req.title, req.seasons)
        # Generate .strm for every winning torrent. For a multi-season series each
        # season is its own pack, so all winners must be materialised  -  not just
        # the first  -  otherwise only season 1 ends up in the library. (In catbox/
        # lazy mode the winners aren't in TorBox and the per-season .strm files were
        # already written during registration, so this loop no-ops.)
        for w in season_winners:
            try:
                item = torbox.find_by_hash(w.info_hash) if w else None
                torrent_id = item.get('id') if item else None
                if not torrent_id:
                    continue
                strm_generator.create_strm_for_torrent(torrent_id, req.title, req.media_type,
                                                        imdb_id=req.imdb_id,
                                                        tmdb_id=getattr(req, 'tmdb_id', None))
            except Exception as exc:
                # One winner's lookup/strm-write failing must not skip strm
                # generation for the other already-successful winners, nor
                # the refresh_library/notify/metrics calls after this loop.
                log.warning("Strm generation failed for %s (hash=%s): %s",
                            req.title, getattr(w, "info_hash", None), exc)
                continue
            # Best-effort subtitle fetch (movies only)
            try:
                import subtitles
                from pathlib import Path
                from config import MEDIA_PATH
                if req.is_movie:
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
            db.record_metric("source_win", winner.source, value_int=1)
            try:
                import metrics_prom
                metrics_prom.quality_added_total.labels(quality=winner.quality or "unknown").inc()
                metrics_prom.source_wins_total.labels(source=winner.source).inc()
            except Exception as exc:
                log.debug("metrics_prom (quality) failed: %s", exc)
    elif req.imdb_id in _WANTED:
        reason = _WANTED.pop(req.imdb_id)
        _LAST_FAIL_REASON.pop(req.imdb_id, None)
        db.update_request(row_id, "wanted", error=reason)
        try:
            import tmdb
            tmdb_id = tmdb.find_by_imdb(req.imdb_id, kind="movie")
        except Exception:
            tmdb_id = None
        db.upsert_wanted_movie(req.imdb_id, tmdb_id, req.title, reason)
        log.info("Marked %s as wanted  -  will recheck for an acceptable release", req.title)
        db.log_activity("wanted", req.title, f"{reason} ({req.imdb_id})", False)
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
