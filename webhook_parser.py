"""Parse Overseerr / Jellyseerr webhook payloads into a normalized request shape."""

import logging
import re
from dataclasses import dataclass, field

import seerr
import tmdb

log = logging.getLogger(__name__)

_IMDB_RE = re.compile(r"\btt\d{6,10}\b")

_ACTIONABLE_TYPES = {
    "MEDIA_APPROVED",
    "MEDIA_AUTO_APPROVED",
    "MEDIA_PENDING",
}


@dataclass
class MediaRequest:
    title: str
    media_type: str  # "movie" or "series"
    imdb_id: str
    seasons: list[int] = field(default_factory=list)
    episode: int | None = None

    @property
    def is_movie(self) -> bool:
        return self.media_type == "movie"


class WebhookError(ValueError):
    pass


class IgnoreEvent(Exception):
    """Raised when the webhook event is informational and should not be acted on."""


def _extract_imdb(payload: dict) -> str | None:
    media = payload.get("media") or {}
    for key in ("imdbId", "imdb_id", "imdb"):
        val = media.get(key) or payload.get(key)
        if val:
            return str(val).strip()
    for extra in payload.get("extra") or []:
        name = (extra.get("name") or "").lower()
        value = extra.get("value") or ""
        if "imdb" in name and value:
            return str(value).strip()
    blob = str(payload)
    m = _IMDB_RE.search(blob)
    return m.group(0) if m else None


def _extract_request_id(payload: dict) -> str | None:
    req = payload.get("request") or {}
    rid = req.get("request_id") or req.get("id") or payload.get("request_id")
    return str(rid) if rid else None


def _fetch_from_seerr(payload: dict) -> tuple[str | None, list[int]]:
    """Return (imdb_id, seasons) from the Seerr API using the request_id in the payload."""
    request_id = _extract_request_id(payload)
    if not request_id:
        return None, []
    try:
        data = seerr.get_request(request_id)
    except Exception as exc:
        log.warning("Seerr API lookup failed for request %s: %s", request_id, exc)
        return None, []

    media = data.get("media") or {}
    imdb_id: str | None = media.get("imdbId") or media.get("imdb_id") or None
    if imdb_id:
        imdb_id = str(imdb_id).strip()
        log.info("Got IMDB ID from Seerr API: %s (request=%s)", imdb_id, request_id)
    else:
        tmdb_id = media.get("tmdbId") or media.get("tmdb_id")
        if tmdb_id:
            raw_type = (media.get("mediaType") or media.get("media_type") or "movie").lower()
            media_type_for_tmdb = "movie" if raw_type == "movie" else "tv"
            imdb_id = tmdb.tmdb_to_imdb(tmdb_id, media_type=media_type_for_tmdb)

    seasons: list[int] = []
    for s in data.get("seasons") or []:
        num = s.get("seasonNumber")
        if isinstance(num, int) and num > 0:
            seasons.append(num)

    return imdb_id or None, seasons


def _extract_media_type(payload: dict) -> str:
    media = payload.get("media") or {}
    raw = (media.get("media_type") or media.get("mediaType") or payload.get("media_type") or "").lower()
    if raw in ("movie", "film"):
        return "movie"
    if raw in ("tv", "series", "show"):
        return "series"
    raise WebhookError(f"Unsupported media_type: {raw!r}")


def _extract_seasons(payload: dict) -> list[int]:
    seasons: list[int] = []
    for extra in payload.get("extra") or []:
        name = (extra.get("name") or "").lower()
        if "season" not in name:
            continue
        value = str(extra.get("value") or "")
        for token in re.split(r"[,\s]+", value):
            if token.isdigit():
                seasons.append(int(token))
    seen: set[int] = set()
    out: list[int] = []
    for s in seasons:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def parse(payload: dict) -> MediaRequest:
    if not isinstance(payload, dict):
        raise WebhookError("Webhook body must be a JSON object")

    nt = payload.get("notification_type")
    if nt == "TEST_NOTIFICATION":
        raise IgnoreEvent("test notification")
    if nt and nt not in _ACTIONABLE_TYPES:
        raise IgnoreEvent(f"non-actionable notification_type={nt}")

    media_type = _extract_media_type(payload)

    imdb_id = _extract_imdb(payload)
    seerr_seasons: list[int] = []
    if not imdb_id:
        log.info("IMDB ID not in payload; querying Seerr API")
        imdb_id, seerr_seasons = _fetch_from_seerr(payload)

    if not imdb_id:
        raise WebhookError("No IMDB id found in webhook payload or Seerr API")

    title = payload.get("subject") or (payload.get("media") or {}).get("title") or imdb_id

    if media_type == "series":
        seasons = _extract_seasons(payload) or seerr_seasons
        if not seasons:
            log.warning("Series request without season info; defaulting to season 1")
            seasons = [1]
    else:
        seasons = []

    return MediaRequest(title=title, media_type=media_type, imdb_id=imdb_id, seasons=seasons)
