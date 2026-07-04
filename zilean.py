import logging

import requests

import settings as _settings
from config import ZILEAN_URL as _ZILEAN_URL_DEFAULT
from torrentio import TorrentioStream, _looks_like_season_pack

log = logging.getLogger(__name__)

_BYTES_PER_GB = 1024 ** 3

# Maps Zilean quality field to the token that rank_streams() regex filters recognise.
_QUALITY_TOKEN_MAP = {
    "WEB-DL": "WEB-DL",
    "WEB": "WEBRip",
    "BluRay": "BluRay",
    "BluRay REMUX": "BluRay Remux",
    "BRRip": "BRRip",
    "DVDRip": "DVDRip",
    "HDTV": "HDTV",
    "CAM": "CAM",
    "TS": "TS",
}


def _to_stream(raw: dict, season: int | None) -> TorrentioStream | None:
    info_hash = raw.get("info_hash") or ""
    if not info_hash:
        return None

    raw_title = raw.get("raw_title", "") or ""
    resolution = (raw.get("resolution") or "unknown").lower()
    # Normalise to the labels used in QUALITY_PREFERENCE / _QUALITY_PATTERNS.
    quality = resolution if resolution in ("2160p", "1080p", "720p", "480p") else "unknown"

    zilean_quality = raw.get("quality") or ""
    # Embed the quality token in name so WEBDL_RE / REMUX_RE / CAM_RE etc. fire correctly.
    source_token = _QUALITY_TOKEN_MAP.get(zilean_quality, zilean_quality)
    name = f"{raw_title} {source_token}".strip()

    size_str = raw.get("size") or "0"
    try:
        size_gb = round(int(size_str) / _BYTES_PER_GB, 2)
    except (ValueError, TypeError):
        size_gb = 0.0

    return TorrentioStream(
        name=name,
        title=raw_title,
        info_hash=info_hash.lower(),
        quality=quality,
        seeders=0,
        size_gb=size_gb,
        is_season_pack=_looks_like_season_pack(raw_title, season),
        source="zilean",
    )


def _from_native(raw: dict, season: int | None) -> TorrentioStream:
    raw_title = raw.get("raw_title", "") or ""
    return TorrentioStream(
        name=raw_title,
        title=raw_title,
        info_hash=(raw.get("info_hash") or "").lower(),
        quality="unknown",
        seeders=0,
        size_gb=round((raw.get("size_bytes") or 0) / _BYTES_PER_GB, 2),
        is_season_pack=_looks_like_season_pack(raw_title, season),
        source="zilean",
    )


def fetch_streams(
    imdb_id: str,
    season: int | None = None,
    episode: int | None = None,
    timeout: int = 10,
) -> list[TorrentioStream]:
    mode = _settings.get("ZILEAN_MODE", "external")
    if mode == "native":
        return _fetch_streams_native(imdb_id, season, episode)
    return _fetch_streams_external(imdb_id, season, episode, timeout)


def _fetch_streams_native(imdb_id: str, season: int | None, episode: int | None) -> list[TorrentioStream]:
    import tmdb
    import zilean_index
    title = tmdb.display_title(imdb_id, media_type="tv" if season is not None else "movie")
    if not title:
        log.warning("Zilean (native): could not resolve title for %s, skipping", imdb_id)
        return []
    raw_list = zilean_index.search(title, season=season, episode=episode)
    parsed = [_from_native(r, season) for r in raw_list]
    log.info("Zilean (native) returned %d results for %r", len(parsed), title)
    return parsed


def _fetch_streams_external(
    imdb_id: str,
    season: int | None,
    episode: int | None,
    timeout: int,
) -> list[TorrentioStream]:
    params: dict[str, object] = {"ImdbId": imdb_id}
    if season is not None:
        params["Season"] = season
    if episode is not None:
        params["Episode"] = episode
    zilean_url = _settings.get("ZILEAN_URL", _ZILEAN_URL_DEFAULT)
    url = f"{zilean_url.rstrip('/')}/dmm/filtered"
    log.info("Querying Zilean: %s params=%s", url, params)
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Zilean unavailable: %s", exc)
        return []
    raw_list = resp.json() or []
    parsed = [s for s in (_to_stream(r, season) for r in raw_list) if s is not None]
    log.info("Zilean returned %d results (%d parsed)", len(raw_list), len(parsed))
    return parsed
