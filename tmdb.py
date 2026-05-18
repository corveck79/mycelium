import logging

import requests as req_lib

from config import TMDB_API_KEY

log = logging.getLogger(__name__)

_BASE = "https://api.themoviedb.org/3"


def _headers() -> dict:
    return {"Authorization": f"Bearer {TMDB_API_KEY}", "Accept": "application/json"}


def _get(path: str, params: dict | None = None, timeout: int = 10) -> dict | None:
    if not TMDB_API_KEY:
        log.warning("TMDB_API_KEY not set; skipping %s", path)
        return None
    try:
        resp = req_lib.get(f"{_BASE}{path}", headers=_headers(), params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json() or {}
    except req_lib.RequestException as exc:
        log.warning("TMDB request failed for %s: %s", path, exc)
        return None


def tmdb_to_imdb(tmdb_id: int | str, media_type: str = "movie") -> str | None:
    kind = "movie" if media_type == "movie" else "tv"
    data = _get(f"/{kind}/{tmdb_id}/external_ids")
    if not data:
        return None
    imdb_id = data.get("imdb_id") or None
    if imdb_id:
        log.info("TMDB resolved %s/%s → %s", kind, tmdb_id, imdb_id)
    else:
        log.warning("TMDB returned no imdb_id for %s/%s", kind, tmdb_id)
    return imdb_id


def find_by_imdb(imdb_id: str, kind: str = "tv") -> int | None:
    """Reverse-lookup: IMDB ID → TMDB ID using the /find endpoint."""
    data = _get(f"/find/{imdb_id}", params={"external_source": "imdb_id"})
    if not data:
        return None
    results = data.get("tv_results" if kind == "tv" else "movie_results") or []
    if results:
        tmdb_id = results[0].get("id")
        log.info("TMDB find %s → tmdb_id=%s", imdb_id, tmdb_id)
        return tmdb_id
    return None


def get_show_info(tmdb_id: int) -> dict | None:
    """Return top-level show info including number_of_seasons."""
    return _get(f"/tv/{tmdb_id}")


def get_season_episodes(tmdb_id: int, season: int) -> list[dict]:
    """Return episode list for a season; each dict has episode_number and air_date."""
    data = _get(f"/tv/{tmdb_id}/season/{season}")
    if not data:
        return []
    return data.get("episodes") or []


def search_movie(title: str, year: int | None = None) -> str | None:
    """Search TMDB for a movie by title; return IMDB ID or None."""
    params: dict = {"query": title}
    if year:
        params["year"] = year
    data = _get("/search/movie", params=params)
    if not data:
        return None
    results = data.get("results") or []
    if not results:
        log.warning("TMDB search_movie: no results for %r (year=%s)", title, year)
        return None
    tmdb_id = results[0]["id"]
    return tmdb_to_imdb(tmdb_id, media_type="movie")


def search_tv(title: str) -> str | None:
    """Search TMDB for a TV show by title; return IMDB ID or None."""
    data = _get("/search/tv", params={"query": title})
    if not data:
        return None
    results = data.get("results") or []
    if not results:
        log.warning("TMDB search_tv: no results for %r", title)
        return None
    tmdb_id = results[0]["id"]
    return tmdb_to_imdb(tmdb_id, media_type="tv")
