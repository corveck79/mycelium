"""Auto-add categories: trending, popular, per-streaming-service top.

For each enabled category, fetch the list from TMDB, filter by min rating / votes,
skip what's already in our request history, then queue the item for processing.

For movies we go straight through processor.process(). For series we add them to
monitored_series so the regular episode monitor will fetch episodes.
"""
import logging

import db
import processor
import settings as _settings
import tmdb
from webhook_parser import MediaRequest

log = logging.getLogger(__name__)


def _passes_filters(item: dict) -> bool:
    min_rating = _settings.get("AUTO_ADD_MIN_RATING")
    min_votes = _settings.get("AUTO_ADD_MIN_VOTES")
    if (item.get("rating") or 0) < min_rating:
        return False
    if (item.get("votes") or 0) < min_votes:
        return False
    return True


def _queue_movie(item: dict, source: str, seen: set[str]) -> bool:
    tmdb_id = item.get("tmdb_id")
    title = item.get("title") or ""
    if not tmdb_id or not title:
        return False
    imdb_id = tmdb.tmdb_to_imdb(tmdb_id, media_type="movie")
    if not imdb_id or imdb_id in seen:
        return False
    log.info("Auto-add (%s): queueing movie %s (%s, rating=%s)",
             source, title, imdb_id, item.get("rating"))
    req = MediaRequest(title=title, media_type="movie", imdb_id=imdb_id, seasons=[])
    try:
        processor.process(req)
        seen.add(imdb_id)
        return True
    except Exception as exc:
        log.warning("Auto-add (%s): processor failed for %s: %s", source, title, exc)
        return False


def _queue_series(item: dict, source: str, seen_series: set[str]) -> bool:
    tmdb_id = item.get("tmdb_id")
    title = item.get("title") or ""
    if not tmdb_id or not title:
        return False
    imdb_id = tmdb.tmdb_to_imdb(tmdb_id, media_type="tv")
    if not imdb_id or imdb_id in seen_series:
        return False
    show = tmdb.get_show_info(tmdb_id) or {}
    n_seasons = show.get("number_of_seasons") or 1
    seasons = list(range(1, n_seasons + 1))
    log.info("Auto-add (%s): monitoring series %s (%s, %d seasons)",
             source, title, imdb_id, n_seasons)
    try:
        db.upsert_monitored_series(imdb_id, tmdb_id, title, seasons)
        seen_series.add(imdb_id)
        return True
    except Exception as exc:
        log.warning("Auto-add (%s): upsert_monitored_series failed for %s: %s", source, title, exc)
        return False


def _run_movie_category(name: str, items: list[dict], limit: int,
                         seen: set[str]) -> int:
    if limit <= 0 or not items:
        return 0
    log.info("Auto-add %s: evaluating %d candidate(s)", name, len(items))
    added = 0
    for item in items[: max(limit * 3, limit)]:  # over-fetch to account for filtering
        if added >= limit:
            break
        if not _passes_filters(item):
            continue
        if _queue_movie(item, name, seen):
            added += 1
    log.info("Auto-add %s: %d new movie(s) queued", name, added)
    return added


def _run_series_category(name: str, items: list[dict], limit: int,
                          seen_series: set[str]) -> int:
    if limit <= 0 or not items:
        return 0
    log.info("Auto-add %s: evaluating %d series candidate(s)", name, len(items))
    added = 0
    for item in items[: max(limit * 3, limit)]:
        if added >= limit:
            break
        if not _passes_filters(item):
            continue
        if _queue_series(item, name, seen_series):
            added += 1
    log.info("Auto-add %s: %d new series queued", name, added)
    return added


def run() -> int:
    """Run all configured auto-add categories. Returns total items processed."""
    total = 0
    seen_movies = {r["imdb_id"] for r in db.get_recent(2000) if r.get("media_type") == "movie"}
    seen_series = {s["imdb_id"] for s in db.get_all_monitored_series()}

    region = _settings.get("AUTO_ADD_REGION")
    trending_precache_count = _settings.get("TRENDING_PRECACHE_COUNT")
    popular_movie_count = _settings.get("POPULAR_MOVIE_COUNT")
    netflix_nl_top_count = _settings.get("NETFLIX_NL_TOP_COUNT")
    prime_nl_top_count = _settings.get("PRIME_NL_TOP_COUNT")
    disney_nl_top_count = _settings.get("DISNEY_NL_TOP_COUNT")
    trending_tv_count = _settings.get("TRENDING_TV_COUNT")
    popular_tv_count = _settings.get("POPULAR_TV_COUNT")

    # Movies
    if trending_precache_count > 0:
        total += _run_movie_category("trending-movie-week",
                                       tmdb.trending("movie", "week"),
                                       trending_precache_count, seen_movies)
    if popular_movie_count > 0:
        total += _run_movie_category("popular-movies",
                                       tmdb.popular("movie", region=region),
                                       popular_movie_count, seen_movies)
    if netflix_nl_top_count > 0:
        total += _run_movie_category("netflix-nl-top",
                                       tmdb.discover_by_provider("movie", tmdb.NL_PROVIDERS["netflix"],
                                                                  region=region),
                                       netflix_nl_top_count, seen_movies)
    if prime_nl_top_count > 0:
        total += _run_movie_category("prime-nl-top",
                                       tmdb.discover_by_provider("movie", tmdb.NL_PROVIDERS["amazon_prime"],
                                                                  region=region),
                                       prime_nl_top_count, seen_movies)
    if disney_nl_top_count > 0:
        total += _run_movie_category("disney-nl-top",
                                       tmdb.discover_by_provider("movie", tmdb.NL_PROVIDERS["disney_plus"],
                                                                  region=region),
                                       disney_nl_top_count, seen_movies)

    # Series
    if trending_tv_count > 0:
        total += _run_series_category("trending-tv-week",
                                        tmdb.trending("tv", "week"),
                                        trending_tv_count, seen_series)
    if popular_tv_count > 0:
        total += _run_series_category("popular-tv",
                                        tmdb.popular("tv"),
                                        popular_tv_count, seen_series)

    log.info("Auto-add: %d total item(s) added across all categories", total)
    return total
