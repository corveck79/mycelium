"""Quality auto-upgrade and season-pack consolidation.

run_auto_upgrade(): walks current successful requests, fetches fresh
candidates, and if a strictly better cached release exists, swaps it in.

run_pack_consolidation(): for series with N per-episode torrents of the
same season, looks for a cached season pack and atomically replaces them.
"""
import logging
from collections import defaultdict
from pathlib import Path

import db
import jellyfin
import settings as _settings
import strm_generator
import torbox
import torrentio
import zilean
from config import MEDIA_PATH
from webhook_parser import MediaRequest

log = logging.getLogger(__name__)


_QUALITY_RANK = {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1, "?": 0}


def _quality_score(q: str | None) -> int:
    return _QUALITY_RANK.get((q or "?").lower(), 0)


def _movie_runtime_override(imdb_id: str) -> dict:
    import tmdb
    try:
        sec = tmdb.get_movie_runtime_sec(imdb_id)
        return {"runtime_minutes": sec / 60.0 if sec else None}
    except Exception:
        return {}


def _episode_runtime_override(imdb_id: str, season: int, episode: int = 1) -> dict:
    import tmdb
    try:
        sec = tmdb.get_episode_runtime_sec(imdb_id, season, episode)
        return {"runtime_minutes": sec / 60.0 if sec else None}
    except Exception:
        return {}


def _fetch_movie_candidates(imdb_id: str) -> list:
    override = _movie_runtime_override(imdb_id)
    if _settings.get("ZILEAN_ENABLED", False):
        streams = zilean.fetch_streams(imdb_id)
        candidates = torrentio.rank_streams(streams, override=override)
        if candidates:
            return candidates
    streams = torrentio.fetch_streams("movie", imdb_id)
    return torrentio.rank_streams(streams, override=override)


def _fetch_season_candidates(imdb_id: str, season: int) -> list:
    override = _episode_runtime_override(imdb_id, season)
    if _settings.get("ZILEAN_ENABLED", False):
        streams = zilean.fetch_streams(imdb_id, season=season, episode=1)
        candidates = torrentio.rank_streams(streams, prefer_season_pack=True, override=override)
        if candidates:
            return candidates
    streams = torrentio.fetch_streams("series", imdb_id, season=season, episode=1)
    return torrentio.rank_streams(streams, prefer_season_pack=True, override=override)


def _better_cached(candidates: list, current_quality: str, current_hash: str) -> object | None:
    """Return the best cached candidate strictly better than current_quality."""
    if not candidates:
        return None
    cached = torbox.check_cached([c.info_hash for c in candidates[:20]])
    current_score = _quality_score(current_quality)
    for c in candidates:
        if c.info_hash not in cached:
            continue
        if c.info_hash.lower() == (current_hash or "").lower():
            continue
        if _quality_score(c.quality) > current_score:
            return c
    return None


def _run_auto_upgrade_catbox() -> int:
    """Catbox variant: scan virtual_items for better cached releases.
    Upgrade = swap magnet/hash/quality in DB only; .strm stays identical."""
    import catbox
    import debrid
    upgraded = 0
    items = db.get_upgradeable_virtual_items()
    log.info("Auto-upgrade (catbox): checking %d upgradeable virtual item(s)", len(items))
    for item in items:
        try:
            candidates = _fetch_movie_candidates(item["imdb_id"])
            if not candidates:
                continue
            hashes = [c.info_hash for c in candidates[:20]]
            cached = debrid.check_cached_multi(hashes).get("torbox", set())
            current_score = _quality_score(item.get("quality"))
            better = next(
                (c for c in candidates
                 if c.info_hash in cached
                 and c.info_hash.lower() != (item.get("info_hash") or "").lower()
                 and _quality_score(c.quality) > current_score),
                None,
            )
            if not better:
                continue
            log.info("Catbox upgrade: %s  %s → %s", item["title"], item.get("quality"), better.quality)
            source = better.name.split()[0] if better.name else None
            db.update_virtual_item_upgrade(item["token"], better.info_hash, better.magnet,
                                            better.quality, source)
            catbox.invalidate_url_cache(item["token"])
            db.log_activity("upgraded", item["title"],
                            f"{item.get('quality')} → {better.quality}", True)
            upgraded += 1
        except Exception as exc:
            log.warning("Catbox upgrade failed for %s: %s", item.get("title"), exc)
    if upgraded:
        log.info("Auto-upgrade (catbox): %d title(s) upgraded", upgraded)
    return upgraded


def run_auto_upgrade() -> int:
    """Scan recent successful requests for better cached releases."""
    if not _settings.get("AUTO_UPGRADE_ENABLED", True):
        return 0
    if _settings.get("CATBOX_MODE", False):
        return _run_auto_upgrade_catbox()
    log.info("Auto-upgrade: scanning")
    upgraded = 0
    successes = [r for r in db.get_recent(500) if r["status"] == "success" and r.get("info_hash")]
    for row in successes:
        if row["media_type"] != "movie":
            continue
        if _quality_score(row.get("quality")) >= _QUALITY_RANK["2160p"]:
            continue
        try:
            candidates = _fetch_movie_candidates(row["imdb_id"])
            better = _better_cached(candidates, row.get("quality") or "?", row["info_hash"] or "")
            if not better:
                continue
            log.info("Upgrade candidate for %s: %s → %s", row["title"], row.get("quality"), better.quality)
            torbox.add_magnet(better.magnet, reason="upgrade")
            item = torbox.wait_until_ready(better.info_hash)
            if not item:
                continue
            strm_generator.create_strm_for_torrent(item["id"], row["title"], "movie")
            db.update_request(row["id"], "success", quality=better.quality,
                              source=better.name.split()[0], info_hash=better.info_hash)
            db.log_activity("upgraded", row["title"],
                            f"{row.get('quality')} → {better.quality}", True)
            strm_generator._cache_cdn_url(better.info_hash, item, row["title"])
            upgraded += 1
        except Exception as exc:
            log.warning("Upgrade failed for %s: %s", row["title"], exc)
    if upgraded:
        jellyfin.refresh_library()
        log.info("Auto-upgrade: %d title(s) upgraded", upgraded)
    return upgraded


def _group_episode_strms_by_season() -> dict[tuple[str, int], list[Path]]:
    """Walk MEDIA_PATH/series and group strm files by (title, season)."""
    media = Path(MEDIA_PATH) / "series"
    if not media.is_dir():
        return {}
    groups: dict[tuple[str, int], list[Path]] = defaultdict(list)
    for show_dir in media.iterdir():
        if not show_dir.is_dir():
            continue
        for season_dir in show_dir.iterdir():
            if not season_dir.is_dir():
                continue
            try:
                season = int("".join(c for c in season_dir.name if c.isdigit()))
            except ValueError:
                continue
            strms = list(season_dir.glob("*.strm"))
            if strms:
                groups[(show_dir.name, season)] = strms
    return groups


def run_pack_consolidation() -> int:
    """For each series-season with >=3 per-episode strms, try to swap in a cached pack."""
    if not _settings.get("SEASON_PACK_CONSOLIDATION_ENABLED", True):
        return 0
    log.info("Season-pack consolidation: scanning")
    groups = _group_episode_strms_by_season()
    consolidated = 0
    monitored = {s["title"]: s["imdb_id"] for s in db.get_all_monitored_series()}
    for (title, season), strms in groups.items():
        if len(strms) < 3:
            continue
        imdb_id = monitored.get(title)
        if not imdb_id:
            continue
        try:
            candidates = _fetch_season_candidates(imdb_id, season)
            packs = [c for c in candidates if getattr(c, "is_season_pack", False)]
            if not packs:
                continue
            cached = torbox.check_cached([p.info_hash for p in packs[:10]])
            pack = next((p for p in packs if p.info_hash in cached), None)
            if not pack:
                continue
            log.info("Pack candidate for %s S%02d: %s (%d strms → 1 pack)",
                     title, season, pack.quality, len(strms))
            torbox.add_magnet(pack.magnet, reason="upgrade-pack")
            item = torbox.wait_until_ready(pack.info_hash)
            if not item:
                continue
            # Write pack strms first; only remove old files if new ones were created.
            new_count = strm_generator.process_torrent(item)
            if not new_count:
                log.warning("Pack consolidation: process_torrent wrote 0 strms for %s S%02d — keeping old strms",
                            title, season)
                continue
            for s in strms:
                try:
                    s.unlink()
                except Exception:
                    pass
            db.log_activity("consolidated", f"{title} S{season:02d}",
                            f"{len(strms)} episodes → 1 pack ({pack.quality})", True)
            consolidated += 1
        except Exception as exc:
            log.warning("Pack consolidation failed for %s S%02d: %s", title, season, exc)
    if consolidated:
        jellyfin.refresh_library()
        log.info("Season-pack consolidation: %d season(s) consolidated", consolidated)
    return consolidated


def recheck_wanted() -> int:
    """Re-search every wanted movie; add it the moment an acceptable-quality
    release becomes available. Quota-aware: stops once the TorBox createtorrent
    budget is low (RealDebrid fallback inside processor still applies)."""
    import processor
    wanted = db.get_wanted_movies()
    if not wanted:
        return 0
    log.info("Wanted: rechecking %d movie(s) for an acceptable release", len(wanted))
    added = 0
    for w in wanted:
        usage = torbox.createtorrent_usage()
        if usage["count"] >= torbox._CREATETORRENT_LIMIT_HOUR - 2:
            log.info("Wanted: createtorrent budget low (%d/%d)  -  pausing recheck",
                     usage["count"], torbox._CREATETORRENT_LIMIT_HOUR)
            break
        req = MediaRequest(title=w["title"], media_type="movie",
                            imdb_id=w["imdb_id"], seasons=[])
        try:
            ok, winner = processor._process_movie(req)
        except processor.RateLimited:
            log.info("Wanted: rate limited  -  pausing recheck")
            break
        except Exception as exc:
            log.warning("Wanted recheck failed for %s: %s", w["title"], exc)
            db.touch_wanted_movie(w["imdb_id"])
            continue
        if ok:
            db.remove_wanted_movie(w["imdb_id"])
            processor._WANTED.pop(w["imdb_id"], None)
            db.log_activity("found", w["title"],
                            f"acceptable release found ({winner.quality if winner else '?'})", True)
            log.info("Wanted: %s is now available  -  added", w["title"])
            added += 1
        else:
            # Still nothing acceptable; clear the transient wanted flag set by
            # _process_movie so it doesn't leak, and bump the attempt counter.
            processor._WANTED.pop(w["imdb_id"], None)
            db.touch_wanted_movie(w["imdb_id"])
    if added:
        jellyfin.refresh_library()
        log.info("Wanted: %d movie(s) became available and were added", added)
    return added
