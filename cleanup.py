import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
import jellyfin
import tmdb
import torbox
import torrentio
import zilean
from config import MEDIA_PATH, ZILEAN_ENABLED
from torrentio import TorrentioStream

log = logging.getLogger(__name__)

_YEAR_RE = re.compile(r"\((\d{4})\)$")
_TORRENT_ID_RE = re.compile(r"torrent_id[=:](\d+)", re.IGNORECASE)


def _extract_torrent_id(strm_url: str) -> str | None:
    m = _TORRENT_ID_RE.search(strm_url)
    if m:
        return m.group(1)
    try:
        qs = parse_qs(urlparse(strm_url).query)
        for key in ("torrent_id", "TorrentId", "id"):
            if key in qs:
                return qs[key][0]
    except Exception:
        pass
    return None


def _parse_folder_name(folder: str) -> tuple[str, int | None]:
    """Return (title, year) from a folder like 'Movie Title (2022)'."""
    m = _YEAR_RE.search(folder.strip())
    if m:
        year = int(m.group(1))
        title = folder[: m.start()].strip()
        return title, year
    return folder.strip(), None


def _collect_strm_files() -> list[Path]:
    media = Path(MEDIA_PATH)
    strm_files: list[Path] = []
    for subdir in ("movies", "series"):
        base = media / subdir
        if base.is_dir():
            strm_files.extend(base.rglob("*.strm"))
    return strm_files


def _is_available_in_mylist(torrent_id: str, mylist: list[dict]) -> bool:
    for item in mylist:
        if str(item.get("id") or "") == torrent_id:
            return True
    return False


def _resolve_imdb(title: str, year: int | None, media_type: str) -> str | None:
    if media_type == "movie":
        return tmdb.search_movie(title, year=year)
    return tmdb.search_tv(title)


def _fetch_candidates(imdb_id: str, title: str, media_type: str) -> list:
    if media_type == "movie":
        if ZILEAN_ENABLED:
            streams = zilean.fetch_streams(imdb_id)
            candidates = torrentio.rank_streams(streams)
            if candidates:
                return candidates
        streams = torrentio.fetch_streams("movie", imdb_id)
        return torrentio.rank_streams(streams)
    else:
        if ZILEAN_ENABLED:
            streams = zilean.fetch_streams(imdb_id, season=1, episode=1)
            candidates = torrentio.rank_streams(streams, prefer_season_pack=True)
            if candidates:
                return candidates
        streams = torrentio.fetch_streams("series", imdb_id, season=1, episode=1)
        return torrentio.rank_streams(streams, prefer_season_pack=True)


def _repair_strm(path: Path, run_id: int, mylist: list[dict]) -> str:
    """
    Attempt to repair a single .strm file.
    Returns one of: 'ok' (still valid), 'repaired', 'deleted', 'unfixable'.
    """
    try:
        url = path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        log.warning("Could not read %s: %s", path, exc)
        db.insert_repair_item(run_id, str(path), None, None, None, None,
                              "unfixable", f"unreadable: {exc}")
        return "unfixable"

    torrent_id = _extract_torrent_id(url)

    # Determine media type from path structure
    rel = path.relative_to(MEDIA_PATH) if path.is_relative_to(MEDIA_PATH) else path
    parts = rel.parts
    media_type = "series" if (len(parts) > 0 and parts[0] == "series") else "movie"

    if torrent_id and _is_available_in_mylist(torrent_id, mylist):
        log.debug("strm OK (torrent_id=%s): %s", torrent_id, path.name)
        return "ok"

    # Torrent gone — try to repair
    folder_name = path.parent.name
    title, year = _parse_folder_name(folder_name)
    log.info("Broken strm: %s (torrent_id=%s) — searching replacement for '%s'",
             path.name, torrent_id, title)

    imdb_id = _resolve_imdb(title, year, media_type)
    if not imdb_id:
        log.warning("Could not resolve IMDB ID for '%s'; marking unfixable", title)
        try:
            path.unlink()
        except Exception:
            pass
        db.insert_repair_item(run_id, str(path), title, media_type, torrent_id, None,
                              "unfixable", "IMDB ID not found")
        return "unfixable"

    candidates = _fetch_candidates(imdb_id, title, media_type)
    if not candidates:
        log.warning("No replacement candidates for '%s' (%s); deleting strm", title, imdb_id)
        try:
            path.unlink()
        except Exception:
            pass
        db.insert_repair_item(run_id, str(path), title, media_type, torrent_id, None,
                              "unfixable", "no candidates found")
        return "unfixable"

    cached_hashes = torbox.check_cached([s.info_hash for s in candidates])
    cached = [s for s in candidates if s.info_hash in cached_hashes]
    to_try = cached or candidates[:1]

    winner: TorrentioStream | None = None
    for stream in to_try:
        try:
            torbox.add_magnet(stream.magnet)
            torbox.wait_until_ready(stream.info_hash)
            winner = stream
            break
        except Exception as exc:
            log.warning("Failed to add replacement for '%s' (hash=%s): %s", title, stream.info_hash, exc)

    if winner:
        try:
            path.unlink()
        except Exception:
            pass
        log.info("Repaired '%s': deleted strm, added new torrent %s", title, winner.info_hash)
        db.insert_repair_item(run_id, str(path), title, media_type, torrent_id,
                              winner.info_hash, "repaired", None)
        return "repaired"

    log.warning("All replacement candidates failed for '%s'; marking unfixable", title)
    try:
        path.unlink()
    except Exception:
        pass
    db.insert_repair_item(run_id, str(path), title, media_type, torrent_id, None,
                          "unfixable", "all replacement candidates failed")
    return "unfixable"


def run_cleanup() -> None:
    log.info("Cleanup: starting strm scan in %s", MEDIA_PATH)
    run_id = db.insert_cleanup_run()
    scanned = repaired = deleted = unfixable = 0

    strm_files = _collect_strm_files()
    scanned = len(strm_files)
    log.info("Cleanup: found %d .strm files", scanned)

    if not strm_files:
        db.update_cleanup_run(run_id, 0, 0, 0, 0)
        return

    try:
        mylist = torbox.list_torrents()
    except Exception as exc:
        log.error("Cleanup: could not fetch TorBox mylist: %s — aborting", exc)
        db.update_cleanup_run(run_id, scanned, 0, 0, 0)
        return

    tmc_needed = False
    for path in strm_files:
        result = _repair_strm(path, run_id, mylist)
        if result == "repaired":
            repaired += 1
            tmc_needed = True
        elif result == "deleted":
            deleted += 1
            tmc_needed = True
        elif result == "unfixable":
            unfixable += 1

    db.update_cleanup_run(run_id, scanned, repaired, deleted, unfixable)
    log.info("Cleanup done: scanned=%d repaired=%d deleted=%d unfixable=%d",
             scanned, repaired, deleted, unfixable)

    if tmc_needed:
        jellyfin.refresh_library()
