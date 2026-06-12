"""Series monitoring and movie sync  -  periodic tasks running alongside the webhook."""

import glob
import logging
import os
from datetime import date

import db
import seerr
import tmdb
import torbox
import torrentio
import zilean
import settings as _settings
from config import MEDIA_PATH

log = logging.getLogger(__name__)

_TODAY = lambda: date.today().isoformat()  # noqa: E731


# ── Filesystem helpers ────────────────────────────────────────────────────────

def _series_dir(title: str) -> str | None:
    """Return the series folder that best matches title, or None."""
    base = os.path.join(MEDIA_PATH, "series")
    if not os.path.isdir(base):
        return None
    needle = title[:12].lower()
    for entry in os.listdir(base):
        if needle in entry.lower():
            return os.path.join(base, entry)
    return None


def strm_exists_episode(title: str, season: int, episode: int) -> bool:
    folder = _series_dir(title)
    if not folder:
        return False
    s_ep = f"s{season:02d}e{episode:02d}"
    pattern = os.path.join(folder, f"Season {season}", "*.strm")
    return any(s_ep in os.path.basename(f).lower() for f in glob.glob(pattern))


def strm_exists_movie(title: str) -> bool:
    base = os.path.join(MEDIA_PATH, "movies")
    if not os.path.isdir(base):
        return False
    needle = title[:12].lower()
    for entry in os.listdir(base):
        if needle in entry.lower():
            if glob.glob(os.path.join(base, entry, "*.strm")):
                return True
    return False


# ── Series monitoring ─────────────────────────────────────────────────────────

def add_series(imdb_id: str, title: str, seasons: list[int]) -> None:
    """Call this after a series is processed to start monitoring it."""
    tmdb_id = tmdb.find_by_imdb(imdb_id, kind="tv")
    db.upsert_monitored_series(imdb_id, tmdb_id, title, seasons)
    if tmdb_id:
        _sync_wanted(imdb_id, tmdb_id, title, seasons)
    log.info("Monitor: added series %s (%s), monitoring seasons %s", title, imdb_id, seasons)


def _sync_wanted(imdb_id: str, tmdb_id: int, title: str, seasons: list[int],
                 monitor_mode: str = "all", since: str | None = None) -> None:
    """Refresh the wanted-episode list for the monitored seasons.

    monitor_mode:
      all      -  every episode of the monitored seasons (back-catalog included)
      future   -  only episodes airing on/after `since` (the date the series was
                added); already-aired episodes are not monitored
      selected -  same as all, but `seasons` already contains only the chosen ones
    """
    today = _TODAY()
    cutoff = since or today
    for season in seasons:
        episodes = tmdb.get_season_episodes(tmdb_id, season)
        for ep in episodes:
            ep_num = ep.get("episode_number")
            air_date = ep.get("air_date") or None
            if not ep_num:
                continue
            if monitor_mode == "future":
                # Skip anything that aired before we started monitoring.
                if not air_date or air_date < cutoff:
                    continue
            if air_date and air_date > today:
                db.upsert_wanted_episode(imdb_id, tmdb_id, title, season, ep_num, air_date)
                db.mark_episode_status(imdb_id, season, ep_num, "not_aired")
                continue
            if strm_exists_episode(title, season, ep_num):
                db.upsert_wanted_episode(imdb_id, tmdb_id, title, season, ep_num, air_date)
                db.mark_episode_status(imdb_id, season, ep_num, "found")
            else:
                db.upsert_wanted_episode(imdb_id, tmdb_id, title, season, ep_num, air_date)
                db.mark_episode_status(imdb_id, season, ep_num, "wanted")


def run_series_check() -> None:
    """Periodic: refresh episode lists, detect new seasons, retry wanted."""
    log.info("Monitor: starting series check")
    today = _TODAY()

    for series in db.get_monitored_series(status="active"):
        imdb_id = series["imdb_id"]
        title = series["title"]
        tmdb_id = series["tmdb_id"]
        seasons = [int(s) for s in (series["seasons"] or "1").split(",") if s.strip().isdigit()]
        monitor_mode = series.get("monitor_mode") or "all"
        since = series.get("added_at_date")

        # Resolve TMDB ID if missing
        if not tmdb_id:
            tmdb_id = tmdb.find_by_imdb(imdb_id, kind="tv")
            if tmdb_id:
                db.update_monitored_series(series["id"], tmdb_id=tmdb_id)

        if not tmdb_id:
            log.warning("Monitor: no TMDB ID for %s; skipping", title)
            db.update_monitored_series(series["id"])
            continue

        # Detect new seasons via TMDB. For 'selected' mode we never auto-expand  - 
        # the user picked specific seasons. For 'all' and 'future' we pick up new
        # seasons as they're announced.
        if monitor_mode != "selected":
            show = tmdb.get_show_info(tmdb_id)
            if show:
                total = show.get("number_of_seasons") or 0
                new_seasons = [s for s in range(1, total + 1) if s not in seasons]
                if new_seasons:
                    log.info("Monitor: new season(s) %s detected for %s", new_seasons, title)
                    seasons = sorted(set(seasons) | set(new_seasons))
                    db.update_monitored_series(series["id"], seasons=seasons)

        # Refresh wanted list (mode-aware)
        _sync_wanted(imdb_id, tmdb_id, title, seasons, monitor_mode=monitor_mode, since=since)
        db.update_monitored_series(series["id"])

    # Retry wanted episodes  -  keep watching indefinitely (like Radarr/Sonarr).
    # In catbox mode no TorBox quota is consumed so we never pause for budget.
    import processor
    catbox_mode = _settings.get("CATBOX_MODE", False)
    wanted = db.get_wanted_episodes(max_attempts=10_000)
    for ep in wanted:
        air_date = ep.get("air_date")
        if air_date and air_date > today:
            db.mark_episode_status(ep["imdb_id"], ep["season"], ep["episode"], "not_aired")
            continue
        if strm_exists_episode(ep["title"], ep["season"], ep["episode"]):
            db.mark_episode_status(ep["imdb_id"], ep["season"], ep["episode"], "found")
            continue
        if not catbox_mode:
            usage = torbox.createtorrent_usage()
            if usage["count"] >= torbox._CREATETORRENT_LIMIT_HOUR - 2:
                log.info("Monitor: createtorrent budget low (%d/%d)  -  pausing episode retries",
                         usage["count"], torbox._CREATETORRENT_LIMIT_HOUR)
                break
        try:
            _retry_episode(ep)
        except processor.RateLimited:
            if catbox_mode:
                log.info("Monitor: checkcached rate limited  -  waiting 60s then continuing")
                import time; time.sleep(60)
                continue
            log.info("Monitor: rate limited  -  pausing episode retries until next run")
            break

    log.info("Monitor: series check complete")


def run_series_backfill() -> dict:
    """Import all series from Sonarr, then run a full series check to create .strm files.
    Combines import_sonarr + run_series_check in one shot.
    Returns a summary dict."""
    import arr_import
    summary: dict = {"import": {}, "check": "done"}
    try:
        result = arr_import.import_sonarr(only_monitored=True)
        summary["import"] = {
            "added": result.get("added", 0),
            "skipped": result.get("skipped", 0),
            "errors": result.get("errors", 0),
        }
        log.info("run_series_backfill: sonarr import done  -  %s", summary["import"])
    except Exception as exc:
        log.error("run_series_backfill: sonarr import failed: %s", exc)
        summary["import"] = {"error": str(exc)}
    run_series_check()
    return summary


def _retry_episode(ep: dict) -> bool:
    """Search + add one episode. Returns True if added. Raises processor.RateLimited
    when the TorBox createtorrent budget is gone (so the caller can pause)."""
    import processor
    imdb_id, title, season, episode = ep["imdb_id"], ep["title"], ep["season"], ep["episode"]
    log.info("Monitor: searching %s S%02dE%02d (attempt %d)", title, season, episode, ep["attempt_count"] + 1)
    db.increment_episode_attempt(ep["id"])

    streams: list = []
    seen_hashes: set = set()
    if _settings.get("ZILEAN_ENABLED", False):
        for s in zilean.fetch_streams(imdb_id, season=season, episode=episode):
            if s.info_hash not in seen_hashes:
                seen_hashes.add(s.info_hash)
                streams.append(s)
    for s in torrentio.fetch_streams("series", imdb_id, season=season, episode=episode):
        if s.info_hash not in seen_hashes:
            seen_hashes.add(s.info_hash)
            streams.append(s)

    candidates = torrentio.rank_streams(streams)
    if not candidates:
        log.info("Monitor: no acceptable candidates for %s S%02dE%02d (still wanted)",
                 title, season, episode)
        return False

    # In catbox mode: write a lazy .strm for the best cached release.
    # TorBox add is deferred until first playback  -  no quota consumed here.
    if _settings.get("CATBOX_MODE", False):
        cached_hashes = torbox.check_cached([s.info_hash for s in candidates])
        best = next((s for s in candidates if s.info_hash in cached_hashes), None)
        if not best:
            log.info("Monitor: no cached release for %s S%02dE%02d  -  still wanted",
                     title, season, episode)
            return False
        import strm_generator
        written = strm_generator.create_lazy_episode_strm(
            info_hash=best.info_hash,
            magnet=best.magnet,
            title=title,
            season=season,
            episode=episode,
            imdb_id=imdb_id,
        )
        if written:
            db.mark_episode_status(imdb_id, season, episode, "found")
            log.info("Monitor: lazy strm created for %s S%02dE%02d", title, season, episode)
        return bool(written)

    cached_hashes = torbox.check_cached([s.info_hash for s in candidates])
    ordered = [s for s in candidates if s.info_hash in cached_hashes] or candidates[:1]
    for stream in ordered:
        # Skip createtorrent if already in the TorBox library.
        existing = torbox.find_by_hash(stream.info_hash)
        if existing and torbox._is_ready(existing):
            log.info("Monitor: %s S%02dE%02d already in TorBox library", title, season, episode)
            return True
        try:
            torbox.add_magnet(stream.magnet, reason="series-monitor")
            torbox.wait_until_ready(stream.info_hash)
            log.info("Monitor: added %s S%02dE%02d", title, season, episode)
            return True
        except torbox.RateLimited:
            raise processor.RateLimited()
        except Exception as exc:
            if "429" in str(exc):
                raise processor.RateLimited()
            log.warning("Monitor: failed to add %s S%02dE%02d: %s", title, season, episode, exc)

    # TorBox couldn't serve it  -  try RealDebrid (no createtorrent limit).
    rd_winner = processor._try_realdebrid_fallback(
        title, candidates, media_type="episode", season=season, episode=episode,
    )
    if rd_winner:
        log.info("Monitor: served %s S%02dE%02d via RealDebrid", title, season, episode)
        return True
    return False


def search_episode_now(imdb_id: str, title: str, season: int, episode: int) -> bool:
    """Manual trigger: search a single episode immediately."""
    ep_rows = [e for e in db.get_all_wanted_episodes()
               if e["imdb_id"] == imdb_id and e["season"] == season and e["episode"] == episode]
    if ep_rows:
        _retry_episode(ep_rows[0])
    else:
        fake = {"id": 0, "imdb_id": imdb_id, "title": title,
                "season": season, "episode": episode, "attempt_count": 0}
        _retry_episode(fake)
    return strm_exists_episode(title, season, episode)


def _search_and_add_season(imdb_id: str, title: str, seasons: list[int]) -> None:
    for season in seasons:
        streams: list = []
        if _settings.get("ZILEAN_ENABLED", False):
            streams = zilean.fetch_streams(imdb_id, season=season, episode=1)
        if not streams:
            streams = torrentio.fetch_streams("series", imdb_id, season=season, episode=1)
        candidates = torrentio.rank_streams(streams, prefer_season_pack=True)
        if not candidates:
            continue
        cached_hashes = torbox.check_cached([s.info_hash for s in candidates])
        ordered = [s for s in candidates if s.info_hash in cached_hashes] or candidates[:1]
        for stream in ordered:
            try:
                torbox.add_magnet(stream.magnet, reason="seerr-sync")
                torbox.wait_until_ready(stream.info_hash)
                log.info("Monitor: added new season %s S%02d", title, season)
                break
            except Exception as exc:
                log.warning("Monitor: failed adding %s S%02d: %s", title, season, exc)


# ── Movie sync ────────────────────────────────────────────────────────────────

def sync_movies() -> None:
    """Sync approved movie requests from Seerr and check filesystem.
    No-op when SEERR_URL is empty (SPA-only mode)."""
    if not seerr.is_configured():
        return
    log.info("Monitor: syncing movie requests from Seerr")
    try:
        items = seerr.list_approved_requests(take=100)
    except Exception as exc:
        log.error("Monitor: failed to fetch Seerr requests: %s", exc)
        return

    for item in items:
        media = item.get("media") or {}
        raw_type = (media.get("mediaType") or media.get("media_type") or "").lower()
        if raw_type != "movie":
            continue

        title = media.get("title") or media.get("originalTitle") or ""
        imdb_id = media.get("imdbId") or media.get("imdb_id")
        if not imdb_id:
            tmdb_id_val = media.get("tmdbId")
            if tmdb_id_val:
                imdb_id = tmdb.tmdb_to_imdb(tmdb_id_val, media_type="movie")
        if not imdb_id or not title:
            continue

        requested_by = (item.get("requestedBy") or {}).get("displayName") or None
        requested_at = item.get("createdAt") or None

        db.upsert_media_item(imdb_id, title, "movie",
                              seerr_request_id=item.get("id"),
                              requested_by=requested_by,
                              requested_at=requested_at)

        found = strm_exists_movie(title)
        status = "available" if found else "pending"
        db.update_media_item_status(imdb_id, "movie", status, strm_found=found)

    log.info("Monitor: movie sync complete")


def sync_series() -> None:
    """Sync approved TV requests from Seerr into monitored_series.
    No-op when SEERR_URL is empty (SPA-only mode)."""
    if not seerr.is_configured():
        return
    log.info("Monitor: syncing series requests from Seerr")
    try:
        items = seerr.list_approved_requests(take=100)
    except Exception as exc:
        log.error("Monitor: failed to fetch Seerr requests: %s", exc)
        return

    added = 0
    for item in items:
        media = item.get("media") or {}
        raw_type = (media.get("mediaType") or media.get("media_type") or "").lower()
        if raw_type not in ("tv", "series"):
            continue

        title = media.get("title") or media.get("originalTitle") or media.get("name") or ""
        imdb_id = media.get("imdbId") or media.get("imdb_id")
        if not imdb_id:
            tmdb_id_val = media.get("tmdbId")
            if tmdb_id_val:
                imdb_id = tmdb.tmdb_to_imdb(tmdb_id_val, media_type="tv")
        if not imdb_id or not title:
            continue

        seasons_raw = item.get("seasons") or []
        seasons = sorted({s.get("seasonNumber") for s in seasons_raw if s.get("seasonNumber")})

        add_series(imdb_id, title, seasons)
        added += 1

    log.info("Monitor: series sync complete (%d series)", added)
