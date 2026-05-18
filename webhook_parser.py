"""Parse Overseerr / Jellyseerr webhook payloads into a normalized request shape."""

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_IMDB_RE = re.compile(r"\btt\d{6,10}\b")

_ACTIONABLE_TYPES = {
    "MEDIA_APPROVED",
    "MEDIA_AUTO_APPROVED",
    "MEDIA_PENDING",  # only if auto-approve is off in Seerr but user still wants fulfillment
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
    # Custom JSON payloads may embed it in extras
    for extra in payload.get("extra") or []:
        name = (extra.get("name") or "").lower()
        value = extra.get("value") or ""
        if "imdb" in name and value:
            return str(value).strip()
    # Last resort: search the whole payload as a string
    blob = str(payload)
    m = _IMDB_RE.search(blob)
    return m.group(0) if m else None


def _extract_media_type(payload: dict) -> str:
    media = payload.get("media") or {}
    raw = (media.get("media_type") or payload.get("media_type") or "").lower()
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
    # Deduplicate while preserving order
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
    if not imdb_id:
        raise WebhookError("No IMDB id found in webhook payload")

    title = payload.get("subject") or (payload.get("media") or {}).get("title") or imdb_id

    seasons = _extract_seasons(payload) if media_type == "series" else []
    if media_type == "series" and not seasons:
        log.warning("Series request without season info; defaulting to season 1")
        seasons = [1]

    return MediaRequest(title=title, media_type=media_type, imdb_id=imdb_id, seasons=seasons)
