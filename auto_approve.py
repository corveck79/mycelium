"""Per-genre auto-approve rules: Netflix-style auto-fill.

Rules are stored as one JSON blob per media type in the settings table:
  AUTO_APPROVE_RULES_MOVIE / AUTO_APPROVE_RULES_TV
    {
      "28": {"enabled": true, "year_from": null, "year_to": 2020,
             "auto_request_trending": true, "min_votes": null},
      ...
    }

`enabled` controls whether a manually-submitted request in that genre/year
range bypasses the pending-approval queue (see is_auto_approved()).
`auto_request_trending` controls whether the scheduled run() job proactively
requests popular items in that genre/year range on its own, without anyone
asking - the actual "fill my list automatically" behaviour.
`min_votes` overrides the global AUTO_ADD_MIN_VOTES threshold for this rule
only; leave null to use the global default.
"""
import logging

import db
import processor
import tmdb
from config import AUTO_ADD_MIN_RATING, AUTO_ADD_MIN_VOTES, AUTO_ADD_REGION
from webhook_parser import MediaRequest

log = logging.getLogger(__name__)

AUTO_REQUEST_PER_GENRE_LIMIT = 5
FAVORITE_ACTOR_PER_ACTOR_LIMIT = 3
FAVORITE_ACTOR_RECENCY_YEARS = 1


def _key(media_type: str) -> str:
    return f"AUTO_APPROVE_RULES_{'MOVIE' if media_type == 'movie' else 'TV'}"


def get_rules(media_type: str) -> dict:
    import json
    raw = db.get_setting(_key(media_type))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def set_rules(media_type: str, rules: dict) -> None:
    import json
    db.set_setting(_key(media_type), json.dumps(rules or {}))


def _rule_for_year(rule: dict, year: int | str | None) -> bool:
    if not year:
        return True
    try:
        year = int(str(year)[:4])
    except (ValueError, TypeError):
        return True
    year_from, year_to = rule.get("year_from"), rule.get("year_to")
    if year_from and year < int(year_from):
        return False
    if year_to and year > int(year_to):
        return False
    return True


def is_auto_approved(media_type: str, genre_ids: list[int] | None, year: int | str | None) -> bool:
    """True if any of the item's genres has an enabled auto-approve rule covering its year."""
    if not genre_ids:
        return False
    rules = get_rules(media_type)
    for gid in genre_ids:
        rule = rules.get(str(gid))
        if rule and rule.get("enabled") and _rule_for_year(rule, year):
            return True
    return False


def _passes_filters(item: dict, min_votes: int | None = None) -> bool:
    if (item.get("rating") or 0) < AUTO_ADD_MIN_RATING:
        return False
    threshold = min_votes if min_votes is not None else AUTO_ADD_MIN_VOTES
    if (item.get("votes") or 0) < threshold:
        return False
    return True


def _queue_movie(item: dict, seen: set[str]) -> bool:
    tmdb_id, title = item.get("tmdb_id"), item.get("title") or ""
    if not tmdb_id or not title:
        return False
    imdb_id = tmdb.tmdb_to_imdb(tmdb_id, media_type="movie")
    if not imdb_id or imdb_id in seen:
        return False
    log.info("Auto-approve: queueing movie %s (%s)", title, imdb_id)
    try:
        processor.process(MediaRequest(title=title, media_type="movie", imdb_id=imdb_id,
                                        seasons=[], tmdb_id=tmdb_id))
        seen.add(imdb_id)
        return True
    except Exception as exc:
        log.warning("Auto-approve: processor failed for %s: %s", title, exc)
        return False


def _queue_series(item: dict, seen: set[str]) -> bool:
    tmdb_id, title = item.get("tmdb_id"), item.get("title") or ""
    if not tmdb_id or not title:
        return False
    imdb_id = tmdb.tmdb_to_imdb(tmdb_id, media_type="tv")
    if not imdb_id or imdb_id in seen:
        return False
    show = tmdb.get_show_info(tmdb_id) or {}
    seasons = list(range(1, (show.get("number_of_seasons") or 1) + 1))
    log.info("Auto-approve: monitoring series %s (%s, %d seasons)", title, imdb_id, len(seasons))
    try:
        db.upsert_monitored_series(imdb_id, tmdb_id, title, seasons)
        seen.add(imdb_id)
        return True
    except Exception as exc:
        log.warning("Auto-approve: upsert_monitored_series failed for %s: %s", title, exc)
        return False


def _has_blacklisted_person(media_type: str, tmdb_id: int | None, person_bl: set[int]) -> bool:
    """True if any cast member of this item is on the actor blacklist."""
    if not person_bl or not tmdb_id:
        return False
    try:
        cast_ids = tmdb.credits_person_ids(media_type, tmdb_id)
    except Exception:
        return False
    return any(pid in person_bl for pid in cast_ids)


def _is_recent_or_upcoming(item: dict) -> bool:
    """True if a filmography credit looks like new/upcoming work rather than an
    actor's back catalog - favoriting someone shouldn't queue their whole career."""
    year = item.get("year")
    if not year:
        return True
    try:
        import datetime
        return int(str(year)[:4]) >= datetime.date.today().year - FAVORITE_ACTOR_RECENCY_YEARS
    except (ValueError, TypeError):
        return True


def _run_favorite_actors(seen_movies: set[str], seen_series: set[str],
                          movie_bl: set[int], tv_bl: set[int]) -> int:
    """For every favorited actor, queue their recent/upcoming movies and shows
    that aren't already in the library."""
    total = 0
    for actor in db.get_favorite_actors():
        person_id, name = actor.get("tmdb_id"), actor.get("name") or ""
        try:
            detail = tmdb.person_details(person_id)
        except Exception as exc:
            log.warning("Auto-approve: person_details failed for %s: %s", name, exc)
            continue
        if not detail:
            continue
        added = 0
        for item in detail.get("filmography") or []:
            if added >= FAVORITE_ACTOR_PER_ACTOR_LIMIT:
                break
            media_type = item.get("media_type")
            if media_type not in ("movie", "tv"):
                continue
            media_bl = movie_bl if media_type == "movie" else tv_bl
            if item.get("tmdb_id") in media_bl or not _is_recent_or_upcoming(item):
                continue
            queue_fn = _queue_movie if media_type == "movie" else _queue_series
            seen = seen_movies if media_type == "movie" else seen_series
            if queue_fn(item, seen):
                added += 1
        if added:
            log.info("Auto-approve favorite actor %s: %d new item(s) queued", name, added)
        total += added
    return total


def run() -> int:
    """Scheduled job: for every genre with auto_request_trending enabled,
    fetch the most popular items in that genre/year window and queue the
    ones we don't have yet - this is what auto-fills the library. Also
    queues recent/upcoming work for any favorited actor."""
    total = 0
    seen_movies = {r["imdb_id"] for r in db.get_recent(2000) if r.get("media_type") == "movie"}
    seen_series = {s["imdb_id"] for s in db.get_all_monitored_series()}
    movie_bl = db.get_content_blacklist_ids("movie")
    tv_bl = db.get_content_blacklist_ids("tv")
    person_bl = db.get_content_blacklist_ids("person")

    for media_type, queue_fn, seen, media_bl in (("movie", _queue_movie, seen_movies, movie_bl),
                                                  ("tv", _queue_series, seen_series, tv_bl)):
        rules = get_rules(media_type)
        for genre_id_str, rule in rules.items():
            if not rule.get("enabled") or not rule.get("auto_request_trending"):
                continue
            genre_id = int(genre_id_str)
            items = tmdb.discover_by_genre(media_type, genre_id, rule.get("year_from"),
                                            rule.get("year_to"), region=AUTO_ADD_REGION)
            added = 0
            for item in items:
                if added >= AUTO_REQUEST_PER_GENRE_LIMIT:
                    break
                if item.get("tmdb_id") in media_bl:
                    continue
                if not _passes_filters(item, rule.get("min_votes")):
                    continue
                if _has_blacklisted_person(media_type, item.get("tmdb_id"), person_bl):
                    continue
                if queue_fn(item, seen):
                    added += 1
            if added:
                log.info("Auto-approve genre=%s/%s: %d new item(s) queued", media_type, genre_id, added)
            total += added

    total += _run_favorite_actors(seen_movies, seen_series, movie_bl, tv_bl)

    log.info("Auto-approve: %d total item(s) added across all genre rules", total)
    return total
