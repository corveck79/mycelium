"""MDBList integration: per-user API key, list sync, capped auto-request.

Each user connects their own MDBList API key (from mdblist.com/preferences)
and picks which of their lists to sync. Unlike Trakt this has no OAuth flow -
MDBList uses a single long-lived API key per account.
"""
import logging
import time

import requests

import db
import settings as _settings
from config import MDBLIST_AUTO_REQUEST_CAP

log = logging.getLogger(__name__)

_BASE = "https://api.mdblist.com"
_HTTP_TIMEOUT = 15


def is_configured(user: dict) -> bool:
    return bool(user.get("mdblist_api_key"))


def get_user_lists(api_key: str) -> list[dict]:
    """Return the account's own lists: [{"id", "name", "items": count}, ...]."""
    try:
        r = requests.get(f"{_BASE}/lists/user", params={"apikey": api_key}, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("MDBList get_user_lists failed: %s", exc)
        return []


def get_list_items(list_id: int | str, api_key: str) -> list[dict]:
    """Return a list's items normalized to [{"imdb_id","tmdb_id","media_type","title"}]."""
    try:
        r = requests.get(f"{_BASE}/lists/{list_id}/items",
                         params={"apikey": api_key}, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as exc:
        log.warning("MDBList get_list_items(%s) failed: %s", list_id, exc)
        return []
    out = []
    for kind, media_type in (("movies", "movie"), ("shows", "series")):
        for item in data.get(kind) or []:
            imdb_id = item.get("imdb_id")
            if not imdb_id:
                continue
            out.append({
                "imdb_id": imdb_id, "tmdb_id": item.get("id") or item.get("tmdb_id"),
                "media_type": media_type, "title": item.get("title") or imdb_id,
            })
    return out


def sync_auto_request(user: dict) -> int:
    """Fetch items from all of the user's configured MDBList lists and queue new
    ones for download, capped so a large list can't flood TorBox in one sync."""
    api_key = user.get("mdblist_api_key")
    if not api_key:
        return 0
    list_ids = [l.strip() for l in (user.get("mdblist_list_ids") or "").split(",") if l.strip()]
    if not list_ids:
        return 0

    import threading
    import processor
    from webhook_parser import MediaRequest

    cap = _settings.get("MDBLIST_AUTO_REQUEST_CAP", MDBLIST_AUTO_REQUEST_CAP)
    seen_movies = {r["imdb_id"] for r in db.get_recent(2000) if r.get("media_type") == "movie"}
    seen_series = {s["imdb_id"] for s in db.get_all_monitored_series()}

    added = 0
    for list_id in list_ids:
        if added >= cap:
            break
        for item in get_list_items(list_id, api_key):
            if added >= cap:
                break
            imdb_id = item["imdb_id"]
            already_seen = imdb_id in (seen_movies if item["media_type"] == "movie" else seen_series)
            if already_seen:
                continue
            seasons = [] if item["media_type"] == "movie" else [1]
            req = MediaRequest(title=item["title"], media_type=item["media_type"],
                                imdb_id=imdb_id, seasons=seasons, tmdb_id=item.get("tmdb_id"))
            threading.Thread(target=processor.process, args=(req,),
                             name=f"mdblist-sync-{imdb_id}", daemon=True).start()
            added += 1
            time.sleep(0.3)  # small stagger so a big synced list doesn't burst TorBox all at once
    return added
