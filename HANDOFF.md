# Mycelium — Session Handoff

Carry this into the next chat so context isn't lost. Last updated: 2026-05-20.

## What this project is
Self-hosted media pipeline (Flask + SQLite + React SPA) that turns watchlist
clicks into Jellyfin-ready `.strm` files streaming from TorBox. Runs as one
Docker container.

- **Live deployment (source of truth):** NAS at
  `/volume1/docker/jelly-stack/webhook` — this directory IS the git repo,
  on branch `main`, remote `github.com/corveck79/mycelium`.
- **Update flow on the NAS:** `git pull origin main && docker compose up -d --build`.
  Working tree is clean; data lives in `./data` (DB, `.strm`, settings) and
  survives rebuilds.
- **App URLs:** dashboard `http://10.0.0.10:8088/ui`, SPA `/app/`.
  Jellyfin at `http://10.0.0.10:8096`.

## Current state (end of session 2026-05-20)

Movies are clean (~79). Series still show duplicates in Jellyfin (screenshot:
87 entries with Cross ×4, The Curse of Oak Island ×3, Daredevil Rinascita ×5,
Fallout ×5, etc.). Most posters are missing (blue placeholders).

### Why series are still duplicated
- Disk has many duplicate series folders: `www UIndex org    -    Tracker`,
  `Tracker 2024`, `tracker 2024`, `[DEVIL-TORRENTS PL] Tracker`, all for the
  same show.
- `merge_series_duplicates()` was added to `cleanup.py` and wired into
  `run_cleanup()`, but the NAS container had **not yet been rebuilt** with the
  new code when the screenshot was taken.
- A second bug was also fixed: `dup.rmdir()` failed when season subdirs
  contained leftover `.nfo`/poster files → now uses `shutil.rmtree`.

### What to do on the NAS right now
1. `git pull origin main && docker compose up -d --build`
2. Dashboard → **Run Cleanup** button
3. Watch logs: `docker compose logs -f mycelium | grep -i "merge\|series\|removed"`
   Should see: `Merging series [...] into canonical 'Tracker'` etc.
4. After cleanup: Jellyfin → **Scan All Libraries** → series count should drop.
5. Dashboard → **Generate NFOs** button → downloads poster.jpg + fanart.jpg +
   episode stills from TMDB into every media folder (runs automatically on
   startup too, 150 s delay).

## All changes shipped this session (all on `main`)

### Previous session
- `395da07` — Fix Jellyfin 401: `jellyfin.py` now reads URL/API key from
  `settings.get()` (settings DB) instead of env-only `config.*`.
- `8b8f5ec` — MergeVersions groups Jellyfin movies by IMDb/TMDB provider ID.
  Deleting a `.strm` now also removes its sibling `.nfo`.
- `9fe182d` — `cleanup.remove_orphan_folders()`: sweeps folders with no `.strm`.
- `f1f2aef` — Catbox scan-burst probe-guard: skips TorBox re-add during library
  scans (was 45 s × 200 items).

### This session
- `a66fa2a` — **Local image fetcher**: `nfo_generator.fetch_local_images()`
  downloads `poster.jpg` + `fanart.jpg` (TMDB) for every movie/series folder,
  and `{stem}-thumb.jpg` episode stills for every series episode. Hooked into
  startup (150 s delay), `/ui/generate-nfos`, and library import.
  Files: `tmdb.py`, `nfo_generator.py`, `app.py`.
- `2795439` — `merge_series_duplicates()` in `cleanup.py`:
  - Groups series folders by IMDb ID read from `tvshow.nfo` (primary) → DB →
    TMDB lookup (fallback).
  - Moves `.strm` files into canonical folder, deletes duplicates.
  - Now called as step 0 in every `run_cleanup()` pass.
- `e6bdbf6` — Fix `merge_series_duplicates`: use `shutil.rmtree` instead of
  `rmdir` so duplicate folders with leftover `.nfo`/posters are fully removed.
- `335cf40` — **EXCLUDE_LANGUAGES** setting: detects Russian torrents (keywords
  + Cyrillic chars) and filters them out before quality sorting.
  Set `ru` in Settings → Languages & subtitles to block Russian dubs.

## Key files
| File | Purpose |
|------|---------|
| `jellyfin.py` | Jellyfin API calls (refresh, MergeVersions, image refresh) |
| `nfo_generator.py` | Write `.nfo` sidecars + fetch local images from TMDB |
| `cleanup.py` | Dedup `.strm`, merge series folders, orphan sweep |
| `torrentio.py` | Torrent candidate ranking + language filtering |
| `tmdb.py` | TMDB API: search, images, episode stills |
| `settings.py` | Runtime-editable settings (reads DB first, `.env` fallback) |
| `catbox.py` | Lazy TorBox materialization for `/stream/<token>` |

## Known remaining issues
- **Series dedup not yet verified in prod** — needs rebuild + cleanup run.
- **Jellyfin volume mount**: was changed from `/volume1/data/media` to
  `/volume1/docker/jelly-stack/webhook/data/media` in
  `/volume1/docker/jellyfin/docker-compose.yml`. Verify Jellyfin was restarted
  after that change and is reading the right 79 movies.
- **Poster download** may be slow on first run (~150 ms per item × all episodes).
  Check logs for `fetch_local_images` progress.
- **DB vs disk drift**: `STRM ∉ DB` / `DB ∉ STRM` counters on dashboard not
  addressed.

## Workflow notes / gotchas
- Work directly on `main` (user's preference). Develop, commit, push to `main`.
- `data/` is gitignored — can't inspect media from a cloud session; ask user to
  run `find`/`ls` on the NAS.
- POST endpoints are CSRF-protected → trigger via dashboard buttons, not curl.
- Single gunicorn worker, 8 threads → in-process state is shared and safe.
- Scheduler intervals need a container restart to change; most settings are
  hot-reloadable via Settings tab.
- Jellyfin compose file: `/volume1/docker/jellyfin/docker-compose.yml`
  (separate from the app compose at `/volume1/docker/jelly-stack/webhook/`).
