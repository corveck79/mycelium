import logging
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
import strm_generator
import tmdb
import torbox
import torrentio
import zilean
import settings as _settings
from config import MEDIA_PATH
from torrentio import TorrentioStream

log = logging.getLogger(__name__)


class _RateLimitedError(Exception):
    pass


_YEAR_RE = re.compile(r"\((\d{4})\)$")
_TORRENT_ID_RE = re.compile(r"torrent_id[=:](\d+)", re.IGNORECASE)
_FILE_ID_RE = re.compile(r"file_id[=:](\d+)", re.IGNORECASE)
_EP_FILENAME_RE = re.compile(r"S(\d{1,2})E(\d{1,2})", re.IGNORECASE)
_QUALITY_TIER_RE = re.compile(r'\b(2160[pi]?|4[Kk]|UHD|1080[pi]?|720[pi]?|480[pi]?)\b', re.IGNORECASE)


def _extract_file_id(strm_url: str) -> str | None:
    m = _FILE_ID_RE.search(strm_url)
    return m.group(1) if m else None


def _normalize_title(s: str) -> str:
    s = re.sub(r"[^\w\s]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


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


_TITLE_TRAIL_RE = re.compile(r"[\[\(\{\s\-]+$")


def _quality_tier(path: Path) -> int:
    """Return quality rank from folder/filename. Higher = better. 0 = unknown."""
    text = f"{path.parent.name} {path.stem}"
    m = _QUALITY_TIER_RE.search(text)
    if not m:
        return 0
    label = m.group(1).upper()
    if label in ('2160P', '2160I', '2160', '4K', 'UHD'):
        return 4
    if label in ('1080P', '1080I', '1080'):
        return 3
    if label in ('720P', '720I', '720'):
        return 2
    if label in ('480P', '480I', '480'):
        return 1
    return 0


def _resolve_imdb(title: str, year: int | None, media_type: str) -> str | None:
    clean = _TITLE_TRAIL_RE.sub("", title).strip()
    if media_type == "movie":
        result = tmdb.search_movie(clean, year=year)
        if not result and clean != title:
            result = tmdb.search_movie(title, year=year)
        return result
    result = tmdb.search_tv(clean)
    if not result and clean != title:
        result = tmdb.search_tv(title)
    return result


def _fetch_candidates(imdb_id: str, title: str, media_type: str) -> list:
    if media_type == "movie":
        if _settings.get("ZILEAN_ENABLED", False):
            streams = zilean.fetch_streams(imdb_id)
            candidates = torrentio.rank_streams(streams)
            if candidates:
                return candidates
        streams = torrentio.fetch_streams("movie", imdb_id)
        return torrentio.rank_streams(streams)
    else:
        if _settings.get("ZILEAN_ENABLED", False):
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

    # Non-TorBox URLs (e.g. RealDebrid direct links, Catbox proxy) have a different
    # lifecycle and cleanup logic doesn't apply.
    if "real-debrid.com" in url or "/d/" in url or "/stream/" in url:
        return "ok"

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
            path.with_suffix(".nfo").unlink(missing_ok=True)
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
            path.with_suffix(".nfo").unlink(missing_ok=True)
        except Exception:
            pass
        db.insert_repair_item(run_id, str(path), title, media_type, torrent_id, None,
                              "unfixable", "no candidates found")
        return "unfixable"

    cached_hashes = torbox.check_cached([s.info_hash for s in candidates])
    cached = [s for s in candidates if s.info_hash in cached_hashes]
    to_try = cached[:1] or candidates[:1]

    winner: TorrentioStream | None = None
    rate_limited = False
    for stream in to_try:
        try:
            torbox.add_magnet(stream.magnet, reason="cleanup-repair")
            torbox.wait_until_ready(stream.info_hash)
            winner = stream
            break
        except Exception as exc:
            log.warning("Failed to add replacement for '%s' (hash=%s): %s", title, stream.info_hash, exc)
            if "429" in str(exc):
                rate_limited = True
                break

    if rate_limited:
        log.warning("Rate limited by TorBox for '%s' — will retry next cleanup run", title)
        raise _RateLimitedError()

    if winner:
        try:
            path.unlink()
            path.with_suffix(".nfo").unlink(missing_ok=True)
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


def _remove_duplicates(strm_files: list[Path], run_id: int) -> tuple[int, list[Path]]:
    """Group .strm files by normalized title/year/episode and remove duplicates.
    Returns (removed_count, remaining_paths)."""
    groups: dict[tuple, list[Path]] = {}
    for path in strm_files:
        try:
            rel = path.relative_to(MEDIA_PATH)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 2:
            continue
        kind = parts[0]
        if kind == "movies":
            title, year = _parse_folder_name(parts[1])
            key = ("movie", _normalize_title(title), year)
        elif kind == "series" and len(parts) >= 4:
            title = parts[1]
            m = _EP_FILENAME_RE.search(parts[3])
            if not m:
                continue
            key = ("episode", _normalize_title(title), int(m.group(1)), int(m.group(2)))
        else:
            continue
        groups.setdefault(key, []).append(path)

    removed = 0
    survivors: list[Path] = []
    for key, paths in groups.items():
        if len(paths) == 1:
            survivors.append(paths[0])
            continue

        # Quality-aware selection:
        # For movies: keep up to one 4K file AND one non-4K (1080p/720p) file.
        # For episodes: keep highest quality only.
        tiers = {p: _quality_tier(p) for p in paths}
        best_sort = sorted(paths, key=lambda p: (tiers[p], len(str(p)), str(p)), reverse=True)

        if key[0] == "movie":
            keepers: list[Path] = []
            best_4k = next((p for p in best_sort if tiers[p] >= 4), None)
            best_hd = next((p for p in best_sort if 1 <= tiers[p] <= 3), None)
            if best_4k:
                keepers.append(best_4k)
            if best_hd and best_hd not in keepers:
                keepers.append(best_hd)
            if not keepers:
                keepers.append(best_sort[0])
        else:
            keepers = [best_sort[0]]

        survivors.extend(keepers)
        keeper_names = ", ".join(k.name for k in keepers)
        for dup in paths:
            if dup in keepers:
                continue
            try:
                dup.unlink()
                # Remove the sibling NFO sidecar so the folder can be emptied —
                # otherwise the leftover .nfo keeps the (now media-less) folder
                # alive and Jellyfin may retain a ghost entry for it.
                dup.with_suffix(".nfo").unlink(missing_ok=True)
                log.info("Duplicate removed: %s (kept %s)", dup, keeper_names)
                try:
                    dup.parent.rmdir()
                except OSError:
                    pass
                db.insert_repair_item(
                    run_id, str(dup), keepers[0].parent.name, key[0], None, None,
                    "deleted", f"duplicate of {keeper_names}",
                )
                removed += 1
            except Exception as exc:
                log.warning("Could not remove duplicate %s: %s", dup, exc)
                survivors.append(dup)
    return removed, survivors


def _regenerate_wrong_files(strm_files: list[Path], mylist: list[dict], run_id: int) -> int:
    """For movies, detect .strm files pointing to non-main file (e.g. trailer) and regenerate."""
    import strm_generator

    mylist_by_id = {str(item.get("id")): item for item in mylist}
    regenerated = 0
    for path in strm_files:
        try:
            rel = path.relative_to(MEDIA_PATH)
        except ValueError:
            continue
        if not rel.parts or rel.parts[0] != "movies":
            continue
        try:
            url = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        torrent_id = _extract_torrent_id(url)
        file_id = _extract_file_id(url)
        if not torrent_id or not file_id:
            continue
        item = mylist_by_id.get(torrent_id)
        if not item:
            continue
        main = strm_generator._pick_main_movie_file(item.get("files") or [])
        if not main or str(main.get("id")) == file_id:
            continue
        # Current strm points to a wrong/smaller file (likely trailer) — regenerate
        log.info("Wrong file detected: %s (file_id=%s, should be %s) — regenerating",
                 path.name, file_id, main.get("id"))
        new_url = strm_generator._get_stream_url(int(torrent_id), main["id"])
        if not new_url:
            continue
        try:
            path.write_text(new_url, encoding="utf-8")
            db.insert_repair_item(
                run_id, str(path), path.parent.name, "movie", file_id,
                str(main.get("id")), "repaired", "regenerated wrong file (was trailer/sample)",
            )
            regenerated += 1
        except Exception as exc:
            log.warning("Could not rewrite %s: %s", path, exc)
    return regenerated


_SERIES_PREFIX_RE = re.compile(
    r'^(\[[^\]]+\]\s*|www[\s.][\w.\-]+(?:[\s.][\w.\-]+)*\s*-\s*|www\s+\w+\s+\w+\s*-\s*'
    r'|rutor\.?\s*info\s*|\[?DEVIL-TORRENTS[^\]]*\]?\s*|\[BEST-TORRENTS[^\]]*\]\s*'
    r'|\[XTORRENTY[^\]]*\]\s*|HIDRATORRENTS[^\s]*\s*(?:MKV)?\s*-?(?:LEGENDADO)?-?\s*'
    r'|superseed\s+\S+\s*)+',
    re.IGNORECASE,
)
_SEASON_TRAIL2_RE = re.compile(r'\s+(?:S\d{1,2}(?:E\d+)?|Season\s+\d+).*$', re.IGNORECASE)
_YEAR_TRAIL2_RE = re.compile(r'\s+\d{4}$')
_SEASON_DIR_RE = re.compile(r'[Ss]eason\s*(\d+)', re.IGNORECASE)
_EP_RE2 = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,2})', re.IGNORECASE)


def _series_clean_title(raw: str) -> str:
    s = _SERIES_PREFIX_RE.sub("", raw).strip()
    s = _SEASON_TRAIL2_RE.sub("", s).strip()
    s = _YEAR_TRAIL2_RE.sub("", s).strip()
    return re.sub(r"[\[\(\{\s\-]+$", "", s).strip() or raw


def remove_orphan_folders() -> int:
    """Delete movie/series folders that no longer contain any .strm file.

    After dedup/repair removes a .strm, leftover .nfo/poster files can keep an
    otherwise media-less folder alive — Jellyfin then retains a ghost entry for
    it. This sweep removes such folders entirely (and empty Season subfolders).
    Returns the number of folders removed.
    """
    import shutil

    media = Path(MEDIA_PATH)
    removed = 0

    movies_dir = media / "movies"
    if movies_dir.is_dir():
        for folder in movies_dir.iterdir():
            if not folder.is_dir():
                continue
            if not any(folder.rglob("*.strm")):
                try:
                    shutil.rmtree(folder)
                    log.info("Removed orphan movie folder (no .strm): %s", folder.name)
                    removed += 1
                except Exception as exc:
                    log.warning("Could not remove orphan folder %s: %s", folder, exc)

    series_dir = media / "series"
    if series_dir.is_dir():
        for folder in series_dir.iterdir():
            if not folder.is_dir():
                continue
            if not any(folder.rglob("*.strm")):
                try:
                    shutil.rmtree(folder)
                    log.info("Removed orphan series folder (no .strm): %s", folder.name)
                    removed += 1
                except Exception as exc:
                    log.warning("Could not remove orphan folder %s: %s", folder, exc)
                continue
            # Series still has episodes — prune empty season subfolders
            for sub in folder.iterdir():
                if sub.is_dir() and not any(sub.rglob("*.strm")):
                    try:
                        shutil.rmtree(sub)
                        log.info("Removed orphan season folder (no .strm): %s/%s",
                                 folder.name, sub.name)
                        removed += 1
                    except Exception as exc:
                        log.warning("Could not remove orphan season %s: %s", sub, exc)

    if removed:
        log.info("remove_orphan_folders: removed %d folder(s)", removed)
    return removed


def merge_series_duplicates() -> int:
    """Find series folders with the same IMDb ID and merge them into one canonical
    folder, moving all season/episode strm files across.  Returns number of
    duplicate folders removed."""
    import xml.etree.ElementTree as ET

    series_base = Path(MEDIA_PATH) / "series"
    if not series_base.is_dir():
        return 0

    items_by_title = {m["title"]: m["imdb_id"] for m in db.get_media_items()}
    monitored = {s["imdb_id"]: s["title"] for s in db.get_all_monitored_series()}

    def _nfo_imdb(folder: Path) -> str | None:
        nfo = folder / "tvshow.nfo"
        if not nfo.exists():
            return None
        try:
            root = ET.parse(nfo).getroot()
            for uid in root.findall("uniqueid"):
                if uid.get("type") == "imdb" and uid.text:
                    return uid.text.strip()
        except Exception:
            pass
        return None

    # Group folders by resolved IMDb ID
    groups: dict[str, list[Path]] = {}
    for folder in series_base.iterdir():
        if not folder.is_dir():
            continue
        # Primary: read directly from tvshow.nfo (always correct when present)
        imdb_id = _nfo_imdb(folder)
        if not imdb_id:
            imdb_id = items_by_title.get(folder.name)
        if not imdb_id or imdb_id.startswith("unknown_"):
            clean = _series_clean_title(folder.name)
            if clean:
                try:
                    imdb_id = tmdb.search_tv(clean)
                    time.sleep(0.15)
                except Exception:
                    imdb_id = None
        if imdb_id and not imdb_id.startswith("unknown_"):
            groups.setdefault(imdb_id, []).append(folder)

    removed = 0
    for imdb_id, folders in groups.items():
        if len(folders) <= 1:
            continue

        # Canonical: prefer folder whose name matches monitored title, else
        # the folder with the shortest cleaned name (most readable)
        mon_title = monitored.get(imdb_id, "")
        canonical = next((f for f in folders if f.name == mon_title), None)
        if not canonical:
            canonical = min(folders, key=lambda f: len(_series_clean_title(f.name)))

        display_title = mon_title or _series_clean_title(canonical.name)
        log.info("Merging series %r into canonical %r", [f.name for f in folders if f != canonical], canonical.name)

        for dup in folders:
            if dup == canonical:
                continue
            # Move .strm files into canonical season folders
            for item in list(dup.iterdir()):
                if item.is_dir() and _SEASON_DIR_RE.match(item.name):
                    dest_season = canonical / item.name
                    dest_season.mkdir(exist_ok=True)
                    for strm in list(item.glob("*.strm")):
                        ep_m = _EP_RE2.search(strm.stem)
                        if ep_m:
                            s_n, e_n = int(ep_m.group(1)), int(ep_m.group(2))
                            dest_name = f"{display_title} S{s_n:02d}E{e_n:02d}.strm"
                        else:
                            dest_name = strm.name
                        dest = dest_season / dest_name
                        if not dest.exists():
                            try:
                                dest.write_text(strm.read_text(encoding="utf-8"), encoding="utf-8")
                            except Exception as exc:
                                log.warning("Could not copy strm %s: %s", strm, exc)
                                continue
                        strm.unlink(missing_ok=True)
            # Remove entire duplicate folder (including leftover .nfo, posters, etc.)
            try:
                import shutil as _shutil
                _shutil.rmtree(dup)
                log.info("Removed duplicate series folder: %s", dup.name)
                removed += 1
            except Exception as exc:
                log.warning("Could not remove %s: %s", dup, exc)

    log.info("merge_series_duplicates: removed %d duplicate folder(s)", removed)
    return removed


def run_cleanup() -> None:
    log.info("Cleanup: starting strm scan in %s", MEDIA_PATH)
    run_id = db.insert_cleanup_run()
    scanned = repaired = deleted = unfixable = 0

    # 0. Merge series folders that share the same IMDb ID (torrent-site prefixes,
    #    case variants, year/season suffixes all land as separate folders).
    merge_series_duplicates()

    # 0b. Sweep folders that have lost all their .strm files (leftover .nfo/posters)
    orphan_removed = remove_orphan_folders()

    strm_files = _collect_strm_files()
    scanned = len(strm_files)
    log.info("Cleanup: found %d .strm files", scanned)

    if not strm_files:
        db.update_cleanup_run(run_id, 0, 0, 0, 0)
        if orphan_removed:
            strm_generator.run_and_refresh()
        return

    try:
        mylist = torbox.list_torrents()
    except Exception as exc:
        log.error("Cleanup: could not fetch TorBox mylist: %s — aborting", exc)
        db.update_cleanup_run(run_id, scanned, 0, 0, 0)
        return

    # 1. Remove duplicate .strm files first (same title/episode in multiple folders)
    dup_removed, strm_files = _remove_duplicates(strm_files, run_id)
    if dup_removed:
        log.info("Cleanup: removed %d duplicate .strm file(s)", dup_removed)
    deleted += dup_removed

    # 2. Regenerate movie .strm files that point to a trailer/wrong file
    fixed_files = _regenerate_wrong_files(strm_files, mylist, run_id)
    if fixed_files:
        log.info("Cleanup: regenerated %d wrong .strm file(s)", fixed_files)
    repaired += fixed_files

    # 3. Repair broken .strm files (torrent no longer in TorBox mylist)
    recently_unfixable = db.get_recently_unfixable_paths(hours=24)
    changed = False
    for path in strm_files:
        if str(path) in recently_unfixable:
            log.debug("Skipping recently-unfixable: %s", path.name)
            unfixable += 1
            continue
        try:
            result = _repair_strm(path, run_id, mylist)
        except _RateLimitedError:
            log.warning("Cleanup: TorBox rate limit hit — stopping repairs for this run")
            break
        time.sleep(2)
        if result == "repaired":
            repaired += 1
            changed = True
        elif result == "deleted":
            deleted += 1
            changed = True
        elif result == "unfixable":
            unfixable += 1

    if dup_removed or fixed_files or orphan_removed:
        changed = True

    db.update_cleanup_run(run_id, scanned, repaired, deleted, unfixable)
    log.info("Cleanup done: scanned=%d repaired=%d deleted=%d unfixable=%d",
             scanned, repaired, deleted, unfixable)

    if changed:
        strm_generator.run_and_refresh()
