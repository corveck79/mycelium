"""
Eenmalige migratie van plex-media root naar gestructureerde mappen.

Wat dit doet:
  1. .fsh cachebestanden -> .fsh/ submap
  2. Root-level filmfolders -> movies/
  3. Root-level Season XX/ mappen -> series/{Shownaam}/Season XX/

Draaien op de NAS:
  docker exec mycelium python3 /app/scripts/migrate_plex_media.py
Of via het gemounte pad:
  python3 /mnt/nas-docker/mycelium/scripts/migrate_plex_media.py
"""

import os
import re
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("SPORE_MEDIA_PATH", "/data/plex-media"))
MOVIES_DIR = ROOT / "movies"
SERIES_DIR = ROOT / "series"
FSH_DIR    = ROOT / ".fsh"

SKIP = {"movies", "series", ".fsh", "TEST"}
EP_RE = re.compile(r'\bS(\d{2})E\d{2}\b', re.I)
SEASON_RE = re.compile(r'^Season (\d+)$')


def main():
    moved = 0
    skipped = 0

    MOVIES_DIR.mkdir(exist_ok=True)
    SERIES_DIR.mkdir(exist_ok=True)
    FSH_DIR.mkdir(exist_ok=True)

    # 1. .fsh cachebestanden naar .fsh/
    for f in sorted(ROOT.glob("*.fsh")):
        if not f.is_file():
            continue
        dest = FSH_DIR / f.name
        if dest.exists():
            print(f"  SKIP .fsh (bestaat al): {f.name}")
            skipped += 1
            continue
        shutil.move(str(f), str(dest))
        print(f"  .fsh -> .fsh/{f.name}")
        moved += 1

    # 2. Root-level filmfolders -> movies/
    for item in sorted(ROOT.iterdir()):
        if item.name.startswith(".") or item.name in SKIP:
            continue
        if not item.is_dir():
            continue
        if SEASON_RE.match(item.name):
            continue  # stap 3
        dest = MOVIES_DIR / item.name
        if dest.exists():
            print(f"  SKIP film (bestaat al in movies/): {item.name}")
            skipped += 1
            continue
        shutil.move(str(item), str(dest))
        print(f"  film -> movies/{item.name}")
        moved += 1

    # 3. Root-level Season XX/ -> series/{Shownaam}/Season XX/
    for season_dir in sorted(ROOT.iterdir()):
        m = SEASON_RE.match(season_dir.name)
        if not m:
            continue
        season_num  = int(m.group(1))
        season_name = f"Season {season_num:02d}"

        for f in sorted(season_dir.iterdir()):
            if not f.is_file():
                continue
            ep_m = re.match(r'^(.+?)\s+S\d{2}E\d{2}', f.name, re.I)
            if not ep_m:
                print(f"  SKIP (geen episodepatroon): {f}")
                skipped += 1
                continue
            show = ep_m.group(1).strip()
            dest_dir = SERIES_DIR / show / season_name
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f.name
            if dest.exists():
                print(f"  SKIP serie (bestaat al): {show}/{season_name}/{f.name}")
                skipped += 1
                continue
            shutil.move(str(f), str(dest))
            print(f"  serie -> series/{show}/{season_name}/{f.name}")
            moved += 1

        # Verwijder lege Season-map
        remaining = list(season_dir.iterdir())
        if not remaining:
            season_dir.rmdir()
            print(f"  Verwijderd (leeg): {season_dir.name}/")
        else:
            print(f"  WAARSCHUWING: {season_dir.name}/ niet leeg, resterend: {[x.name for x in remaining]}")

    print(f"\nKlaar: {moved} verplaatst, {skipped} overgeslagen.")
    print("Stap daarna: Plex -> library -> Scan Library Files")


if __name__ == "__main__":
    main()
