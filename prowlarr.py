"""Prowlarr scraper.

Prowlarr aggregates many torrent indexers (12 enabled here: Nyaa, YTS,
EZTV, TorrentGalaxy, Pirate Bay, Knaben, Internet Archive, LimeTorrents,
MixtapeTorrent, Zilean, plus DrunkenSlug/NZBgeek usenet). Unlike MediaFusion
it doesn't need an internal catalog seed: every /api/v1/search call queries
all configured indexers live and returns aggregated results.

Used as a complement to Torrentio + Zilean for:
  - Anime (Nyaa is the best anime tracker; Torrentio's anime coverage is
    weak)
  - Foreign + obscure releases that never made it into DMM
  - Recent torrents that the public DMM caches haven't picked up yet

Disabled unless PROWLARR_ENABLED=true. Failures degrade silently to an
empty list so an unavailable Prowlarr never blocks Torrentio + Zilean.
"""
from __future__ import annotations

import logging
import re

import requests

from config import PROWLARR_API_KEY, PROWLARR_BASE_URL, PROWLARR_ENABLED
from torrentio import (
    TorrentioStream,
    _classify_quality,
    _detect_languages,
    _looks_like_season_pack,
)

log = logging.getLogger(__name__)

_BYTES_PER_GB = 1024 ** 3
_HASH_RE = re.compile(r"btih:([a-fA-F0-9]{40})", re.IGNORECASE)

# Prowlarr Newznab category IDs.
_CAT_MOVIE = 2000
_CAT_TV = 5000


def _info_hash_from_row(row: dict) -> str | None:
    """Extract a 40-char info-hash. Prefer infoHash field; fall back to
    parsing the magnet URI in `guid` or `magnetUrl`."""
    raw = row.get("infoHash")
    if raw and len(raw) == 40:
        return raw.lower()
    for key in ("guid", "magnetUrl"):
        val = row.get(key) or ""
        m = _HASH_RE.search(val)
        if m:
            return m.group(1).lower()
    return None


def _to_stream(row: dict, season: int | None) -> TorrentioStream | None:
    if (row.get("protocol") or "").lower() != "torrent":
        # Skip usenet results: TorBox add_magnet flow only handles magnets.
        return None
    info_hash = _info_hash_from_row(row)
    if not info_hash:
        return None

    title = row.get("title") or row.get("fileName") or ""
    indexer = row.get("indexer") or ""
    seeders = int(row.get("seeders") or 0)
    size_bytes = row.get("size") or 0
    try:
        size_gb = round(int(size_bytes) / _BYTES_PER_GB, 2)
    except (TypeError, ValueError):
        size_gb = 0.0

    # Embed indexer name in the rendered "name" so the dashboard log shows
    # which tracker each candidate came from.
    name = f"{title} [{indexer}]" if indexer else title
    classification_blob = {"name": name, "title": title}

    return TorrentioStream(
        name=name,
        title=title,
        info_hash=info_hash,
        quality=_classify_quality(classification_blob),
        seeders=seeders,
        size_gb=size_gb,
        is_season_pack=_looks_like_season_pack(title, season),
        languages=_detect_languages(name),
        source=f"prowlarr/{indexer.lower()}" if indexer else "prowlarr",
    )


def fetch_streams(
    media_type: str,
    imdb_id: str,
    season: int | None = None,
    episode: int | None = None,
    timeout: int = 30,
) -> list[TorrentioStream]:
    """Return parsed TorrentioStream objects from Prowlarr's aggregated
    indexer search. Returns [] on any failure (disabled, unreachable,
    unauthorised, bad response). Never raises."""
    if not PROWLARR_ENABLED or not PROWLARR_BASE_URL or not PROWLARR_API_KEY:
        return []

    # Prowlarr accepts the special Newznab `{ImdbId:nnnnn}` token for
    # IMDB-keyed searches across indexers that support it. For series we
    # always pass type=tvsearch + categories=5000 so the tv-specific
    # indexers (EZTV, MixtapeTorrent) light up. Movies use type=movie.
    numeric_imdb = imdb_id.lstrip("t")  # drop the "tt" prefix
    query = "{ImdbId:" + numeric_imdb + "}"
    if media_type == "movie":
        search_type = "movie"
        categories = _CAT_MOVIE
    else:
        search_type = "tvsearch"
        categories = _CAT_TV

    params = {
        "query": query,
        "type": search_type,
        "categories": categories,
        "limit": 100,
    }
    url = f"{PROWLARR_BASE_URL.rstrip('/')}/api/v1/search"
    log.info("Querying Prowlarr: %s (type=%s)", url, search_type)
    try:
        resp = requests.get(
            url, params=params, timeout=timeout,
            headers={"X-Api-Key": PROWLARR_API_KEY},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Prowlarr unavailable: %s", exc)
        return []

    rows = resp.json() or []
    if not isinstance(rows, list):
        log.warning("Prowlarr unexpected response shape: %s", type(rows).__name__)
        return []
    parsed = [s for s in (_to_stream(r, season) for r in rows) if s is not None]
    log.info("Prowlarr returned %d raw rows (%d parsed magnets)", len(rows), len(parsed))
    return parsed
