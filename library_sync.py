"""Library synchronisation between disk and DB.

orphans(): returns counts of inconsistencies — strm-without-DB and DB-without-strm.
import_existing(): walk the media folder and insert any .strm files that have no
corresponding media_items entry, so DB-loss recoveries can be self-healing.
"""
import logging
import re
import time
from pathlib import Path

import db
import tmdb
from config import MEDIA_PATH

log = logging.getLogger(__name__)

_FOLDER_YEAR_RE = re.compile(r"\((\d{4})\)$")


def _strm_files() -> list[Path]:
    media = Path(MEDIA_PATH)
    if not media.is_dir():
        return []
    files: list[Path] = []
    for sub in ("movies", "series"):
        d = media / sub
        if d.is_dir():
            files.extend(d.rglob("*.strm"))
    return files


def orphans() -> dict:
    """Count strm files with no DB entry and DB entries with no strm file."""
    files = _strm_files()
    folder_names = {p.parent.name for p in files}

    media_items = db.get_media_items()
    db_titles = {m["title"] for m in media_items}

    strm_without_db = sum(1 for name in folder_names if name not in db_titles)
    db_without_strm = sum(1 for t in db_titles if t not in folder_names)

    return {
        "strm_count": len(files),
        "db_count": len(media_items),
        "strm_without_db": strm_without_db,
        "db_without_strm": db_without_strm,
    }


def import_existing() -> dict:
    """For each .strm file with no DB entry, insert a placeholder media_items row."""
    files = _strm_files()
    if not files:
        return {"scanned": 0, "imported": 0}

    existing_titles = {m["title"] for m in db.get_media_items()}
    imported = 0
    for path in files:
        # Folder name is the canonical title: "Title (Year)" or "Series Title".
        folder = path.parent.name
        # For series, walk one level up (path is series/Title/Season XX/file.strm)
        try:
            rel = path.relative_to(MEDIA_PATH)
            if rel.parts[0] == "series" and len(rel.parts) >= 4:
                folder = rel.parts[1]
        except ValueError:
            pass

        if folder in existing_titles:
            continue

        # Try to extract a fake imdb id from a strm URL if present, else use folder hash
        try:
            url = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        m = re.search(r"tt\d{6,10}", url)
        fake_imdb = m.group(0) if m else f"unknown_{abs(hash(folder)) % 10**8}"

        media_type = "series" if folder != path.parent.name else "movie"
        try:
            db.upsert_media_item(fake_imdb, folder, media_type)
            db.update_media_item_status(fake_imdb, media_type, "imported", strm_found=True)
            imported += 1
            existing_titles.add(folder)
        except Exception as exc:
            log.debug("Import skip %s: %s", folder, exc)

    log.info("Library import: scanned %d strm files, imported %d new items",
             len(files), imported)
    return {"scanned": len(files), "imported": imported}


_TORRENT_PREFIX_RE = re.compile(
    r"^(\[[^\]]+\]\s*|www[\s.][\w.\-]+\s*-\s*|rutor\.?\s*info\s*|\[?DEVIL-TORRENTS[^\]]*\]?\s*|HIDRATORRENTS[^\s]*\s*(?:MKV)?\s*-?(?:LEGENDADO)?-?\s*|superseed\s+\S+\s*)+",
    re.IGNORECASE,
)
_TRAILING_JUNK_RE = re.compile(r"[\[\(]\s*$")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9 :,'!&\-\.]+")


def _clean_title(raw: str) -> str:
    """Strip torrent-site prefixes and trailing junk before TMDB lookup."""
    s = raw.strip()
    s = _TORRENT_PREFIX_RE.sub("", s)
    s = _TRAILING_JUNK_RE.sub("", s).strip()
    # Drop nested parentheses that aren't the year
    s = re.sub(r"\([^)]*[A-Za-zА-Яа-я][^)]*\)", "", s).strip()
    return s


def _latin_only(s: str) -> str:
    """Return the longest contiguous Latin-alphabet run (for mixed Cyrillic titles)."""
    matches = _LATIN_RE.findall(s)
    if not matches:
        return s
    return max(matches, key=len).strip()


def resolve_unknowns() -> dict:
    """Resolve unknown_ placeholder IDs to real IMDb IDs via TMDB."""
    items = db.get_unknown_media_items()
    if not items:
        return {"resolved": 0, "failed": 0}
    resolved = 0
    failed = 0
    for item in items:
        old_id = item["imdb_id"]
        title_full = item["title"]
        media_type = item["media_type"]
        year_m = _FOLDER_YEAR_RE.search(title_full)
        year = int(year_m.group(1)) if year_m else None
        base = _FOLDER_YEAR_RE.sub("", title_full).strip()

        candidates: list[str] = []
        cleaned = _clean_title(base)
        if cleaned:
            candidates.append(cleaned)
        latin = _latin_only(cleaned or base)
        if latin and latin not in candidates:
            candidates.append(latin)
        if base not in candidates:
            candidates.append(base)

        real_id = None
        for cand in candidates:
            try:
                real_id = (tmdb.search_movie(cand, year)
                           if media_type == "movie"
                           else tmdb.search_tv(cand))
            except Exception as exc:
                log.debug("TMDB lookup failed for %r: %s", cand, exc)
                real_id = None
            if real_id:
                break
            time.sleep(0.15)

        if real_id and db.rekey_media_item(old_id, real_id, media_type):
            log.info("Resolved %s -> %s (%s)", old_id, real_id, title_full)
            resolved += 1
        else:
            log.debug("Unresolved: %s (tried %s)", title_full, candidates)
            failed += 1
        time.sleep(0.25)
    log.info("resolve_unknowns: %d resolved, %d unresolved", resolved, failed)
    return {"resolved": resolved, "failed": failed}
