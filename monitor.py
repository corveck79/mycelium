"""Series monitoring and movie sync — periodic tasks running alongside the webhook."""

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
from config import MEDIA_PATH, MAX_RETRY_ATTEMPTS, ZILEAN_ENABLED

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


def _sync_wanted(imdb_id: str, tmdb_id: int, title: str, seasons: list[int]) -> None:
    today = _TODAY()
    for season in seasons:
        episodes = tmdb.get_season_episodes(tmdb_id, season)
        for ep in episodes:
            ep_num = ep.get("episode_number")
            air_date = ep.get("air_date") or None
            if not ep_num:
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


def run_series_check() -> None:
    """Periodic: refresh episode lists, detect new seasons, retry wanted."""
    log.info("Monitor: starting series check")
    today = _TODAY()

    for series in db.get_monitored_series(status="active"):
        imdb_id = series["imdb_id"]
        title = series["title"]
        tmdb_id = series["tmdb_id"]
        seasons = [int(s) for s in (series["seasons"] or "1").split(",") if s.strip().isdigit()]

        # Resolve TMDB ID if missing
        if not tmdb_id:
            tmdb_id = tmdb.find_by_imdb(imdb_id, kind="tv")
            if tmdb_id:
                db.update_monitored_series(series["id"], tmdb_id=tmdb_id)

        if not tmdb_id:
            log.warning("Monitor: no TMDB ID for %s; skipping", title)
            db.update_monitored_series(series["id"])
            continue

        # Detect new seasons via TMDB
        show = tmdb.get_show_info(tmdb_id)
        if show:
            total = show.get("number_of_seasons") or 0
            new_seasons = [s for s in range(1, total + 1) if s not in seasons]
            if new_seasons:
                log.info("Monitor: new season(s) %s detected for %s", new_seasons, title)
                seasons = sorted(set(seasons) | set(new_seasons))
                db.update_monitored_series(series["id"], seasons=seasons)
                _search_and_add_season(imdb_id, title, new_seasons)

        # Refresh wanted list
        _sync_wanted(imdb_id, tmdb_id, title, seasons)
        db.update_monitored_series(series["id"])

    # Retry wanted episodes
    for ep in db.get_wanted_episodes(max_attempts=MAX_RETRY_ATTEMPTS):
        air_date = ep.get("air_date")
        if air_date and air_date > today:
            db.mark_episode_status(ep["imdb_id"], ep["season"], ep["episode"], "not_aired")
            continue
        if strm_exists_episode(ep["title"], ep["season"], ep["episode"]):
            db.mark_episode_status(ep["imdb_id"], ep["season"], ep["episode"], "found")
            continue
        _retry_episode(ep)

    log.info("Monitor: series check complete")


def _retry_episode(ep: dict) -> None:
    imdb_id, title, season, episode = ep["imdb_id"], ep["title"], ep["season"], ep["episode"]
    log.info("Monitor: searching %s S%02dE%02d (attempt %d)", title, season, episode, ep["attempt_count"] + 1)
    db.increment_episode_attempt(ep["id"])

    streams: list = []
    if ZILEAN_ENABLED:
        streams = zilean.fetch_streams(imdb_id, season=season, episode=episode)
    if not streams:
        streams = torrentio.fetch_streams("series", imdb_id, season=season, episode=episode)

    candidates = torrentio.rank_streams(streams)
    if not candidates:
        log.info("Monitor: no candidates for %s S%02dE%02d", title, season, episode)
        return

    cached_hashes = torbox.check_cached([s.info_hash for s in candidates])
    ordered = [s for s in candidates if s.info_hash in cached_hashes] or candidates[:1]
    for stream in ordered:
        try:
            torbox.add_magnet(stream.magnet)
            torbox.wait_until_ready(stream.info_hash)
            log.info("Monitor: added %s S%02dE%02d", title, season, episode)
            return
        except Exception as exc:
            log.warning("Monitor: failed to add %s S%02dE%02d: %s", title, season, episode, exc)


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
        if ZILEAN_ENABLED:
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
                torbox.add_magnet(stream.magnet)
                torbox.wait_until_ready(stream.info_hash)
                log.info("Monitor: added new season %s S%02d", title, season)
                break
            except Exception as exc:
                log.warning("Monitor: failed adding %s S%02d: %s", title, season, exc)


# ── Movie sync ────────────────────────────────────────────────────────────────

def sync_movies() -> None:
    """Sync approved movie requests from Seerr and check filesystem."""
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
