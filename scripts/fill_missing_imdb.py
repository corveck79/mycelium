"""
Batch-resolve missing imdb_ids for virtual_items via TMDB.

Run from the project root:
    python scripts/fill_missing_imdb.py [--dry-run]

Works on unique (title, media_type) pairs so each TMDB lookup covers
all items that share a title  -  typically 10-200 rows per lookup.
"""
import os
import re
import sqlite3
import sys
import time

DB_PATH = "/mnt/nas-docker/mycelium/data/requests.db"
DRY_RUN = "--dry-run" in sys.argv

# Read TMDB key from settings DB before importing config/tmdb (they read env at import time)
_bootstrap_db = sqlite3.connect(DB_PATH)
_key_row = _bootstrap_db.execute(
    "SELECT value FROM settings WHERE key = 'TMDB_API_KEY'"
).fetchone()
if _key_row and _key_row[0]:
    os.environ["TMDB_API_KEY"] = _key_row[0]
_bootstrap_db.close()

sys.path.insert(0, "/mnt/nas-docker/mycelium")
import tmdb

# ── Title cleaning ────────────────────────────────────────────────────────────

# Prefixes to strip (case-insensitive)
_PREFIX_RE = re.compile(
    r"^(?:"
    r"\[.*?\]\s*"                          # [GROUP-NAME] ...
    r"|www\s+\S+\s+org\s*[-–]\s*"         # www UIndex org - ...
    r"|\S+\s+\S+\s+org\s*[-–]\s*"         # any.site.org - ...
    r")",
    re.IGNORECASE,
)

# Year like (2024) or 2024 at end
_YEAR_RE = re.compile(r"\(?(\d{4})\)?")

# Junk at end: season markers, quality, group tags
_JUNK_TRAIL_RE = re.compile(
    r"\s*(?:"
    r"S\d{1,2}(?:E\d{1,3})?"              # S01 / S01E01
    r"|Season\s+\d+"                       # Season 3
    r"|MULTi"                              # MULTi
    r"|COMPLETE"                           # COMPLETE
    r"|BluRay|BDRip|WEB(?:-?DL)?|HDTV"    # source tags
    r"|x264|x265|HEVC|AVC"                # codec tags
    r"|1080p|720p|2160p|4K"               # quality tags
    r"|\d{3,4}p"                           # generic resolution
    r").*$",
    re.IGNORECASE,
)

# Cyrillic / mixed scripts: extract the English part if there's an English title in parens
_ENGLISH_PAREN_RE = re.compile(r"([A-Za-z][A-Za-z0-9 ':!?&.,'-]{3,})\s*\(\d{4}\)")


def _clean(title: str) -> tuple[str, int | None]:
    """Return (cleaned_title, year_or_None)."""
    t = title.strip()

    # Strip site/group prefix
    t = _PREFIX_RE.sub("", t).strip()

    # Try to extract English title from mixed-script mess
    eng_match = _ENGLISH_PAREN_RE.search(t)
    if eng_match and any(ord(c) > 127 for c in t):
        t = eng_match.group(0)

    # Extract year
    years = _YEAR_RE.findall(t)
    year = int(years[-1]) if years else None

    # Strip junk at end
    t = _JUNK_TRAIL_RE.sub("", t).strip()

    # Remove year from title string
    t = re.sub(r"\s*\(?\d{4}\)?", "", t).strip()

    # Collapse whitespace, strip punctuation at end
    t = re.sub(r"\s+", " ", t).strip(" -–,.")

    return t, year


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    rows = db.execute("""
        SELECT title, media_type, COUNT(*) as n
        FROM virtual_items
        WHERE imdb_id IS NULL OR imdb_id = ''
        GROUP BY title, media_type
        ORDER BY n DESC
    """).fetchall()

    total = sum(r["n"] for r in rows)
    print(f"{len(rows)} unieke titels, {total} items totaal")
    if DRY_RUN:
        print("(dry-run modus  -  geen DB wijzigingen)\n")

    resolved = 0
    failed = 0

    for row in rows:
        raw_title = row["title"]
        media_type = row["media_type"]
        count = row["n"]

        cleaned, year = _clean(raw_title)
        if not cleaned:
            print(f"  SKIP (lege titel na cleaning): {raw_title!r}")
            failed += count
            continue

        # Try TMDB lookup
        imdb_id = None
        tries = [cleaned]
        # Also try without year if year was extracted
        if year:
            tries.append(cleaned)  # with year param
        # And without year param as fallback
        tries.append(cleaned)

        try:
            if media_type == "movie":
                imdb_id = tmdb.search_movie(cleaned, year)
                if not imdb_id and year:
                    imdb_id = tmdb.search_movie(cleaned, None)
            else:
                imdb_id = tmdb.search_tv(cleaned)
                if not imdb_id and year:
                    # Try appending year hint
                    imdb_id = tmdb.search_tv(f"{cleaned} {year}")
        except Exception as exc:
            print(f"  ERROR {raw_title!r}: {exc}")
            failed += count
            time.sleep(0.3)
            continue

        if imdb_id:
            print(f"  OK  [{media_type}] {raw_title!r} -> {imdb_id} (cleaned: {cleaned!r}, year={year}, {count} items)")
            if not DRY_RUN:
                db.execute(
                    "UPDATE virtual_items SET imdb_id = ? WHERE (imdb_id IS NULL OR imdb_id = '') AND title = ? AND media_type = ?",
                    (imdb_id, raw_title, media_type),
                )
                db.commit()
            resolved += count
        else:
            print(f"  MISS [{media_type}] {raw_title!r} (cleaned: {cleaned!r}, year={year}, {count} items)")
            failed += count

        time.sleep(0.25)

    print(f"\nDone: {resolved} items resolved, {failed} not found")


if __name__ == "__main__":
    main()
