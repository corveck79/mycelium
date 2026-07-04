import time

import auth
import settings
from flask import Blueprint, abort, jsonify, request
from . import trakt_api

bp = Blueprint("trakt_routes", __name__)

# In-memory pending device auth sessions: {user_id: {device_code, expires_at}}
_pending: dict[int, dict] = {}


def _require_user() -> dict:
    rec = auth.current_user_record()
    if not rec:
        abort(401)
    return rec


@bp.get("/ui/api/trakt/status")
def trakt_status():
    rec = _require_user()
    user_id = rec.get("id")
    if not user_id:
        return jsonify(connected=False, username=None, synced_at=None,
                       configured=bool(settings.get("TRAKT_CLIENT_ID")))
    tok = trakt_api.get_token(user_id)
    return jsonify(
        connected=tok is not None,
        username=tok["trakt_username"] if tok else None,
        synced_at=tok["synced_at"] if tok else None,
        configured=bool(settings.get("TRAKT_CLIENT_ID")),
    )


@bp.post("/ui/api/trakt/auth/start")
def trakt_auth_start():
    if not settings.get("TRAKT_CLIENT_ID"):
        return jsonify(error="TRAKT_CLIENT_ID not configured  -  set it via admin settings"), 400
    rec = _require_user()
    user_id = rec.get("id")
    if not user_id:
        abort(401)
    try:
        data = trakt_api.start_device_auth()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    _pending[user_id] = {
        "device_code": data["device_code"],
        "expires_at":  time.time() + data.get("expires_in", 600),
    }
    return jsonify(
        user_code=data["user_code"],
        verification_url=data["verification_url"],
        expires_in=data.get("expires_in", 600),
        interval=data.get("interval", 5),
    )


@bp.get("/ui/api/trakt/auth/poll")
def trakt_auth_poll():
    rec = _require_user()
    user_id = rec.get("id")
    if not user_id:
        abort(401)
    pending = _pending.get(user_id)
    if not pending:
        return jsonify(status="no_pending")
    if time.time() > pending["expires_at"]:
        _pending.pop(user_id, None)
        return jsonify(status="expired")
    try:
        token_data = trakt_api.poll_device_auth(pending["device_code"])
    except ValueError as exc:
        _pending.pop(user_id, None)
        return jsonify(status="error", error=str(exc))
    if not token_data:
        return jsonify(status="pending")
    access_token = token_data["access_token"]
    profile = trakt_api.get_me(access_token)
    username = profile.get("username") if profile else None
    trakt_api.save_token(
        user_id,
        access_token,
        token_data["refresh_token"],
        int(time.time()) + token_data["expires_in"],
        username,
    )
    _pending.pop(user_id, None)
    return jsonify(status="connected", username=username)


@bp.post("/ui/api/trakt/auth/revoke")
def trakt_auth_revoke():
    rec = _require_user()
    user_id = rec.get("id")
    if not user_id:
        abort(401)
    tok = trakt_api.get_token(user_id)
    if tok:
        trakt_api.revoke_token(tok["access_token"])
        trakt_api.delete_token(user_id)
    _pending.pop(user_id, None)
    return jsonify(ok=True)


@bp.get("/ui/api/trakt/watched")
def trakt_watched():
    rec = _require_user()
    user_id = rec.get("id")
    if not user_id:
        return jsonify(imdb_ids=[])
    tok = trakt_api.get_token(user_id)
    if not tok:
        return jsonify(imdb_ids=[])
    return jsonify(imdb_ids=trakt_api.get_watched_imdb_ids(user_id))



@bp.get("/ui/api/trakt/watched-episodes")
def trakt_watched_episodes():
    rec = _require_user()
    user_id = rec.get("id")
    if not user_id:
        return jsonify(shows={})
    tok = trakt_api.get_token(user_id)
    if not tok:
        return jsonify(shows={})
    return jsonify(shows=trakt_api.get_watched_episodes(user_id))


@bp.post("/ui/api/trakt/sync")
def trakt_sync():
    rec = _require_user()
    user_id = rec.get("id")
    if not user_id:
        abort(401)
    tok = trakt_api.get_token(user_id)
    if not tok:
        return jsonify(error="Not connected to Trakt"), 400
    tok = trakt_api.refresh_if_needed(tok)
    added = trakt_api.sync_user_watchlist(user_id, tok["access_token"])
    return jsonify(ok=True, added=added)


@bp.post("/ui/api/trakt/sync-watched")
def trakt_sync_watched():
    rec = _require_user()
    user_id = rec.get("id")
    if not user_id:
        abort(401)
    tok = trakt_api.get_token(user_id)
    if not tok:
        return jsonify(error="Not connected to Trakt"), 400
    tok = trakt_api.refresh_if_needed(tok)
    watched = trakt_api.sync_user_watched(user_id, tok["access_token"])
    return jsonify(ok=True, watched=watched)


@bp.post("/ui/api/trakt/auto-request")
def trakt_auto_request():
    """Queue new watchlist items for download (capped via TRAKT_AUTO_REQUEST_CAP),
    on top of the plain watchlist sync above which only populates the browsing list."""
    rec = _require_user()
    user_id = rec.get("id")
    if not user_id:
        abort(401)
    tok = trakt_api.get_token(user_id)
    if not tok:
        return jsonify(error="Not connected to Trakt"), 400
    tok = trakt_api.refresh_if_needed(tok)
    added = trakt_api.auto_request_watchlist(user_id, tok["access_token"])
    return jsonify(ok=True, added=added)


@bp.post("/ui/api/trakt/scrobble")
def trakt_scrobble_endpoint():
    rec = _require_user()
    user_id = rec.get("id")
    if not user_id:
        abort(401)
    tok = trakt_api.get_token(user_id)
    if not tok:
        return jsonify(ok=False, reason="not_connected")
    d = request.json or {}
    ok = trakt_api.scrobble(
        access_token=tok["access_token"],
        action=d.get("action", "stop"),
        media_type=d.get("media_type", "movie"),
        imdb_id=d["imdb_id"],
        progress=float(d.get("progress", 0)),
        season=d.get("season"),
        episode=d.get("episode"),
        title=d.get("title"),
    )
    return jsonify(ok=ok)
