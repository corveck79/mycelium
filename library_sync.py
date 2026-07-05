"""Library synchronisation between disk and DB.

orphans(): returns counts of inconsistencies  -  strm-without-DB and DB-without-strm.
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

_SEASON_RE = re.compile(r"[Ss]eason\s*(\d+)")

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


def _clean_series_title(raw: str) -> str:
    """Clean a series folder name for display: strip torrent prefixes, year suffix, season markers."""
    s = _clean_title(raw)
    s = _SEASON_TRAIL_RE.sub("", s).strip()
    s = _YEAR_TRAIL_RE.sub("", s).strip()
    return s or raw


def _imdb_from_strm_files(series_dir: Path) -> str | None:
    """Scan .strm files inside a series folder and return the first tt-ID found."""
    for strm in series_dir.rglob("*.strm"):
        try:
            url = strm.read_text(encoding="utf-8").strip()
            m = re.search(r"tt\d{6,10}", url)
            if m:
                return m.group(0)
        except Exception:
            pass
    return None


def import_series_to_monitored() -> int:
    """Walk media/series/ and register any missing series in monitored_series.

    Falls back to scanning .strm URLs for IMDb IDs when the folder name is not
    in media_items (e.g. messy torrent-site prefixed folder names).
    Also cleans up messy titles on existing monitored_series rows.
    """
    base = Path(MEDIA_PATH) / "series"
    if not base.is_dir():
        return 0

    existing_rows = {s["imdb_id"]: s for s in db.get_all_monitored_series()}
    series_items = {m["title"]: m for m in db.get_media_items("series")}
    # secondary lookup: imdb_id → item (for dedup)
    series_by_imdb = {m["imdb_id"]: m for m in db.get_media_items("series")}

    # Clean up messy titles on already-monitored entries
    for imdb_id, row in existing_rows.items():
        raw_title = row["title"] or ""
        clean = _clean_series_title(raw_title)
        if clean != raw_title:
            seasons = [int(s) for s in (row.get("seasons") or "1").split(",") if s.strip().isdigit()]
            tmdb_id = row.get("tmdb_id")
            db.upsert_monitored_series(imdb_id, tmdb_id, clean, seasons or [1])
            log.info("Cleaned series title: %r → %r", raw_title, clean)

    added = 0
    for series_dir in sorted(base.iterdir()):
        if not series_dir.is_dir():
            continue
        folder_name = series_dir.name

        # Resolve IMDb ID: DB lookup first, then scan strm files
        item = series_items.get(folder_name)
        imdb_id = item["imdb_id"] if item else None
        if not imdb_id or imdb_id.startswith("unknown_"):
            imdb_id = _imdb_from_strm_files(series_dir)
        if not imdb_id or imdb_id.startswith("unknown_"):
            continue
        if imdb_id in existing_rows:
            continue
        if imdb_id in series_by_imdb and series_by_imdb[imdb_id]["imdb_id"].startswith("unknown_"):
            continue

        seasons = sorted({
            int(m.group(1))
            for sub in series_dir.iterdir()
            if sub.is_dir()
            for m in [_SEASON_RE.match(sub.name)]
            if m
        }) or [1]

        tmdb_id: int | None = None
        try:
            tmdb_id = tmdb.find_by_imdb(imdb_id, kind="tv")
        except Exception:
            pass

        clean_title = _clean_series_title(folder_name)
        db.upsert_monitored_series(imdb_id, tmdb_id, clean_title, seasons)
        existing_rows[imdb_id] = {"imdb_id": imdb_id, "title": clean_title}
        log.info("Imported series to monitored: %r (%s) seasons=%s", clean_title, imdb_id, seasons)
        added += 1

    log.info("import_series_to_monitored: added %d series", added)
    return added


_TORRENT_PREFIX_RE = re.compile(
    r"^(\[[^\]]+\]\s*|www[\s.][\w.\-]+(?:[\s.][\w.\-]+)*\s*-\s*|rutor\.?\s*info\s*|\[?DEVIL-TORRENTS[^\]]*\]?\s*|HIDRATORRENTS[^\s]*\s*(?:MKV)?\s*-?(?:LEGENDADO)?-?\s*|superseed\s+\S+\s*|www\s+\w+\s+\w+\s*-\s*)+",
    re.IGNORECASE,
)
_SEASON_TRAIL_RE = re.compile(r'\s+(?:S\d{1,2}(?:E\d+)?|Season\s+\d+).*$', re.IGNORECASE)
_YEAR_TRAIL_RE = re.compile(r'\s+\d{4}$')
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

        if media_type == "series":
            # Also try with trailing year (2024/2025) and season markers stripped
            for src in list(candidates):
                no_season = _SEASON_TRAIL_RE.sub("", src).strip()
                if no_season and no_season not in candidates:
                    candidates.append(no_season)
                no_year = _YEAR_TRAIL_RE.sub("", no_season or src).strip()
                if no_year and no_year not in candidates:
                    candidates.append(no_year)

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


# Patterns to strip before normalizing a folder name for dedup
_DEDUP_SITE_RE = re.compile(
    r'^\s*(?:\[[^\]]*\]\s*|'                              # [GROUP] prefix
    r'(?:www[\.\s]\S+)\s*|'                               # www.site.org OR www site org
    r'(?:https?://)\S+\s*|'                               # https://...
    r'(?:rutor|hidratorrents|xtorrenty|superseed|'
    r'byethost\d*|uindex|devil.torrents)'
    r'\s*[\.\-\s]?\s*(?:info|org|pl|com|net)?\s*[-–\s]*)+',
    re.IGNORECASE,
)
_DEDUP_CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]+[\s\S]*?(?=[A-Za-z])')  # Cyrillic run up to first Latin char
_DEDUP_TRAILING_BRACKET_RE = re.compile(r'[\(\[\{]\s*$')          # trailing open bracket


def _norm(s: str) -> str:
    """Aggressive normalization for duplicate detection.
    Strips site prefixes, Cyrillic, year, leading article, punctuation."""
    # Strip site/group prefixes
    s = _DEDUP_SITE_RE.sub('', s).strip()
    # Strip leftover TLD word after www stripping (e.g. "org - " after "www UIndex" was consumed)
    s = re.sub(r'^(?:org|com|net|info|pl)\s*[-–]+\s*', '', s, flags=re.IGNORECASE).strip()
    # Strip leading Cyrillic (torrent sites prepend Russian title)
    s = _DEDUP_CYRILLIC_RE.sub('', s).strip()
    # Strip anything left in leading brackets
    s = re.sub(r'^\[[^\]]*\]\s*', '', s).strip()
    # Strip common torrent language/format tags at the start (e.g. "MKV -LEGENDADO-")
    s = re.sub(r'^(?:MKV|AVI|MP4|HEVC|x265|x264)\s*[-–]?\s*(?:LEGENDADO|DUBBED|SUB|MULTI)?\s*[-–]?\s*',
               '', s, flags=re.IGNORECASE).strip()
    # Strip parenthesised blocks that contain Cyrillic (e.g. director names in Russian)
    s = re.sub(r'\([^)]*[Ѐ-ӿ][^)]*\)', '', s).strip()
    # Strip year in parens at end
    s = re.sub(r'\s*\(\d{4}\)\s*$', '', s).strip()
    # Strip all remaining parenthesised content  -  in folder names this is always
    # torrent metadata (director name, translator, etc.), never part of the actual title
    s = re.sub(r'\([^)]*\)', '', s).strip()
    # Strip trailing open bracket (malformed names like "Absolution (")
    s = _DEDUP_TRAILING_BRACKET_RE.sub('', s).strip()
    # Strip trailing square bracket junk
    s = re.sub(r'\s*\[.*$', '', s).strip()
    # Strip leading article
    s = re.sub(r'^(the|a|an)\s+', '', s, flags=re.IGNORECASE)
    # Alphanumeric only, lowercase
    return re.sub(r'[^a-z0-9]', '', s.lower())


_SCORE_SITE_RE = re.compile(
    r'\b(rutor|xtorrenty|uindex|hidratorrents|superseed|byethost|devil.torrents|warmachine\.)\b',
    re.IGNORECASE,
)


def _folder_score(name: str) -> int:
    """Score a folder name  -  higher = better (prefer clean TMDB-style names)."""
    score = 0
    # Heavy penalty for known torrent-site prefixes embedded in the name
    if _SCORE_SITE_RE.search(name):
        score -= 150
    if re.match(r'^[A-Za-z]', name):          # starts with Latin letter (not bracket/Cyrillic/digit)
        score += 100
    if not re.search(r'[\[\]\{\}]', name):    # no square/curly brackets
        score += 50
    if re.search(r'\(\d{4}\)$', name):        # ends with (year)  -  proper TMDB format
        score += 30
    if not re.search(r'[Ѐ-ӿ]', name):        # no Cyrillic
        score += 20
    # Penalize stray parens/brackets in the title part (before the year)
    title_part = re.sub(r'\s*\(\d{4}\)\s*$', '', name)
    if not re.search(r'[\(\)\[\]]', title_part):  # clean title, no stray brackets
        score += 15
    if "'" in name or re.search(r"[A-Z][a-z]", name):  # proper casing
        score += 10
    score -= len(name) // 20                  # slight penalty for very long names
    return score


def dedup_movie_folders() -> dict:
    """Find and remove duplicate movie folders, keeping the best-named one.
    Handles: site prefixes, Cyrillic, trailing brackets, case differences, article order.
    Returns {"checked": N, "removed": M}."""
    import shutil
    movies_dir = Path(MEDIA_PATH) / "movies"
    if not movies_dir.is_dir():
        return {"checked": 0, "removed": 0}

    # Group folders by (normalized_title, year)
    groups: dict[tuple, list[Path]] = {}
    for folder in movies_dir.iterdir():
        if not folder.is_dir():
            continue
        yr_m = _FOLDER_YEAR_RE.search(folder.name)
        year = yr_m.group(1) if yr_m else ""
        key = (_norm(folder.name), year)
        groups.setdefault(key, []).append(folder)

    removed = 0
    checked = len(groups)
    for key, folders in groups.items():
        if len(folders) < 2:
            continue
        norm_key, year = key
        if not norm_key:          # skip if normalization ate everything
            continue
        # Sort: highest score first = the one we keep
        folders.sort(key=lambda p: -_folder_score(p.name))
        keeper = folders[0]
        for dupe in folders[1:]:
            log.info("dedup: removing %r  →  keeping %r", dupe.name, keeper.name)
            try:
                shutil.rmtree(dupe)
                removed += 1
                # This is a destructive, unattended, fuzzy-match-based delete -
                # log it to the visible activity feed (not just the text log)
                # so a bad match is discoverable, not just silently gone.
                db.log_activity("dedup_removed", dupe.name, f"kept {keeper.name!r}", True)
            except Exception as exc:
                log.warning("dedup: could not remove %s: %s", dupe, exc)

    log.info("dedup_movie_folders: %d title groups checked, %d duplicate folders removed",
             checked, removed)
    return {"checked": checked, "removed": removed}
