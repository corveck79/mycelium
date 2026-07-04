"""Trakt.tv integration: OAuth device-code auth, watchlist auto-request, watched sync.

Each user connects their own Trakt account (device-code flow, no browser redirect
needed). Tokens are stored per-user in the users table. TRAKT_CLIENT_ID/SECRET are
app-level credentials the admin configures once in Settings.
"""
import logging
import time

import requests

import db
import settings as _settings
from config import TRAKT_AUTO_REQUEST_CAP

log = logging.getLogger(__name__)

_BASE = "https://api.trakt.tv"
_HTTP_TIMEOUT = 15


def _client_id() -> str:
    return _settings.get("TRAKT_CLIENT_ID", "")


def _client_secret() -> str:
    return _settings.get("TRAKT_CLIENT_SECRET", "")


def is_configured() -> bool:
    return bool(_client_id() and _client_secret())


def _headers(access_token: str | None = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": _client_id(),
    }
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


def request_device_code() -> dict | None:
    """Start the device-code flow. Returns {device_code, user_code, verification_url,
    expires_in, interval} or None if Trakt isn't configured or the request failed."""
    if not is_configured():
        return None
    try:
        r = requests.post(f"{_BASE}/oauth/device/code",
                          json={"client_id": _client_id()}, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Trakt device code request failed: %s", exc)
        return None


def poll_device_token(device_code: str) -> dict:
    """Poll for the device-code flow's outcome once.
    Returns {"status": "success", "access_token", "refresh_token", "expires_at"},
    {"status": "pending"}, or {"status": "error", "error": "..."}."""
    try:
        r = requests.post(f"{_BASE}/oauth/device/token", json={
            "code": device_code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
        }, timeout=_HTTP_TIMEOUT)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    if r.status_code == 200:
        data = r.json()
        return {
            "status": "success",
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_at": time.time() + float(data.get("expires_in") or 0),
        }
    if r.status_code == 400:
        return {"status": "pending"}
    if r.status_code in (404, 409, 410, 418):
        return {"status": "error", "error": "device code expired or denied"}
    return {"status": "error", "error": f"HTTP {r.status_code}"}


def _refresh_token(refresh_token: str) -> dict | None:
    try:
        r = requests.post(f"{_BASE}/oauth/token", json={
            "refresh_token": refresh_token,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
        }, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_at": time.time() + float(data.get("expires_in") or 0),
        }
    except Exception as exc:
        log.warning("Trakt token refresh failed: %s", exc)
        return None


def _valid_access_token(user: dict) -> str | None:
    """Return a usable access token for this user, refreshing it first if expired.
    Persists a refreshed token back to the DB. Returns None if not connected."""
    token = user.get("trakt_access_token")
    if not token:
        return None
    expires = user.get("trakt_token_expires") or 0
    if time.time() < float(expires) - 60:
        return token
    refresh = user.get("trakt_refresh_token")
    if not refresh:
        return None
    refreshed = _refresh_token(refresh)
    if not refreshed:
        return None
    db.update_user(
        user["id"],
        trakt_access_token=refreshed["access_token"],
        trakt_refresh_token=refreshed["refresh_token"],
        trakt_token_expires=refreshed["expires_at"],
    )
    return refreshed["access_token"]


def status(user: dict) -> dict:
    connected = bool(user.get("trakt_access_token"))
    return {
        "connected": connected,
        "username": user.get("trakt_username") if connected else None,
        "configured": is_configured(),
        "synced_at": None,
    }


def revoke(user_id: int) -> None:
    db.update_user(user_id, trakt_access_token=None, trakt_refresh_token=None,
                    trakt_token_expires=None, trakt_username=None)


def _get(path: str, token: str) -> list | dict | None:
    try:
        r = requests.get(f"{_BASE}{path}", headers=_headers(token), timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Trakt GET %s failed: %s", path, exc)
        return None


def _extract_imdb(entry: dict, kind: str) -> tuple[str | None, int | None]:
    item = entry.get(kind) or {}
    ids = item.get("ids") or {}
    return ids.get("imdb"), ids.get("tmdb")


def get_watchlist(token: str) -> list[dict]:
    """Return the user's Trakt watchlist, normalized to
    [{"imdb_id", "tmdb_id", "media_type", "title"}]."""
    out: list[dict] = []
    for kind, media_type in (("movies", "movie"), ("shows", "series")):
        data = _get(f"/sync/watchlist/{kind}", token) or []
        for entry in data:
            item = entry.get(kind[:-1]) or {}
            imdb_id, tmdb_id = _extract_imdb(entry, kind[:-1])
            if not imdb_id:
                continue
            out.append({
                "imdb_id": imdb_id, "tmdb_id": tmdb_id,
                "media_type": media_type, "title": item.get("title") or imdb_id,
            })
    return out


def get_watched(token: str) -> dict:
    """Return watched movies + episodes from Trakt history, normalized as
    {"movies": [{"imdb_id","tmdb_id"}], "episodes": [{"imdb_id","tmdb_id","season","episode"}]}."""
    movies = []
    data = _get("/sync/watched/movies", token) or []
    for entry in data:
        imdb_id, tmdb_id = _extract_imdb(entry, "movie")
        if tmdb_id:
            movies.append({"imdb_id": imdb_id, "tmdb_id": tmdb_id})

    episodes = []
    data = _get("/sync/watched/shows", token) or []
    for entry in data:
        show = entry.get("show") or {}
        ids = show.get("ids") or {}
        imdb_id, tmdb_id = ids.get("imdb"), ids.get("tmdb")
        if not tmdb_id:
            continue
        for season in entry.get("seasons") or []:
            season_num = season.get("number")
            for ep in season.get("episodes") or []:
                episodes.append({
                    "imdb_id": imdb_id, "tmdb_id": tmdb_id,
                    "season": season_num, "episode": ep.get("number"),
                })
    return {"movies": movies, "episodes": episodes}


def sync_watched(user: dict) -> int:
    """Fetch watched movies/episodes from Trakt and upsert into trakt_watched.
    Returns the number of rows written."""
    token = _valid_access_token(user)
    if not token:
        return 0
    watched = get_watched(token)
    count = 0
    for m in watched["movies"]:
        db.upsert_trakt_watched(user["id"], m["tmdb_id"], "movie", imdb_id=m.get("imdb_id"))
        count += 1
    for e in watched["episodes"]:
        db.upsert_trakt_watched(user["id"], e["tmdb_id"], "episode", imdb_id=e.get("imdb_id"),
                                 season=e.get("season"), episode=e.get("episode"))
        count += 1
    return count


def sync_watchlist_auto_request(user: dict) -> int:
    """Queue new Trakt watchlist items for download, capped per run so a large
    imported watchlist can't flood TorBox's createtorrent quota in one call.
    Returns the number of items newly queued."""
    token = _valid_access_token(user)
    if not token:
        return 0
    import threading
    import processor
    from webhook_parser import MediaRequest

    watchlist = get_watchlist(token)
    cap = _settings.get("TRAKT_AUTO_REQUEST_CAP", TRAKT_AUTO_REQUEST_CAP)
    seen_movies = {r["imdb_id"] for r in db.get_recent(2000) if r.get("media_type") == "movie"}
    seen_series = {s["imdb_id"] for s in db.get_all_monitored_series()}

    added = 0
    for item in watchlist:
        if added >= cap:
            break
        imdb_id = item["imdb_id"]
        already_seen = imdb_id in (seen_movies if item["media_type"] == "movie" else seen_series)
        if already_seen:
            continue
        seasons = [] if item["media_type"] == "movie" else [1]
        req = MediaRequest(title=item["title"], media_type=item["media_type"],
                            imdb_id=imdb_id, seasons=seasons, tmdb_id=item.get("tmdb_id"))
        # processor.process() blocks on network calls and TorBox polling (can take
        # minutes), so queue each item in the background instead of waiting here -
        # same pattern app.py's manual request routes use.
        threading.Thread(target=processor.process, args=(req,),
                         name=f"trakt-sync-{imdb_id}", daemon=True).start()
        added += 1
    return added
