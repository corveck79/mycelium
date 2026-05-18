import logging
import re
from dataclasses import dataclass

import requests

from config import ALLOW_4K, QUALITY_PREFERENCE, TORRENTIO_BASE_URL, TORRENTIO_OPTS

log = logging.getLogger(__name__)

_QUALITY_PATTERNS = {
    "2160p": re.compile(r"\b(2160p|4k|uhd)\b", re.IGNORECASE),
    "1080p": re.compile(r"\b1080p\b", re.IGNORECASE),
    "720p": re.compile(r"\b720p\b", re.IGNORECASE),
    "480p": re.compile(r"\b480p\b", re.IGNORECASE),
}

_SEEDERS_RE = re.compile(r"👤\s*(\d+)")
_SIZE_RE = re.compile(r"💾\s*([\d.]+)\s*(GB|MB)", re.IGNORECASE)
_SEASON_PACK_HINTS = ("season", "complete", "s%02d ", "s%02d.")


@dataclass
class TorrentioStream:
    name: str
    title: str
    info_hash: str
    quality: str
    seeders: int
    size_gb: float
    is_season_pack: bool

    @property
    def magnet(self) -> str:
        return f"magnet:?xt=urn:btih:{self.info_hash}"


def _classify_quality(stream: dict) -> str:
    blob = f"{stream.get('name', '')} {stream.get('title', '')}"
    for label, pattern in _QUALITY_PATTERNS.items():
        if pattern.search(blob):
            return label
    return "unknown"


def _parse_seeders(title: str) -> int:
    m = _SEEDERS_RE.search(title or "")
    return int(m.group(1)) if m else 0


def _parse_size_gb(title: str) -> float:
    m = _SIZE_RE.search(title or "")
    if not m:
        return 0.0
    value, unit = float(m.group(1)), m.group(2).upper()
    return value if unit == "GB" else value / 1024.0


def _looks_like_season_pack(title: str, season: int | None) -> bool:
    if season is None:
        return False
    blob = (title or "").lower()
    if "complete" in blob:
        return True
    if "season" in blob:
        return True
    # Match "S01" but not "S01E02"
    if re.search(rf"s0?{season}(?!e\d)", blob, re.IGNORECASE):
        return True
    return False


def _to_stream(raw: dict, season: int | None) -> TorrentioStream | None:
    info_hash = raw.get("infoHash")
    if not info_hash:
        return None
    title = raw.get("title", "") or ""
    return TorrentioStream(
        name=raw.get("name", "") or "",
        title=title,
        info_hash=info_hash.lower(),
        quality=_classify_quality(raw),
        seeders=_parse_seeders(title),
        size_gb=_parse_size_gb(title),
        is_season_pack=_looks_like_season_pack(title, season),
    )


def _build_url(media_type: str, imdb_id: str, season: int | None, episode: int | None) -> str:
    prefix = f"{TORRENTIO_BASE_URL.rstrip('/')}"
    if TORRENTIO_OPTS:
        prefix = f"{prefix}/{TORRENTIO_OPTS.strip('/')}"
    if media_type == "movie":
        return f"{prefix}/stream/movie/{imdb_id}.json"
    # series
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
    url = _build_url(media_type, imdb_id, season, episode)
    log.info("Querying Torrentio: %s", url)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}
    raw_streams = payload.get("streams", []) or []
    parsed = [s for s in (_to_stream(r, season) for r in raw_streams) if s is not None]
    log.info("Torrentio returned %d streams (%d parsed)", len(raw_streams), len(parsed))
    return parsed


def _quality_rank(stream: TorrentioStream) -> int:
    try:
        return QUALITY_PREFERENCE.index(stream.quality)
    except ValueError:
        return len(QUALITY_PREFERENCE) + 1


def pick_best(
    streams: list[TorrentioStream],
    prefer_season_pack: bool = False,
) -> TorrentioStream | None:
    if not streams:
        return None
    candidates = streams if ALLOW_4K else [s for s in streams if s.quality != "2160p"]
    if not candidates:
        log.warning("No non-4K candidates available; falling back to full list")
        candidates = streams

    def sort_key(s: TorrentioStream):
        return (
            0 if prefer_season_pack and s.is_season_pack else 1,
            _quality_rank(s),
            -s.seeders,
            s.size_gb,
        )

    candidates.sort(key=sort_key)
    best = candidates[0]
    log.info(
        "Selected stream: quality=%s seeders=%d size=%.2fGB pack=%s hash=%s",
        best.quality,
        best.seeders,
        best.size_gb,
        best.is_season_pack,
        best.info_hash,
    )
    return best
