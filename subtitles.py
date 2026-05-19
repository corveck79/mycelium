"""OpenSubtitles .srt fetch for newly created .strm files.

Best-effort: needs OPENSUBTITLES_API_KEY in .env. Free tier allows 5
downloads/day per API key, paid tier higher. Skips silently if not configured.

API docs: https://opensubtitles.stoplight.io/docs/opensubtitles-api/
"""
import logging
from pathlib import Path

import requests

from config import (
    OPENSUBTITLES_API_KEY,
    OPENSUBTITLES_LANGUAGES,
    OPENSUBTITLES_USER_AGENT,
)

log = logging.getLogger(__name__)
_BASE = "https://api.opensubtitles.com/api/v1"


def _headers() -> dict:
    return {
        "Api-Key": OPENSUBTITLES_API_KEY,
        "User-Agent": OPENSUBTITLES_USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _search(imdb_id: str, season: int | None, episode: int | None,
            language: str) -> list[dict]:
    params = {"languages": language}
    # OS expects numeric imdb id (no "tt" prefix)
    params["imdb_id"] = imdb_id.lstrip("t")
    if season is not None:
        params["season_number"] = season
    if episode is not None:
        params["episode_number"] = episode
    try:
        r = requests.get(f"{_BASE}/subtitles", headers=_headers(), params=params, timeout=10)
        r.raise_for_status()
        return (r.json() or {}).get("data") or []
    except Exception as exc:
        log.debug("OpenSubtitles search failed: %s", exc)
        return []


def _request_download_url(file_id: int) -> str | None:
    try:
        r = requests.post(
            f"{_BASE}/download",
            headers=_headers(),
            json={"file_id": file_id},
            timeout=10,
        )
        r.raise_for_status()
        return (r.json() or {}).get("link")
    except Exception as exc:
        log.warning("OpenSubtitles download request failed (file_id=%s): %s", file_id, exc)
        return None


def fetch_for(strm_path: Path, imdb_id: str, media_type: str,
              season: int | None = None, episode: int | None = None) -> int:
    """Download configured-language subtitles next to a .strm file.
    Returns count of files written."""
    if not OPENSUBTITLES_API_KEY or not OPENSUBTITLES_LANGUAGES:
        return 0
    written = 0
    for lang in OPENSUBTITLES_LANGUAGES:
        target = strm_path.with_suffix(f".{lang}.srt")
        if target.exists():
            continue
        results = _search(imdb_id, season, episode, lang)
        if not results:
            continue
        # Pick most-downloaded file from top result
        top = results[0]
        files = (top.get("attributes") or {}).get("files") or []
        if not files:
            continue
        file_id = files[0].get("file_id")
        if not file_id:
            continue
        url = _request_download_url(file_id)
        if not url:
            continue
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            target.write_bytes(r.content)
            log.info("Subtitle saved: %s (%d bytes)", target.name, len(r.content))
            written += 1
        except Exception as exc:
            log.warning("Subtitle download failed: %s", exc)
    return written
