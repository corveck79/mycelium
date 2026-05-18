import logging

import requests

from config import TMDB_API_KEY

log = logging.getLogger(__name__)

_BASE = "https://api.themoviedb.org/3"


def tmdb_to_imdb(tmdb_id: int | str, media_type: str = "movie") -> str | None:
    """Resolve a TMDB ID to an IMDB ID via the TMDB external_ids endpoint.

    media_type should be "movie" or "series"/"tv".
    Returns None if TMDB_API_KEY is unset, the request fails, or no imdb_id is returned.
    """
    if not TMDB_API_KEY:
        log.warning("TMDB_API_KEY not set; cannot resolve tmdbId=%s to IMDB ID", tmdb_id)
        return None

    kind = "movie" if media_type == "movie" else "tv"
    url = f"{_BASE}/{kind}/{tmdb_id}/external_ids"
    headers = {"Authorization": f"Bearer {TMDB_API_KEY}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("TMDB external_ids failed for %s/%s: %s", kind, tmdb_id, exc)
        return None

    imdb_id = (resp.json() or {}).get("imdb_id") or None
    if imdb_id:
        log.info("TMDB resolved %s/%s → %s", kind, tmdb_id, imdb_id)
    else:
        log.warning("TMDB returned no imdb_id for %s/%s", kind, tmdb_id)
    return imdb_id
