"""Catbox-style lazy materialization for TorBox.

When CATBOX_MODE is enabled, .strm files contain a proxy URL pointing to
/stream/<token>. On playback the webhook ensures the torrent is in TorBox
(re-adding from the cached magnet if it has been released), fetches a fresh
CDN URL, and 307-redirects the client.

After CATBOX_IDLE_MINUTES of inactivity an item is removed from TorBox to
stay within TorBox's 30-day cache retention policy. The virtual entry stays
in the DB so playback works again on the next request.

Resolved CDN URLs are cached in-memory per token to avoid hammering TorBox's
60/hour createtorrent + 300/min general rate limits when Jellyfin sends
multiple probe/seek requests for the same item in quick succession.
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta

import db
import settings as _settings
import torbox
from config import CATBOX_HOST, CATBOX_IDLE_MINUTES

log = logging.getLogger(__name__)


_URL_CACHE_TTL_SEC = 82800  # 23 hours — within TorBox CDN URL 24h validity
ON_PLAY_READY_TIMEOUT_SEC = 45  # max wait on-play before giving up (cached = seconds)
_url_cache: dict[str, tuple[str, float]] = {}
_url_cache_lock = threading.Lock()

# Failure cooldown: after a failed materialize (429, timeout, no file found),
# block retries for a short window so Jellyfin's burst of probe requests doesn't
# hammer TorBox with repeated createtorrent calls.
_FAIL_COOLDOWN_SEC = 30        # standard failure (readd blocked, no file)
_FAIL_COOLDOWN_429_SEC = 120   # TorBox 429 — back off longer
_fail_cache: dict[str, float] = {}  # token → expiry monotonic timestamp
_fail_cache_lock = threading.Lock()

# ── Reason codes (structured, for playability_state + admin UI) ───────────────
REASON_UNKNOWN_TOKEN    = "UNKNOWN_TOKEN"
REASON_NO_IMDB          = "NO_IMDB"
REASON_TORRENTIO_EMPTY  = "TORRENTIO_EMPTY"
REASON_NO_CACHED        = "NO_CACHED_RELEASE"
REASON_WAIT_TIMEOUT     = "WAIT_TIMEOUT"
REASON_NO_FILE          = "NO_FILE"
REASON_RD_429           = "RD_429"
REASON_TB_429           = "TB_429"
REASON_ADD_FAILED       = "ADD_FAILED"
REASON_SEARCH_ERROR     = "SEARCH_UNAVAILABLE"

def _fail_get(token: str) -> bool:
    with _fail_cache_lock:
        exp = _fail_cache.get(token)
        if exp is None:
            return False
        if exp > time.monotonic():
            return True
        del _fail_cache[token]
        return False

def _fail_put(token: str, ttl: int = _FAIL_COOLDOWN_SEC) -> None:
    with _fail_cache_lock:
        _fail_cache[token] = time.monotonic() + ttl

_token_locks: dict[str, threading.Lock] = {}

# Per-content search cache so Zilean/Torrentio are called at most once per hour
# for the same (imdb_id, season, episode) combo, regardless of how many tokens share it.
_search_cache: dict[tuple, tuple[float, object]] = {}  # key → (expiry, result)
_search_cache_lock = threading.Lock()
_SEARCH_HIT_TTL    = 300    # 5 min: re-check soon if a cached release was found
_SEARCH_MISS_TTL   = 21600  # 6 h:  nothing cached — back off (matches _fail_put below)
_token_locks_lock = threading.Lock()

# ── scan/probe burst detection ────────────────────────────────────────────────
# A media-server library scan opens many DISTINCT .strm URLs in a short burst,
# whereas real playback touches a single token (plus seeks on that same token).
# When we see a burst of distinct tokens we treat the requests as scan probes and
# refuse to re-add idle-released torrents — re-materializing the whole library on
# every scan is slow and churns TorBox's createtorrent quota. Items already live
# in TorBox still resolve cheaply (mylist is cached), so they probe fine.
_SCAN_WINDOW_SEC = 25
_SCAN_DISTINCT_THRESHOLD = 4
_recent_tokens: dict[str, float] = {}
_recent_lock = threading.Lock()


def _is_scan_burst(token: str) -> bool:
    """Record this token request and report whether we appear to be inside a
    library-scan burst (many distinct tokens within the recent window)."""
    now = time.monotonic()
    with _recent_lock:
        for t, ts in list(_recent_tokens.items()):
            if now - ts > _SCAN_WINDOW_SEC:
                del _recent_tokens[t]
        _recent_tokens[token] = now
        return len(_recent_tokens) >= _SCAN_DISTINCT_THRESHOLD


def _token_lock(token: str) -> threading.Lock:
    with _token_locks_lock:
        lock = _token_locks.get(token)
        if lock is None:
            lock = threading.Lock()
            _token_locks[token] = lock
        return lock


def _content_key(item: dict) -> str | None:
    imdb_id = item.get("imdb_id")
    if not imdb_id:
        return None
    season, episode = item.get("season"), item.get("episode")
    if season and episode:
        return f"{imdb_id}:S{season:02d}E{episode:02d}"
    return imdb_id


def _cache_get(token: str) -> str | None:
    with _url_cache_lock:
        entry = _url_cache.get(token)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        if entry:
            del _url_cache[token]
    return None


def _cache_put(token: str, url: str) -> None:
    with _url_cache_lock:
        _url_cache[token] = (url, time.monotonic() + _URL_CACHE_TTL_SEC)


def cache_url(token: str, url: str) -> None:
    """Store a CDN URL in the in-memory cache (used by preload)."""
    _cache_put(token, url)


def invalidate_url_cache(token: str | None = None) -> None:
    with _url_cache_lock:
        if token is None:
            _url_cache.clear()
        else:
            _url_cache.pop(token, None)


def catbox_host() -> str:
    """Externally reachable host for the .strm proxy URL. Settings DB first,
    env/config fallback. Must be reachable from Jellyfin."""
    return (_settings.get("CATBOX_HOST", CATBOX_HOST) or "").strip()


def proxy_url(token: str) -> str:
    return f"{catbox_host().rstrip('/')}/stream/{token}"


def register(info_hash: str, magnet: str, title: str, media_type: str,
             strm_path: str | None = None, torbox_id: int | None = None,
             file_id: int | None = None, imdb_id: str | None = None,
             quality: str | None = None, source: str | None = None,
             size_gb: float | None = None, season: int | None = None,
             episode: int | None = None, year: int | None = None) -> str:
    token = uuid.uuid4().hex[:16]
    db.insert_virtual_item(token, info_hash, magnet, title, media_type,
                            strm_path=strm_path, torbox_id=torbox_id, file_id=file_id,
                            imdb_id=imdb_id, quality=quality, source=source,
                            size_gb=size_gb, season=season, episode=episode, year=year)
    return token


def materialize(token: str, allow_readd: bool | None = None) -> str | None:
    """Ensure the torrent is in TorBox and return a fresh stream URL.
    Cached URLs are served for up to 30 minutes to absorb Jellyfin's probe/seek
    bursts without spending TorBox createtorrent rate-limit slots.

    allow_readd controls whether an idle-released torrent may be re-added (which
    can block ~45s waiting for it to become ready). When None (default), it is
    auto-decided: during a scan-burst we skip the re-add so the scan stays fast.
    """
    cached = _cache_get(token)
    if cached:
        db.touch_virtual_item(token)
        return cached

    # Respect failure cooldown — don't spam TorBox after a recent failed attempt.
    if _fail_get(token):
        return None

    if allow_readd is None:
        allow_readd = not _is_scan_burst(token)

    with _token_lock(token):
        # Re-check inside the lock: another thread may have succeeded or set cooldown.
        cached = _cache_get(token)
        if cached:
            db.touch_virtual_item(token)
            return cached
        if _fail_get(token):
            return None
        url = _materialize_locked(token, allow_readd=allow_readd)
        if url:
            _cache_put(token, url)
            _schedule_next_episode_preload(token)
        else:
            _fail_put(token)
        return url


def _rd_get_url(item: dict, rd_id: str) -> str | None:
    """Get a playable URL from RealDebrid for this virtual item."""
    import re as _re
    import realdebrid as _rd
    if item["media_type"] == "movie":
        return _rd.get_main_video_url(rd_id)
    # Episode: match SxxExx in filename
    pairs = _rd.get_video_files_with_urls(rd_id)
    if not pairs:
        return None
    s_num, e_num = item.get("season"), item.get("episode")
    if s_num and e_num:
        ep_re = _re.compile(rf'[Ss]0?{s_num}[Ee]0?{e_num}\b', _re.IGNORECASE)
        matched = [(f, u) for f, u in pairs if ep_re.search(f.get("path") or f.get("name") or "")]
        if matched:
            return matched[0][1]
    return max(pairs, key=lambda fu: fu[0].get("bytes") or 0)[1]


def _materialize_locked(token: str, allow_readd: bool = True) -> str | None:
    item = db.get_virtual_item(token)
    if not item:
        log.warning("Catbox: unknown token %s", token)
        _metrics_inc("failed")
        return None

    ckey = _content_key(item)
    debrid_provider = (item.get("debrid_provider") or "torbox").lower()
    rematerialized = False

    # ── RealDebrid path ───────────────────────────────────────────────────────
    if debrid_provider == "realdebrid":
        import realdebrid as _rd
        rd_id = item.get("rd_id")

        # Fast path: rd_id still live in RD library
        if rd_id:
            info = _rd.get_info(rd_id)
            if info and info.get("status") == "downloaded":
                url = _rd_get_url(item, rd_id)
                if url:
                    db.touch_virtual_item(token)
                    if ckey:
                        db.update_playability_ok(ckey, "realdebrid")
                    _metrics_inc("ok" if not rematerialized else "rematerialized")
                    return url
            log.info("Catbox/RD: %s no longer in RD library — will re-add", item["title"])
            db.update_virtual_rd_id(token, None)
            rd_id = None
            rematerialized = True

        if not allow_readd:
            log.debug("Catbox/RD: skipping re-add for %s during scan-burst probe", item["title"])
            return None

        rematerialized = True
        log.info("Catbox/RD: searching cached release for %s", item["title"])
        fresh = _search_cached_release(item)
        if fresh is _SEARCH_UNAVAILABLE:
            _fail_put(token, _FAIL_COOLDOWN_SEC)
            if ckey:
                db.update_playability_fail(ckey, REASON_SEARCH_ERROR)
            return None
        if not fresh:
            log.error("Catbox/RD: no cached release for %s — keeping .strm, retry in 6h",
                      item["title"])
            _fail_put(token, 21600)  # 6h — repair job will clean up if truly dead
            if ckey:
                db.update_playability_fail(ckey, REASON_NO_CACHED)
            return None

        new_hash, new_magnet, provider = fresh
        db.update_virtual_item_upgrade(token, new_hash, new_magnet, None, None)
        db.update_virtual_debrid_provider(token, provider)
        if provider == "torbox":
            # Search found TorBox — fall through to TorBox block below
            debrid_provider = "torbox"
            item["debrid_provider"] = "torbox"
            item["info_hash"] = new_hash
            item["file_id"] = None
        else:
            try:
                result = _rd.add_magnet(new_magnet)
                rd_id = result["id"]
                rd_info = _rd.wait_until_ready(rd_id)
                if not rd_info:
                    log.error("Catbox/RD: wait_until_ready timed out for %s", item["title"])
                    _fail_put(token, _FAIL_COOLDOWN_SEC)
                    if ckey:
                        db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                    return None
                db.update_virtual_rd_id(token, rd_id)
                url = _rd_get_url(item, rd_id)
                if url:
                    db.touch_virtual_item(token)
                    if ckey:
                        db.update_playability_ok(ckey, "realdebrid")
                    _metrics_inc("rematerialized")
                return url
            except Exception as exc:
                is_429 = "429" in str(exc)
                log.error("Catbox/RD: add_magnet failed for %s: %s", item["title"], exc)
                _fail_put(token, _FAIL_COOLDOWN_429_SEC if is_429 else _FAIL_COOLDOWN_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_RD_429 if is_429 else REASON_ADD_FAILED)
                return None

    # ── TorBox path ───────────────────────────────────────────────────────────
    torbox_id = item["torbox_id"]

    # Fast path: cached torbox_id still live in TorBox.
    if torbox_id:
        live = torbox.find_by_id(torbox_id)
        if not live or not torbox._is_ready(live):
            torbox_id = None
            rematerialized = True

    # Second chance: torrent may still be in TorBox library under its hash.
    if not torbox_id and item.get("info_hash"):
        existing = torbox.find_by_hash(item["info_hash"])
        if existing and torbox._is_ready(existing):
            torbox_id = existing["id"]
            db.update_virtual_torbox_id(token, torbox_id)
            log.info("Catbox: %s still in library (id=%s) — no re-add needed",
                     item["title"], torbox_id)

    # Third chance: use the stored magnet to add directly — covers both items that
    # previously had a torbox_id (fell out of mylist top-1000) and freshly lazy-
    # registered items (torbox_id=NULL, magnet already selected at request time).
    if not torbox_id and item.get("magnet") and allow_readd:
        try:
            log.info("Catbox: %s adding stored magnet", item["title"])
            added = torbox.add_magnet(item["magnet"], reason="catbox-readd")
            _tid = added.get("id") or added.get("torrent_id")
            existing = added if _tid and torbox._is_ready(added) else (
                torbox.find_by_id(_tid) if _tid else
                torbox.find_by_hash(item["info_hash"], force_refresh=True)
            )
            if not (existing and torbox._is_ready(existing)) and _tid:
                existing = torbox.wait_until_ready(
                    item["info_hash"], timeout=ON_PLAY_READY_TIMEOUT_SEC, torrent_id=_tid)
            if existing and torbox._is_ready(existing):
                torbox_id = existing["id"]
                db.update_virtual_torbox_id(token, torbox_id)
                log.info("Catbox: %s added via stored magnet (id=%s)", item["title"], torbox_id)
        except Exception as exc:
            is_429 = "429" in str(exc)
            log.warning("Catbox: stored-magnet re-add failed for %s: %s", item["title"], exc)
            if is_429:
                _fail_put(token, _FAIL_COOLDOWN_429_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_TB_429)
                return None

    # Fourth chance: known hash may be cached on RD even if TorBox doesn't have it.
    # This avoids a full Torrentio search for items where Torrentio returns 0 results.
    if not torbox_id and item.get("info_hash") and allow_readd:
        try:
            import realdebrid as _rd
            if _rd.is_configured():
                known_hash = item["info_hash"].lower()
                rd_instant = _rd.check_cached([known_hash])
                if known_hash in {h.lower() for h in rd_instant}:
                    log.info("Catbox: known hash cached on RD for %s — switching to RD path",
                             item["title"])
                    db.update_virtual_debrid_provider(token, "realdebrid")
                    magnet = item.get("magnet") or f"magnet:?xt=urn:btih:{known_hash}"
                    rd_result = _rd.add_magnet(magnet)
                    rd_id = rd_result["id"]
                    rd_info = _rd.wait_until_ready(rd_id)
                    if rd_info:
                        db.update_virtual_rd_id(token, rd_id)
                        url = _rd_get_url(item, rd_id)
                        if url:
                            db.touch_virtual_item(token)
                            if ckey:
                                db.update_playability_ok(ckey, "realdebrid")
                            _metrics_inc("rematerialized")
                            return url
                    log.error("Catbox: RD wait_until_ready timed out for %s", item["title"])
                    _fail_put(token, _FAIL_COOLDOWN_SEC)
                    if ckey:
                        db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                    return None
        except Exception as exc:
            log.warning("Catbox: RD known-hash check failed for %s: %s", item["title"], exc)

    if not torbox_id and not allow_readd:
        log.debug("Catbox: skipping re-add for %s during scan-burst probe", item["title"])
        return None

    if not torbox_id:
        rematerialized = True
        log.info("Catbox: searching fresh cached release for %s", item["title"])
        fresh = _search_cached_release(item)
        if fresh is _SEARCH_UNAVAILABLE:
            _fail_put(token, _FAIL_COOLDOWN_SEC)
            if ckey:
                db.update_playability_fail(ckey, REASON_SEARCH_ERROR)
            return None
        if not fresh:
            log.error("Catbox: no cached release found for %s — keeping .strm, retry in 6h",
                      item["title"])
            _fail_put(token, 21600)  # 6h — repair job will clean up if truly dead
            if ckey:
                db.update_playability_fail(ckey, REASON_NO_CACHED)
            return None

        new_hash, new_magnet, provider = fresh
        db.update_virtual_debrid_provider(token, provider)
        if provider == "realdebrid":
            # Search found RD — switch provider and handle via RD
            import realdebrid as _rd
            db.update_virtual_item_upgrade(token, new_hash, new_magnet, None, None)
            try:
                result = _rd.add_magnet(new_magnet)
                rd_id = result["id"]
                rd_info = _rd.wait_until_ready(rd_id)
                if not rd_info:
                    _fail_put(token, _FAIL_COOLDOWN_SEC)
                    if ckey:
                        db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                    return None
                db.update_virtual_rd_id(token, rd_id)
                item["rd_id"] = rd_id
                url = _rd_get_url(item, rd_id)
                if url:
                    db.touch_virtual_item(token)
                    if ckey:
                        db.update_playability_ok(ckey, "realdebrid")
                    _metrics_inc("rematerialized")
                return url
            except Exception as exc:
                is_429 = "429" in str(exc)
                log.error("Catbox: RD add_magnet failed for %s: %s", item["title"], exc)
                _fail_put(token, _FAIL_COOLDOWN_429_SEC if is_429 else _FAIL_COOLDOWN_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_RD_429 if is_429 else REASON_ADD_FAILED)
                return None

        if new_hash != (item.get("info_hash") or "").lower():
            log.info("Catbox: swapping hash %s → %s", item["title"], new_hash)
            db.update_virtual_item_upgrade(token, new_hash, new_magnet, None, None)
            item["info_hash"] = new_hash
            item["file_id"] = None

        try:
            added = torbox.add_magnet(new_magnet, reason="catbox-search")
            # Use the ID from the add response to avoid a full mylist refresh.
            # TorBox returns "torrent_id" for cached adds, "id" for others.
            _tid = added.get("id") or added.get("torrent_id")
            live = added if _tid and torbox._is_ready(added) else None
            if not live:
                live = torbox.find_by_id(_tid) if _tid else None
            if not live or not torbox._is_ready(live):
                live = torbox.wait_until_ready(
                    new_hash, timeout=ON_PLAY_READY_TIMEOUT_SEC, torrent_id=_tid or None)
            if not live:
                log.error("Catbox: fresh release not ready for %s — keeping .strm, retry soon",
                          item["title"])
                _fail_put(token, _FAIL_COOLDOWN_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                return None
            torbox_id = live["id"]
            db.update_virtual_torbox_id(token, torbox_id)
        except Exception as exc:
            is_429 = "429" in str(exc)
            log.error("Catbox: add_magnet failed for %s: %s", token, exc)
            _fail_put(token, _FAIL_COOLDOWN_429_SEC if is_429 else _FAIL_COOLDOWN_SEC)
            if ckey:
                db.update_playability_fail(ckey, REASON_TB_429 if is_429 else REASON_ADD_FAILED)
            return None

    file_id = item["file_id"]
    if not file_id:
        live = torbox.find_by_id(torbox_id)
        if live:
            import re as _re
            import strm_generator
            if item["media_type"] == "movie":
                main = strm_generator._pick_main_movie_file(live.get("files") or [])
                if main:
                    file_id = main["id"]
                    db.update_virtual_file_id(token, file_id)
                elif not (live.get("files")):
                    # TorBox returned the torrent without a files list (common for the
                    # ?id= single-item endpoint).  Use file_id=0 which tells TorBox to
                    # serve the largest file automatically — works for single-file movies.
                    log.info("Catbox: no files list for %s — using file_id=0 (auto)", item["title"])
                    file_id = 0
            else:
                videos = [f for f in (live.get("files") or [])
                          if strm_generator._is_video(f.get("name") or "")
                          and not strm_generator._is_trailer(f)]
                s_num = item.get("season")
                e_num = item.get("episode")
                if s_num and e_num:
                    ep_re = _re.compile(rf'[Ss]0?{s_num}[Ee]0?{e_num}\b', _re.IGNORECASE)
                    matched = [f for f in videos if ep_re.search(f.get("name") or "")]
                    main = matched[0] if matched else (
                        max(videos, key=lambda f: f.get("size") or 0) if videos else None
                    )
                else:
                    main = max(videos, key=lambda f: f.get("size") or 0) if videos else None
                if main:
                    file_id = main["id"]
                    db.update_virtual_file_id(token, file_id)

    if file_id is None or (not file_id and file_id != 0):
        log.error("Catbox: no playable file found for %s — keeping .strm, retry later", token)
        _fail_put(token, _FAIL_COOLDOWN_SEC)
        if ckey:
            db.update_playability_fail(ckey, REASON_NO_FILE)
        return None

    import strm_generator
    url = strm_generator._get_stream_url(torbox_id, file_id)
    if url:
        db.touch_virtual_item(token)
        if ckey:
            db.update_playability_ok(ckey, "torbox")
        _metrics_inc("rematerialized" if rematerialized else "ok")
    else:
        _metrics_inc("failed")
    return url


def _metrics_inc(result: str) -> None:
    try:
        import metrics_prom
        metrics_prom.catbox_stream_total.labels(result=result).inc()
    except Exception:
        pass


def _remove_strm(item: dict) -> None:
    """Delete the .strm file for a definitively dead item so Jellyfin stops showing it."""
    import os
    strm_path = item.get("strm_path")
    if not strm_path:
        return
    try:
        if os.path.exists(strm_path):
            os.remove(strm_path)
            log.info("Catbox: removed dead .strm %s", strm_path)
    except Exception as exc:
        log.warning("Catbox: could not remove .strm %s: %s", strm_path, exc)


_SEARCH_UNAVAILABLE = object()  # sentinel: search couldn't run (no imdb_id, network error)


def _search_cached_release(item: dict) -> object:
    """Thin cache layer around _search_best_cached_release.

    Deduplicates Zilean/Torrentio calls when multiple tokens share the same
    (imdb_id, season, episode).  A miss is cached for 6 h, a hit for 5 min
    (so a newly-cached release is picked up quickly on retry).
    """
    imdb_id = item.get("imdb_id")
    if not imdb_id:
        # No imdb_id → _search_best_cached_release will handle + log the warning.
        return _search_best_cached_release(item)
    key = (imdb_id, item.get("season"), item.get("episode"))
    now = time.monotonic()
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if entry and entry[0] > now:
            result = entry[1]
            log.debug("Catbox search cache hit for %s %s — skipping Zilean/Torrentio",
                      imdb_id, key[1:])
            return result
    result = _search_best_cached_release(item)
    ttl = _SEARCH_HIT_TTL if result and result is not _SEARCH_UNAVAILABLE else _SEARCH_MISS_TTL
    with _search_cache_lock:
        _search_cache[key] = (now + ttl, result)
    return result


def _search_best_cached_release(item: dict) -> tuple[str, str] | None | object:
    """Search Torrentio for the best currently-cached release for this item.

    Returns:
      (info_hash, magnet)  — found a cached release
      None                 — searched OK, nothing cached right now
      _SEARCH_UNAVAILABLE  — couldn't search (no imdb_id, network error) — do NOT remove .strm
    """
    imdb_id = item.get("imdb_id")
    if not imdb_id:
        # Try to resolve imdb_id from TMDB using title + year, then persist it.
        try:
            import tmdb as _tmdb
            kind = "movie" if item.get("media_type") == "movie" else "tv"
            title = item.get("title") or ""
            year = item.get("year")
            results = _tmdb._get("/search/" + ("movie" if kind == "movie" else "tv"),
                                  params={"query": title, "year": year or ""}) or {}
            hits = results.get("results") or []
            if hits:
                tmdb_id = hits[0]["id"]
                imdb_id = _tmdb.tmdb_to_imdb(tmdb_id, media_type=kind)
                if imdb_id:
                    db.update_virtual_item_imdb(item["token"], imdb_id)
                    log.info("Catbox search: resolved imdb_id %s for %s via TMDB",
                             imdb_id, title)
        except Exception as exc:
            log.warning("Catbox search: TMDB lookup failed for %s: %s", item.get("title"), exc)
    if not imdb_id:
        log.warning("Catbox search: no imdb_id for %s — keeping .strm, will retry later",
                    item["title"])
        return _SEARCH_UNAVAILABLE
    try:
        import concurrent.futures
        import torrentio
        import debrid
        import blacklist
        media_type = item["media_type"]
        season = item.get("season")
        episode = item.get("episode")
        import zilean as _zilean

        # Run Zilean and Torrentio in parallel to halve search latency.
        def _fetch_zilean():
            if not _settings.get("ZILEAN_ENABLED", False):
                return []
            return _zilean.fetch_streams(imdb_id, season=season, episode=episode)

        def _fetch_torrentio():
            return torrentio.fetch_streams(
                "movie" if media_type == "movie" else "series",
                imdb_id, season=season, episode=episode,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_zilean = ex.submit(_fetch_zilean)
            f_torrentio = ex.submit(_fetch_torrentio)
            zilean_streams = f_zilean.result()
            torrentio_streams = f_torrentio.result()

        log.info("Catbox search: Zilean=%d Torrentio=%d stream(s) for %s (%s)",
                 len(zilean_streams), len(torrentio_streams), item.get("title"), imdb_id)
        # Merge: Zilean first, add Torrentio entries not already in Zilean (dedup by info_hash)
        seen_hashes = {s.info_hash for s in zilean_streams}
        streams = zilean_streams + [s for s in torrentio_streams if s.info_hash not in seen_hashes]
        log.info("Catbox search: %d stream(s) total after merge for %s",
                 len(streams), item.get("title"))
        if not streams:
            return None
        ranked = torrentio.rank_streams(streams)
        ranked = blacklist.filter_candidates(ranked)
        log.info("Catbox search: %d candidate(s) after ranking/filter for %s",
                 len(ranked), item.get("title"))
        if not ranked:
            return None
        hashes = [s.info_hash for s in ranked]
        cache_results = debrid.check_cached_multi(hashes)
        rd_cached = cache_results.get("realdebrid", set())
        tb_cached = cache_results.get("torbox", set())
        log.info("Catbox search: RD=%d TB=%d cached out of %d for %s",
                 len(rd_cached), len(tb_cached), len(ranked), item.get("title"))
        # RD first, TorBox fallback
        for s in ranked:
            if s.info_hash in rd_cached:
                return s.info_hash.lower(), s.magnet, "realdebrid"
        for s in ranked:
            if s.info_hash in tb_cached:
                return s.info_hash.lower(), s.magnet, "torbox"
        return None
    except Exception as exc:
        log.warning("Catbox search: failed for %s: %s — keeping .strm", item["title"], exc)
        return _SEARCH_UNAVAILABLE


def _schedule_next_episode_preload(token: str) -> None:
    """After a series episode materializes successfully, preload the next episode
    in background so it is instant when the user gets there.

    Lookup order: same season episode+1, then season+1 episode 1.
    Only fires if CATBOX_PRELOAD is enabled and the next episode has a
    registered virtual_item with info_hash + magnet."""
    try:
        import settings as _s
        import config as _cfg
        if not _s.get("CATBOX_PRELOAD", _cfg.CATBOX_PRELOAD):
            return
        item = db.get_virtual_item(token)
        if not item or item.get("media_type") != "series":
            return
        imdb_id = item.get("imdb_id")
        season = item.get("season")
        episode = item.get("episode")
        if not (imdb_id and season and episode):
            return
        # Try next episode in same season, then first episode of the next season
        nxt = db.get_virtual_item_by_episode(imdb_id, season, episode + 1)
        if not nxt:
            nxt = db.get_virtual_item_by_episode(imdb_id, season + 1, 1)
        if not nxt:
            return
        next_hash = nxt.get("info_hash")
        next_magnet = nxt.get("magnet")
        next_title = nxt.get("title") or ""
        if not (next_hash and next_magnet):
            return
        import strm_generator as _sg
        import threading as _t
        _t.Thread(
            target=_sg._preload_torrent,
            args=(next_hash, next_magnet, next_title),
            daemon=True,
        ).start()
        log.debug("Catbox: scheduled preload for next episode %s", next_title)
    except Exception as exc:
        log.debug("Catbox: next-episode preload scheduling failed: %s", exc)


def release_idle() -> int:
    """Remove TorBox items idle longer than CATBOX_IDLE_MINUTES. Returns count released."""
    cutoff = datetime.utcnow() - timedelta(minutes=CATBOX_IDLE_MINUTES)
    cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    items = db.get_idle_virtual_items(cutoff_iso)
    released = 0
    for item in items:
        deleted = torbox.delete_torrent(item["torbox_id"])
        if not deleted:
            # Torrent may already be gone from TorBox (evicted or manually removed).
            # Still clear the local reference so catbox can re-add it on next play.
            still_there = torbox.find_by_id(item["torbox_id"])
            if still_there:
                continue
        db.update_virtual_torbox_id(item["token"], None)
        log.info("Catbox: released idle torrent %s (%s)", item["torbox_id"], item["title"])
        released += 1
    if released:
        log.info("Catbox: released %d idle torrent(s)", released)
    return released
