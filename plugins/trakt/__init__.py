from flask import Blueprint
from . import routes as _routes

blueprint = Blueprint("trakt", __name__)
blueprint.register_blueprint(_routes.bp)

PLUGIN_META = {
    "label":       "Trakt",
    "version":     "1.1.0",
    "description": "Sync watchlist with Trakt.tv, auto-request new watchlist items "
                    "for download (capped), and scrobble watch history",
    "user_fields": [],
    # settings_ui drives the generic PluginSettingsCard renderer in the frontend.
    # No frontend code changes needed when adding a new plugin  -  just define this.
    "settings_ui": {
        "status_url": "/ui/api/trakt/status",
        # config_gate: show a warning if this field is falsy in the status response
        "config_gate": {
            "field":   "configured",
            "message": "Create a Trakt app at trakt.tv/oauth/applications, then add "
                       "TRAKT_CLIENT_ID and TRAKT_CLIENT_SECRET in Admin → Connections settings.",
            "link":    "https://trakt.tv/oauth/applications",
            "link_label": "trakt.tv/oauth/applications",
        },
        # oauth_device: standard device-auth flow  -  frontend handles all states
        "oauth_device": {
            "connected_field": "connected",
            "username_field":  "username",
            "synced_field":    "synced_at",
            "start_url":  "/ui/api/trakt/auth/start",
            "poll_url":   "/ui/api/trakt/auth/poll",
            "revoke_url": "/ui/api/trakt/auth/revoke",
        },
        # actions: buttons rendered when show_if field is truthy
        "actions": [
            {
                "label":            "↻ Sync watchlist",
                "url":              "/ui/api/trakt/sync",
                "method":           "POST",
                "show_if":          "connected",
                "success_key":      "added",
                "success_template": "✓ {added} items synced",
            },
            {
                "label":            "↻ Sync watched",
                "url":              "/ui/api/trakt/sync-watched",
                "method":           "POST",
                "show_if":          "connected",
                "success_key":      "watched",
                "success_template": "✓ {watched} titles synced",
            },
            {
                "label":            "⬇ Auto-request watchlist",
                "url":              "/ui/api/trakt/auto-request",
                "method":           "POST",
                "show_if":          "connected",
                "success_key":      "added",
                "success_template": "✓ {added} new items queued for download",
            },
        ],
    },
}


def run_migrations() -> None:
    import db
    with db._connect() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS trakt_tokens (
                user_id       INTEGER PRIMARY KEY,
                access_token  TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at    INTEGER NOT NULL,
                trakt_username TEXT,
                synced_at     TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS trakt_watched (
                user_id    INTEGER NOT NULL,
                imdb_id    TEXT    NOT NULL,
                tmdb_id    INTEGER,
                media_type TEXT    NOT NULL DEFAULT 'movie',
                watched_at TEXT,
                PRIMARY KEY (user_id, imdb_id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        # Add tmdb_id column if it doesn't exist yet (upgrade path)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(trakt_watched)")}
        if "tmdb_id" not in cols:
            c.execute("ALTER TABLE trakt_watched ADD COLUMN tmdb_id INTEGER")
        c.execute("""
            CREATE TABLE IF NOT EXISTS trakt_watched_episodes (
                user_id  INTEGER NOT NULL,
                imdb_id  TEXT    NOT NULL,
                season   INTEGER NOT NULL,
                episode  INTEGER NOT NULL,
                PRIMARY KEY (user_id, imdb_id, season, episode),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)


def session_data(user_record: dict) -> dict:
    user_id = user_record.get("id")
    if not user_id:
        return {"trakt_connected": False, "trakt_username": None}
    from . import trakt_api
    tok = trakt_api.get_token(user_id)
    return {
        "trakt_connected": tok is not None,
        "trakt_username":  tok["trakt_username"] if tok else None,
    }


def register_jobs(scheduler) -> None:
    from . import trakt_api
    scheduler.add_job(
        trakt_api.sync_all_users,
        "interval", minutes=30,
        id="trakt_sync",
        replace_existing=True,
    )
