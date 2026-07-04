"""Auto-approve: per-genre year-ranged auto-fill + favorite-actor auto-request.

Genre rules are stored as JSON in the AUTO_APPROVE_GENRE_RULES setting (a list,
not a scalar env var, so it lives in the settings overlay rather than config.py):
[{"media_type": "movie", "genre_id": 28, "genre_name": "Action",
  "year_from": 2015, "year_to": 2024, "enabled": true}, ...]

Favorite actors are per-user (favorite_actors table); this module fills across
all users' follows into one shared daily budget, since it's downloading into
the same shared library regardless of who followed the actor.
"""
import logging

import db
import processor
import settings as _settings
import tmdb
from config import AUTO_APPROVE_ACTOR_DAILY_LIMIT, AUTO_APPROVE_DAILY_LIMIT

log = logging.getLogger(__name__)

# TMDB genre ids for talk show / news / reality  -  following an actor shouldn't
# queue every talk-show appearance or reality-TV guest spot they've ever had.
_EXCLUDED_ACTOR_GENRES = {10767, 10763, 10764}


def _genre_rules() -> list[dict]:
    import json
    raw = _settings.get("AUTO_APPROVE_GENRE_RULES", "[]")
    try:
        return json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        log.warning("AUTO_APPROVE_GENRE_RULES is not valid JSON; ignoring")
        return []


def set_genre_rules(rules: list[dict]) -> None:
    import json
    _settings.set("AUTO_APPROVE_GENRE_RULES", json.dumps(rules))


def _queue(item: dict, source: str, seen: set[str]) -> bool:
    tmdb_id = item.get("tmdb_id")
    title = item.get("title") or ""
    if not tmdb_id or not title:
        return False
    media_type = "movie" if item.get("media_type") == "movie" else "series"
    imdb_id = tmdb.tmdb_to_imdb(tmdb_id, media_type=media_type)
    if not imdb_id or imdb_id in seen:
        return False
    from webhook_parser import MediaRequest
    req = MediaRequest(title=title, media_type=media_type, imdb_id=imdb_id,
                        seasons=[] if media_type == "movie" else [1], tmdb_id=tmdb_id)
    try:
        processor.process(req)
        seen.add(imdb_id)
        log.info("Auto-approve (%s): queued %s (%s)", source, title, imdb_id)
        return True
    except Exception as exc:
        log.warning("Auto-approve (%s): failed to queue %s: %s", source, title, exc)
        return False


def _seen_imdb_ids() -> set[str]:
    seen_movies = {r["imdb_id"] for r in db.get_recent(2000) if r.get("media_type") == "movie" and r.get("imdb_id")}
    seen_series = {s["imdb_id"] for s in db.get_all_monitored_series() if s.get("imdb_id")}
    return seen_movies | seen_series


def run_genre_fill() -> int:
    """Fill each enabled genre rule (bounded by its year range) into a shared
    daily budget. Returns the number of new items queued."""
    rules = [r for r in _genre_rules() if r.get("enabled")]
    if not rules:
        return 0
    cap = _settings.get("AUTO_APPROVE_DAILY_LIMIT", AUTO_APPROVE_DAILY_LIMIT)
    seen = _seen_imdb_ids()

    added = 0
    for rule in rules:
        if added >= cap:
            break
        media_type = rule.get("media_type", "movie")
        genre_id = rule.get("genre_id")
        if not genre_id:
            continue
        items = tmdb.discover_by_genre(media_type, genre_id,
                                        year_from=rule.get("year_from"), year_to=rule.get("year_to"))
        source = f"genre:{rule.get('genre_name') or genre_id}"
        for item in items:
            if added >= cap:
                break
            if _queue(item, source, seen):
                added += 1
    return added


def run_favorite_actor_fill() -> int:
    """For every followed actor (across all users), auto-request new titles from
    their filmography, skipping talk shows/news/reality. Returns queued count."""
    cap = _settings.get("AUTO_APPROVE_ACTOR_DAILY_LIMIT", AUTO_APPROVE_ACTOR_DAILY_LIMIT)
    actors = db.get_all_favorite_actors()
    if not actors:
        return 0
    seen = _seen_imdb_ids()

    added = 0
    for actor in actors:
        if added >= cap:
            break
        person = tmdb.person_details(actor["person_id"])
        if not person:
            continue
        for item in person["filmography"]:
            if added >= cap:
                break
            if set(item.get("genre_ids") or []) & _EXCLUDED_ACTOR_GENRES:
                continue
            if _queue(item, f"actor:{person['name']}", seen):
                added += 1
    return added


def run() -> int:
    """Run both auto-approve passes. Returns total items queued."""
    return run_genre_fill() + run_favorite_actor_fill()
