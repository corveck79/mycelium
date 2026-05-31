"""MediaFusion scraper.

MediaFusion (https://github.com/mhdzumair/MediaFusion) is a Stremio addon
aggregator that scrapes from configured Prowlarr indexers + Torrentio +
Zilean and surfaces results in the same /stream/<type>/<imdb>.json shape
Torrentio uses. Running it alongside Torrentio + Zilean gives Mycelium
direct access to whatever Prowlarr is configured with (Nyaa for anime,
YTS for movies, EZTV/TorrentGalaxy/Pirate Bay/etc.) without writing a
native Prowlarr scraper.

Disabled unless MEDIAFUSION_ENABLED=true. Failures degrade silently to an
empty list so an unavailable MediaFusion never blocks Torrentio + Zilean.
"""
from __future__ import annotations

import logging

import requests

from config import MEDIAFUSION_BASE_URL, MEDIAFUSION_ENABLED
from torrentio import (
    TorrentioStream,
    _HTTP_HEADERS,
    _classify_quality,
    _detect_languages,
    _looks_like_season_pack,
    _parse_seeders,
    _parse_size_gb,
)

log = logging.getLogger(__name__)


def _to_stream(raw: dict, season: int | None) -> TorrentioStream | None:
    info_hash = raw.get("infoHash")
    if not info_hash:
        return None
    # MediaFusion puts the human-readable metadata blob in `description` while
    # Torrentio puts it in `title`. The blob carries 💾 size + 👤 seeders +
    # language markers, all of which Torrentio's parsing helpers expect.
    description = raw.get("description") or ""
    name = raw.get("name") or ""
    binge_group = (raw.get("behaviorHints") or {}).get("bingeGroup") or ""
    binge_tokens = binge_group.replace("|", " ").replace("-", " ")
    combined_name = f"{name} {binge_tokens}".strip()
    classification_blob = {"name": combined_name, "title": description}

    return TorrentioStream(
        name=combined_name,
        title=description,
        info_hash=info_hash.lower(),
        quality=_classify_quality(classification_blob),
        seeders=_parse_seeders(description),
        size_gb=_parse_size_gb(description),
        is_season_pack=_looks_like_season_pack(description, season),
        languages=_detect_languages(f"{name} {description}"),
        source="mediafusion",
    )


def _build_url(media_type: str, imdb_id: str,
               season: int | None, episode: int | None) -> str:
    prefix = MEDIAFUSION_BASE_URL.rstrip("/")
    if media_type == "movie":
        return f"{prefix}/stream/movie/{imdb_id}.json"
    if season is None or episode is None:
        raise ValueError("season and episode are required for series")
    return f"{prefix}/stream/series/{imdb_id}:{season}:{episode}.json"


def fetch_streams(
    media_type: str,
    imdb_id: str,
    season: int | None = None,
    episode: int | None = None,
    timeout: int = 30,
) -> list[TorrentioStream]:
    """Return parsed TorrentioStream objects from MediaFusion, or [] if
    disabled / unreachable / errored. Never raises."""
    if not MEDIAFUSION_ENABLED or not MEDIAFUSION_BASE_URL:
        return []
    try:
        url = _build_url(media_type, imdb_id, season, episode)
    except ValueError as exc:
        log.warning("MediaFusion URL build failed: %s", exc)
        return []
    log.info("Querying MediaFusion: %s", url)
    try:
        resp = requests.get(url, timeout=timeout, headers=_HTTP_HEADERS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("MediaFusion unavailable: %s", exc)
        return []
    payload = resp.json() or {}
    raw_streams = payload.get("streams", []) or []
    parsed = [s for s in (_to_stream(r, season) for r in raw_streams) if s is not None]
    log.info("MediaFusion returned %d streams (%d parsed)", len(raw_streams), len(parsed))
    return parsed
