"""Trakt.tv API wrapper  -  device auth, token management, watchlist sync, scrobble."""
from __future__ import annotations

import logging
import time

import requests as req_lib

import db
import settings

log = logging.getLogger(__name__)

_BASE = "https://api.trakt.tv"


def _client_id() -> str:
    return settings.get("TRAKT_CLIENT_ID", "") or ""


def _client_secret() -> str:
    return settings.get("TRAKT_CLIENT_SECRET", "") or ""


def _headers(access_token: str | None = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": _client_id(),
    }
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


# ── Token storage ──────────────────────────────────────────────────────────────

def get_token(user_id: int) -> dict | None:
    with db._connect() as c:
        row = c.execute(
            "SELECT * FROM trakt_tokens WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def save_token(user_id: int, access_token: str, refresh_token: str,
               expires_at: int, trakt_username: str | None = None) -> None:
    with db._connect() as c:
        c.execute(
            """INSERT INTO trakt_tokens
                   (user_id, access_token, refresh_token, expires_at, trakt_username)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   access_token   = excluded.access_token,
                   refresh_token  = excluded.refresh_token,
                   expires_at     = excluded.expires_at,
                   trakt_username = COALESCE(excluded.trakt_username, trakt_username)""",
            (user_id, access_token, refresh_token, expires_at, trakt_username),
        )


def delete_token(user_id: int) -> None:
    with db._connect() as c:
        c.execute("DELETE FROM trakt_tokens WHERE user_id = ?", (user_id,))


def _mark_synced(user_id: int) -> None:
    with db._connect() as c:
        c.execute(
            "UPDATE trakt_tokens SET synced_at = datetime('now') WHERE user_id = ?",
            (user_id,),
        )


def _get_all_tokens() -> list[dict]:
    with db._connect() as c:
        rows = c.execute("SELECT * FROM trakt_tokens").fetchall()
        return [dict(r) for r in rows]


# ── Token refresh ──────────────────────────────────────────────────────────────

def refresh_if_needed(tok: dict) -> dict:
    if time.time() < tok["expires_at"] - 3600:
        return tok
    try:
        r = req_lib.post(f"{_BASE}/oauth/token", json={
            "refresh_token": tok["refresh_token"],
            "client_id":     _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri":  "urn:ietf:wg:oauth:2.0:oob",
            "grant_type":    "refresh_token",
        }, timeout=15)
        r.raise_for_status()
        d = r.json()
        new_tok = {**tok,
                   "access_token":  d["access_token"],
                   "refresh_token": d["refresh_token"],
                   "expires_at":    int(time.time()) + d["expires_in"]}
        save_token(tok["user_id"], new_tok["access_token"],
                   new_tok["refresh_token"], new_tok["expires_at"])
        return new_tok
    except Exception as exc:
        log.warning("Trakt token refresh failed for user %d: %s", tok["user_id"], exc)
        return tok


# ── Device auth ────────────────────────────────────────────────────────────────

def start_device_auth() -> dict:
    """Returns {device_code, user_code, verification_url, expires_in, interval}."""
    cid = _client_id()
    if not cid:
        raise ValueError("TRAKT_CLIENT_ID not configured")
    r = req_lib.post(f"{_BASE}/oauth/device/code",
                     json={"client_id": cid}, timeout=15)
    r.raise_for_status()
    return r.json()


def poll_device_auth(device_code: str) -> dict | None:
    """Returns token dict on success, None if still pending, raises on error."""
    r = req_lib.post(f"{_BASE}/oauth/device/token", json={
        "code":          device_code,
        "client_id":     _client_id(),
        "client_secret": _client_secret(),
    }, timeout=15)
    if r.status_code == 400:
        return None   # still waiting
    if r.status_code == 404:
        raise ValueError("Invalid device code")
    if r.status_code == 409:
        raise ValueError("Code already used")
    if r.status_code == 410:
        raise ValueError("Code expired")
    if r.status_code == 418:
        raise ValueError("Access denied by user")
    r.raise_for_status()
    return r.json()


def revoke_token(access_token: str) -> None:
    try:
        req_lib.post(f"{_BASE}/oauth/revoke", json={
            "token":         access_token,
            "client_id":     _client_id(),
            "client_secret": _client_secret(),
        }, timeout=10)
    except Exception as exc:
        log.warning("Trakt revoke failed: %s", exc)


# ── Profile ────────────────────────────────────────────────────────────────────

def get_me(access_token: str) -> dict | None:
    try:
        r = req_lib.get(f"{_BASE}/users/me",
                        headers=_headers(access_token), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Trakt get_me failed: %s", exc)
        return None


# ── Watchlist sync ─────────────────────────────────────────────────────────────

def _fetch_watchlist(access_token: str, kind: str) -> list[dict]:
    try:
        r = req_lib.get(f"{_BASE}/users/me/watchlist/{kind}",
                        headers=_headers(access_token), timeout=30)
        r.raise_for_status()
        return r.json() or []
    except Exception as exc:
        log.warning("Trakt watchlist/%s fetch failed: %s", kind, exc)
        return []


def sync_user_watchlist(user_id: int, access_token: str) -> int:
    """Pulls Trakt watchlist into Mycelium watchlist. Returns count added."""
    added = 0

    for item in _fetch_watchlist(access_token, "movies"):
        m = item.get("movie", {})
        ids = m.get("ids", {})
        imdb_id = ids.get("imdb")
        if not imdb_id:
            continue
        try:
            db.add_to_watchlist(user_id, imdb_id, ids.get("tmdb"),
                                "movie", m.get("title") or "", None)
            added += 1
        except Exception:
            pass

    for item in _fetch_watchlist(access_token, "shows"):
        s = item.get("show", {})
        ids = s.get("ids", {})
        imdb_id = ids.get("imdb")
        if not imdb_id:
            continue
        try:
            db.add_to_watchlist(user_id, imdb_id, ids.get("tmdb"),
                                "tv", s.get("title") or "", None)
            added += 1
        except Exception:
            pass

    _mark_synced(user_id)
    return added


def _fetch_watched(access_token: str, kind: str) -> list[dict]:
    """kind: 'movies' or 'shows'"""
    try:
        r = req_lib.get(f"{_BASE}/users/me/watched/{kind}",
                        headers=_headers(access_token), timeout=30)
        r.raise_for_status()
        return r.json() or []
    except Exception as exc:
        log.warning("Trakt watched/%s fetch failed: %s", kind, exc)
        return []


def upsert_watched(user_id: int, imdb_id: str, media_type: str,
                   watched_at: str | None = None, tmdb_id: int | None = None) -> None:
    with db._connect() as c:
        c.execute(
            """INSERT INTO trakt_watched (user_id, imdb_id, tmdb_id, media_type, watched_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, imdb_id) DO UPDATE SET
                   tmdb_id    = COALESCE(excluded.tmdb_id, tmdb_id),
                   media_type = excluded.media_type,
                   watched_at = COALESCE(excluded.watched_at, watched_at)""",
            (user_id, imdb_id, tmdb_id, media_type, watched_at),
        )


def get_watched_imdb_ids(user_id: int) -> list[str]:
    with db._connect() as c:
        rows = c.execute(
            "SELECT imdb_id FROM trakt_watched WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [r["imdb_id"] for r in rows]


def upsert_watched_episode(user_id: int, imdb_id: str, season: int, episode: int) -> None:
    with db._connect() as c:
        c.execute(
            """INSERT OR IGNORE INTO trakt_watched_episodes (user_id, imdb_id, season, episode)
               VALUES (?, ?, ?, ?)""",
            (user_id, imdb_id, season, episode),
        )


def get_watched_episodes(user_id: int) -> dict[str, dict[str, list[int]]]:
    """Returns {imdb_id: {season_str: [ep, ...]}} for all watched episodes."""
    with db._connect() as c:
        rows = c.execute(
            "SELECT imdb_id, season, episode FROM trakt_watched_episodes WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    result: dict[str, dict[str, list[int]]] = {}
    for r in rows:
        show = result.setdefault(r["imdb_id"], {})
        show.setdefault(str(r["season"]), []).append(r["episode"])
    return result


def sync_user_watched(user_id: int, access_token: str) -> int:
    """Pulls Trakt watch history into trakt_watched. Returns count synced."""
    synced = 0
    for item in _fetch_watched(access_token, "movies"):
        m = item.get("movie", {})
        ids = m.get("ids", {})
        imdb_id = ids.get("imdb")
        if not imdb_id:
            continue
        upsert_watched(user_id, imdb_id, "movie", item.get("last_watched_at"),
                       tmdb_id=ids.get("tmdb"))
        synced += 1
    for item in _fetch_watched(access_token, "shows"):
        s = item.get("show", {})
        ids = s.get("ids", {})
        imdb_id = ids.get("imdb")
        if not imdb_id:
            continue
        upsert_watched(user_id, imdb_id, "tv", item.get("last_watched_at"),
                       tmdb_id=ids.get("tmdb"))
        # Also store per-episode data from Trakt's seasons array
        for season_data in item.get("seasons", []):
            snum = season_data.get("number")
            if snum is None:
                continue
            for ep_data in season_data.get("episodes", []):
                enum = ep_data.get("number")
                if enum is not None and ep_data.get("plays", 0) > 0:
                    upsert_watched_episode(user_id, imdb_id, snum, enum)
        synced += 1
    return synced


def auto_request_watchlist(user_id: int, access_token: str) -> int:
    """Queue new Trakt watchlist items for download (not just add to the browsing
    watchlist), capped per call so a large imported watchlist can't flood TorBox's
    createtorrent quota in one run. Returns the number of items newly queued."""
    import threading
    import processor
    import settings as _settings
    from config import TRAKT_AUTO_REQUEST_CAP
    from webhook_parser import MediaRequest

    cap = _settings.get("TRAKT_AUTO_REQUEST_CAP", TRAKT_AUTO_REQUEST_CAP)
    seen_movies = {r["imdb_id"] for r in db.get_recent(2000) if r.get("media_type") == "movie"}
    seen_series = {s["imdb_id"] for s in db.get_all_monitored_series()}

    added = 0
    for kind, media_type in (("movies", "movie"), ("shows", "series")):
        if added >= cap:
            break
        for item in _fetch_watchlist(access_token, kind):
            if added >= cap:
                break
            obj = item.get(kind[:-1], {})
            ids = obj.get("ids", {})
            imdb_id = ids.get("imdb")
            if not imdb_id:
                continue
            already_seen = imdb_id in (seen_movies if media_type == "movie" else seen_series)
            if already_seen:
                continue
            seasons = [] if media_type == "movie" else [1]
            req = MediaRequest(title=obj.get("title") or imdb_id, media_type=media_type,
                                imdb_id=imdb_id, seasons=seasons, tmdb_id=ids.get("tmdb"))
            threading.Thread(target=processor.process, args=(req,),
                             name=f"trakt-auto-request-{imdb_id}", daemon=True).start()
            added += 1
    return added


def sync_all_users() -> None:
    """Background job: sync watchlists + watch history for all connected users,
    then auto-request new watchlist items for download (capped)."""
    for tok in _get_all_tokens():
        try:
            tok = refresh_if_needed(tok)
            wl = sync_user_watchlist(tok["user_id"], tok["access_token"])
            watched = sync_user_watched(tok["user_id"], tok["access_token"])
            requested = auto_request_watchlist(tok["user_id"], tok["access_token"])
            log.info("Trakt sync: user %d  -  %d watchlist, %d watched, %d auto-requested",
                     tok["user_id"], wl, watched, requested)
        except Exception as exc:
            log.warning("Trakt sync failed for user %d: %s", tok["user_id"], exc)


# ── Scrobble ───────────────────────────────────────────────────────────────────

def scrobble(access_token: str, action: str, media_type: str,
             imdb_id: str, progress: float,
             season: int | None = None, episode: int | None = None,
             title: str | None = None) -> bool:
    """action: 'start' | 'pause' | 'stop'"""
    if media_type == "movie":
        payload = {
            "movie":    {"ids": {"imdb": imdb_id}, "title": title or ""},
            "progress": progress,
        }
    else:
        payload = {
            "show":    {"ids": {"imdb": imdb_id}},
            "episode": {"season": season, "number": episode},
            "progress": progress,
        }
    try:
        r = req_lib.post(f"{_BASE}/scrobble/{action}",
                         json=payload, headers=_headers(access_token), timeout=10)
        r.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Trakt scrobble/%s failed: %s", action, exc)
        return False
