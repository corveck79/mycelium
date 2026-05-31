"""Generate Kodi/Jellyfin-compatible NFO sidecar files alongside .strm files.

Movies: movies/Title (Year)/Title (Year).nfo
Series: series/Title/tvshow.nfo

Jellyfin reads these to get the exact IMDb ID, so it can fetch metadata and
posters without guessing from the folder name.  When multiple folders exist
for the same series (different torrent sources), writing the same IMDb ID in
all tvshow.nfo files allows Jellyfin to merge them into a single library entry.
"""
import logging
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as _stdlib_xml_escape

import requests as _requests

import db
import tmdb
from config import MEDIA_PATH
from io_utils import atomic_write_text

_IMAGE_BASE_POSTER = "https://image.tmdb.org/t/p/w500"
_IMAGE_BASE_BACKDROP = "https://image.tmdb.org/t/p/w1280"
_IMAGE_BASE_STILL = "https://image.tmdb.org/t/p/w300"
_EP_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")

log = logging.getLogger(__name__)

_YEAR_RE = re.compile(r"\((\d{4})\)$")
_SEASON_TRAIL_RE = re.compile(r'\s+(?:S\d{1,2}(?:E\d+)?|Season\s+\d+).*$', re.IGNORECASE)
_YEAR_TRAIL_RE = re.compile(r'\s+\d{4}$')
_PREFIX_RE = re.compile(
    r'^(\[[^\]]+\]\s*|www[\s.][\w.\-]+(?:[\s.][\w.\-]+)*\s*-\s*|www\s+\w+\s+\w+\s*-\s*'
    r'|rutor\.?\s*info\s*|\[?DEVIL-TORRENTS[^\]]*\]?\s*|HIDRATORRENTS[^\s]*\s*(?:MKV)?\s*-?(?:LEGENDADO)?-?\s*'
    r'|superseed\s+\S+\s*|\[BEST-TORRENTS[^\]]*\]\s*|\[XTORRENTY[^\]]*\]\s*)+',
    re.IGNORECASE,
)
_CYRILLIC_PREFIX_RE = re.compile(r'^[Ѐ-ӿ\s\(\)\[\]«»,.\-– - ]+')


def _clean_for_tmdb(raw: str) -> str:
    s = _PREFIX_RE.sub("", raw).strip()
    # Strip leading Cyrillic block (Russian title prepended by torrent sites)
    s = _CYRILLIC_PREFIX_RE.sub("", s).strip()
    # Strip parenthesised blocks containing Cyrillic (e.g. director name)
    s = re.sub(r'\([^)]*[Ѐ-ӿ][^)]*\)', '', s).strip()
    s = _SEASON_TRAIL_RE.sub("", s).strip()
    # Strip trailing year in parens  -  passed separately as year_hint
    s = re.sub(r'\s*\(\d{4}\)\s*$', '', s).strip()
    s = _YEAR_TRAIL_RE.sub("", s).strip()
    s = re.sub(r"[\[\(\{\s\-]+$", "", s).strip()
    return s


def _xml_escape(s: str) -> str:
    """Escape characters that have special meaning in XML element content."""
    return _stdlib_xml_escape(s or "")


def _movie_nfo(title: str, year: int | None, imdb_id: str) -> str:
    year_tag = f"\n  <year>{year}</year>" if year else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<movie>\n"
        f"  <title>{_xml_escape(title)}</title>{year_tag}\n"
        f'  <uniqueid type="imdb" default="true">{_xml_escape(imdb_id)}</uniqueid>\n'
        "</movie>\n"
    )


def _tvshow_nfo(title: str, imdb_id: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<tvshow>\n"
        f"  <title>{_xml_escape(title)}</title>\n"
        f'  <uniqueid type="imdb" default="true">{_xml_escape(imdb_id)}</uniqueid>\n'
        "</tvshow>\n"
    )


_BAD_TITLE_RE = re.compile(r'^Season\s+\d+$', re.IGNORECASE)


def repair_tvshow_titles() -> dict:
    """Rewrite tvshow.nfo files whose title is 'Season XX' instead of the real show name.

    Uses (in order of preference):
      1. Canonical title from monitored_series table (by imdb_id)
      2. Folder name stripped of trailing region codes like (IN) or (ZA)
    """
    media = Path(MEDIA_PATH)
    series_dir = media / "series"
    if not series_dir.is_dir():
        return {"fixed": 0, "skipped": 0}

    monitored_by_imdb = {s["imdb_id"]: s["title"] for s in db.get_all_monitored_series()}

    fixed = skipped = 0
    for folder in sorted(series_dir.iterdir()):
        if not folder.is_dir():
            continue
        nfo_path = folder / "tvshow.nfo"
        if not nfo_path.exists():
            continue
        try:
            root = ET.parse(nfo_path).getroot()
        except Exception:
            continue

        title_el = root.find("title")
        if not title_el or not title_el.text:
            continue
        if not _BAD_TITLE_RE.match(title_el.text.strip()):
            continue  # title already looks correct

        imdb_id = _read_imdb_from_nfo(nfo_path)
        if not imdb_id:
            skipped += 1
            continue

        # Prefer canonical DB title, fall back to folder name (strip region suffix like (IN))
        correct_title = monitored_by_imdb.get(imdb_id)
        if not correct_title:
            correct_title = re.sub(r'\s+\([A-Z]{2}\)$', '', folder.name).strip() or folder.name

        try:
            atomic_write_text(nfo_path, _tvshow_nfo(correct_title, imdb_id))
            log.info("NFO repair: '%s' -> '%s' (%s)", title_el.text.strip(), correct_title, nfo_path)
            fixed += 1
        except Exception as exc:
            log.warning("NFO repair: could not write %s: %s", nfo_path, exc)
            skipped += 1

    log.info("NFO repair complete: %d fixed, %d skipped", fixed, skipped)
    return {"fixed": fixed, "skipped": skipped}


def _read_imdb_from_nfo(nfo_path: Path) -> str | None:
    """Parse IMDb ID from a Kodi/Jellyfin .nfo file."""
    try:
        root = ET.parse(nfo_path).getroot()
        for uid in root.findall("uniqueid"):
            if uid.get("type") == "imdb" and uid.text:
                return uid.text.strip()
    except Exception:
        pass
    return None


def _download_image(url: str, dest: Path) -> bool:
    try:
        resp = _requests.get(url, timeout=20, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(65536):
                fh.write(chunk)
        log.info("Saved image: %s", dest)
        return True
    except Exception as exc:
        log.debug("Image download failed %s: %s", dest.name, exc)
        dest.unlink(missing_ok=True)
        return False


def _write(path: Path, content: str) -> bool:
    if path.exists():
        return False
    try:
        atomic_write_text(path, content)
        log.info("Wrote NFO: %s", path)
        return True
    except Exception as exc:
        log.warning("Could not write NFO %s: %s", path, exc)
        return False


def generate_all() -> dict:
    """Write missing NFO files for all movies and series using IMDb IDs from DB.

    For series folders not found in the DB (messy torrent names), a TMDB lookup
    is attempted so that duplicate series folders get the same IMDb ID in their
    tvshow.nfo  -  Jellyfin then merges them into a single library entry.
    """
    media = Path(MEDIA_PATH)
    items_by_title = {m["title"]: m["imdb_id"] for m in db.get_media_items()}
    # Secondary lookup: imdb_id → title (canonical)
    monitored_by_imdb = {s["imdb_id"]: s["title"] for s in db.get_all_monitored_series()}

    movies = series = 0

    movies_dir = media / "movies"
    if movies_dir.is_dir():
        for folder in movies_dir.iterdir():
            if not folder.is_dir():
                continue
            nfo_path = folder / f"{folder.name}.nfo"
            if nfo_path.exists():
                continue
            m_yr = _YEAR_RE.search(folder.name)
            year_hint = int(m_yr.group(1)) if m_yr else None
            clean = _clean_for_tmdb(folder.name)

            # DB lookup: try folder name as-is, then without year, then cleaned title
            imdb_id = (
                items_by_title.get(folder.name)
                or (items_by_title.get(clean) if clean else None)
                or (items_by_title.get(f"{clean} ({year_hint})") if clean and year_hint else None)
            )
            if imdb_id and imdb_id.startswith("unknown_"):
                imdb_id = None

            if not imdb_id:
                if not clean:
                    continue
                try:
                    imdb_id = tmdb.search_movie(clean, year_hint)
                    time.sleep(0.2)
                except Exception:
                    imdb_id = None
                if not imdb_id:
                    # Retry without year hint  -  some films have wrong year in folder name
                    try:
                        imdb_id = tmdb.search_movie(clean, None)
                        time.sleep(0.2)
                    except Exception:
                        imdb_id = None
                if not imdb_id:
                    continue
            m = _YEAR_RE.search(folder.name)
            year = int(m.group(1)) if m else None
            title = _YEAR_RE.sub("", folder.name).strip() if m else folder.name
            if _write(nfo_path, _movie_nfo(title, year, imdb_id)):
                movies += 1

    series_dir = media / "series"
    if series_dir.is_dir():
        for folder in series_dir.iterdir():
            if not folder.is_dir():
                continue
            nfo_path = folder / "tvshow.nfo"
            if nfo_path.exists():
                continue

            imdb_id = items_by_title.get(folder.name)
            if not imdb_id or imdb_id.startswith("unknown_"):
                # Not in DB  -  try TMDB lookup so duplicate folders get the right ID
                clean = _clean_for_tmdb(folder.name)
                if not clean:
                    continue
                try:
                    imdb_id = tmdb.search_tv(clean)
                    time.sleep(0.2)
                except Exception:
                    imdb_id = None
                if not imdb_id:
                    continue

            # Use canonical title from monitored_series if available
            display_title = monitored_by_imdb.get(imdb_id, _clean_for_tmdb(folder.name) or folder.name)
            # Safety guard: never write a "Season XX" string as the show title
            if _BAD_TITLE_RE.match(display_title):
                display_title = folder.name
            if _write(nfo_path, _tvshow_nfo(display_title, imdb_id)):
                series += 1

    log.info("NFO generation complete: %d movie(s), %d series", movies, series)
    return {"movies": movies, "series": series}


def fetch_images_for_folder(folder: Path, imdb_id: str, media_type: str = "movie") -> None:
    """Download poster.jpg + fanart.jpg for a single folder. Called atomically at creation."""
    poster = folder / "poster.jpg"
    fanart = folder / "fanart.jpg"
    if poster.exists() and fanart.exists():
        return
    try:
        p, b = tmdb.get_images(imdb_id, "movie" if media_type == "movie" else "tv")
        if p and not poster.exists():
            _download_image(f"{_IMAGE_BASE_POSTER}{p}", poster)
        if b and not fanart.exists():
            _download_image(f"{_IMAGE_BASE_BACKDROP}{b}", fanart)
    except Exception as exc:
        log.debug("fetch_images_for_folder %s: %s", folder.name, exc)


def fetch_local_images() -> dict:
    """Download poster.jpg, fanart.jpg, and episode stills from TMDB for all media folders.

    Skips files that already exist so re-runs are cheap. Sleeps 150 ms between
    TMDB calls to stay well under the 50 req/s rate limit.
    """
    media = Path(MEDIA_PATH)
    m_count = s_count = e_count = 0

    movies_dir = media / "movies"
    if movies_dir.is_dir():
        for folder in sorted(movies_dir.iterdir()):
            if not folder.is_dir():
                continue
            poster = folder / "poster.jpg"
            fanart = folder / "fanart.jpg"
            if poster.exists() and fanart.exists():
                continue
            nfo = folder / f"{folder.name}.nfo"
            if not nfo.exists():
                continue
            imdb_id = _read_imdb_from_nfo(nfo)
            if not imdb_id:
                continue
            try:
                p, b = tmdb.get_images(imdb_id, "movie")
                time.sleep(0.15)
            except Exception:
                continue
            if p and not poster.exists():
                if _download_image(f"{_IMAGE_BASE_POSTER}{p}", poster):
                    m_count += 1
            if b and not fanart.exists():
                _download_image(f"{_IMAGE_BASE_BACKDROP}{b}", fanart)

    series_dir = media / "series"
    if series_dir.is_dir():
        for folder in sorted(series_dir.iterdir()):
            if not folder.is_dir():
                continue
            nfo = folder / "tvshow.nfo"
            imdb_id = _read_imdb_from_nfo(nfo) if nfo.exists() else None

            if imdb_id:
                poster = folder / "poster.jpg"
                fanart = folder / "fanart.jpg"
                if not poster.exists() or not fanart.exists():
                    try:
                        p, b = tmdb.get_images(imdb_id, "tv")
                        time.sleep(0.15)
                        if p and not poster.exists():
                            if _download_image(f"{_IMAGE_BASE_POSTER}{p}", poster):
                                s_count += 1
                        if b and not fanart.exists():
                            _download_image(f"{_IMAGE_BASE_BACKDROP}{b}", fanart)
                    except Exception:
                        pass

                # Episode stills  -  one TMDB call per missing thumbnail
                tmdb_id = tmdb.find_by_imdb(imdb_id, kind="tv")
                if tmdb_id:
                    time.sleep(0.15)
                    for season_folder in sorted(folder.iterdir()):
                        if not season_folder.is_dir():
                            continue
                        for strm in sorted(season_folder.glob("*.strm")):
                            thumb = strm.with_name(f"{strm.stem}-thumb.jpg")
                            if thumb.exists():
                                continue
                            m = _EP_RE.search(strm.name)
                            if not m:
                                continue
                            s_num, e_num = int(m.group(1)), int(m.group(2))
                            try:
                                still = tmdb.get_episode_still(tmdb_id, s_num, e_num)
                                time.sleep(0.15)
                            except Exception:
                                continue
                            if still and _download_image(f"{_IMAGE_BASE_STILL}{still}", thumb):
                                e_count += 1

    log.info("fetch_local_images: %d movie poster(s), %d series poster(s), %d episode still(s)",
             m_count, s_count, e_count)
    return {"movies": m_count, "series": s_count, "episodes": e_count}
