import logging
import sqlite3
import threading
from contextlib import contextmanager

from config import DB_PATH

log = logging.getLogger(__name__)

# Thread-local connection cache: one open SQLite handle per thread, reused for
# the lifetime of the thread. Eliminates the open/close churn on every query
# under heavy load (dashboard polling + scheduler + webhooks running together).
_tls = threading.local()


def _raw_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _thread_conn() -> sqlite3.Connection:
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = _raw_connect()
        _tls.conn = conn
    return conn


@contextmanager
def _connect():
    """Yield a per-thread sqlite3 connection. We deliberately do NOT close it
    on exit; the connection lives for the thread's lifetime."""
    conn = _thread_conn()
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

_DDL = """
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    imdb_id     TEXT    NOT NULL UNIQUE,
    media_type  TEXT    NOT NULL,
    seasons     TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending',
    quality     TEXT,
    source      TEXT,
    info_hash   TEXT,
    error       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS monitored_series (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id      TEXT    NOT NULL UNIQUE,
    tmdb_id      INTEGER,
    title        TEXT    NOT NULL,
    seasons      TEXT,
    status       TEXT    NOT NULL DEFAULT 'active',
    last_checked TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS wanted_episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id         TEXT    NOT NULL,
    tmdb_id         INTEGER,
    title           TEXT    NOT NULL,
    season          INTEGER NOT NULL,
    episode         INTEGER NOT NULL,
    air_date        TEXT,
    status          TEXT    NOT NULL DEFAULT 'wanted',
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    first_attempted TEXT,
    last_attempted  TEXT,
    UNIQUE(imdb_id, season, episode)
);

CREATE TABLE IF NOT EXISTS media_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id          TEXT    NOT NULL,
    title            TEXT    NOT NULL,
    media_type       TEXT    NOT NULL DEFAULT 'movie',
    seerr_request_id INTEGER,
    requested_by     TEXT,
    requested_at     TEXT,
    status           TEXT    NOT NULL DEFAULT 'pending',
    strm_found       INTEGER NOT NULL DEFAULT 0,
    last_checked     TEXT,
    UNIQUE(imdb_id, media_type)
);

CREATE TABLE IF NOT EXISTS cleanup_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    scanned     INTEGER NOT NULL DEFAULT 0,
    repaired    INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    unfixable   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event       TEXT    NOT NULL,
    title       TEXT,
    message     TEXT,
    success     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS poster_cache (
    imdb_id     TEXT    PRIMARY KEY,
    poster_path TEXT,
    cached_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS virtual_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT    NOT NULL UNIQUE,
    info_hash    TEXT    NOT NULL,
    magnet       TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    media_type   TEXT    NOT NULL,
    strm_path    TEXT,
    torbox_id    INTEGER,
    file_id      INTEGER,
    last_played  TEXT,
    play_count   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS failed_hashes (
    info_hash      TEXT    PRIMARY KEY,
    fail_count     INTEGER NOT NULL DEFAULT 1,
    last_error     TEXT,
    last_attempt   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key       TEXT    NOT NULL UNIQUE,
    received_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS retry_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id         TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    media_type      TEXT    NOT NULL,
    seasons         TEXT,
    attempt         INTEGER NOT NULL DEFAULT 0,
    next_retry_at   TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS show_quality_override (
    imdb_id              TEXT    PRIMARY KEY,
    quality_preference   TEXT,
    allow_4k             INTEGER,
    prefer_hevc          INTEGER,
    notes                TEXT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS metric_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    metric        TEXT    NOT NULL,
    label         TEXT,
    value_int     INTEGER,
    value_real    REAL,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT    PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS repair_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cleanup_run_id  INTEGER NOT NULL REFERENCES cleanup_runs(id),
    path            TEXT    NOT NULL,
    title           TEXT,
    media_type      TEXT,
    old_torrent_id  TEXT,
    new_info_hash   TEXT,
    status          TEXT    NOT NULL DEFAULT 'unknown',
    reason          TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_imdb_unique ON requests(imdb_id);
CREATE INDEX IF NOT EXISTS idx_requests_status_created    ON requests(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_monitored_series_status    ON monitored_series(status);
CREATE INDEX IF NOT EXISTS idx_wanted_status_attempts     ON wanted_episodes(status, attempt_count);
CREATE INDEX IF NOT EXISTS idx_wanted_imdb                ON wanted_episodes(imdb_id);
CREATE INDEX IF NOT EXISTS idx_media_items_status         ON media_items(status);
CREATE INDEX IF NOT EXISTS idx_failed_hashes_failcount    ON failed_hashes(fail_count);
CREATE INDEX IF NOT EXISTS idx_virtual_items_torbox       ON virtual_items(torbox_id);
CREATE INDEX IF NOT EXISTS idx_virtual_items_lastplayed   ON virtual_items(last_played);
CREATE INDEX IF NOT EXISTS idx_metric_events_metric_time  ON metric_events(metric, created_at);
CREATE INDEX IF NOT EXISTS idx_metric_events_created      ON metric_events(created_at);
CREATE INDEX IF NOT EXISTS idx_activity_log_created       ON activity_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_events_received    ON webhook_events(received_at);
CREATE INDEX IF NOT EXISTS idx_retry_queue_next           ON retry_queue(next_retry_at);
CREATE INDEX IF NOT EXISTS idx_repair_items_run           ON repair_items(cleanup_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user',
    quota_monthly INTEGER NOT NULL DEFAULT 0,
    auto_approve  INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    imdb_id     TEXT    NOT NULL,
    tmdb_id     INTEGER,
    media_type  TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    poster_path TEXT,
    added_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    UNIQUE(user_id, imdb_id, media_type)
);

CREATE TABLE IF NOT EXISTS user_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    imdb_id      TEXT    NOT NULL,
    tmdb_id      INTEGER,
    media_type   TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    seasons      TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',
    reviewed_by  INTEGER,
    reviewed_at  TEXT,
    note         TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_users_username             ON users(username);
CREATE INDEX IF NOT EXISTS idx_watchlist_user             ON watchlist(user_id);
CREATE INDEX IF NOT EXISTS idx_user_requests_user_status  ON user_requests(user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_requests_status       ON user_requests(status, created_at DESC);

CREATE TABLE IF NOT EXISTS wanted_movies (
    imdb_id      TEXT    PRIMARY KEY,
    tmdb_id      INTEGER,
    title        TEXT    NOT NULL,
    reason       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    added_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    last_checked TEXT
);

CREATE TABLE IF NOT EXISTS playability_state (
    content_key          TEXT    PRIMARY KEY,
    status               TEXT    NOT NULL DEFAULT 'unknown',
    last_ok_provider     TEXT,
    last_ok_at           TEXT,
    last_fail_reason     TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_playability_status ON playability_state(status, updated_at);

CREATE TABLE IF NOT EXISTS createtorrent_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    reason    TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_createtorrent_ts ON createtorrent_log(ts);
"""


def init() -> None:
    # PRAGMAs are applied in _raw_connect() on every new thread-local connection.
    with _connect() as conn:
        for stmt in _DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        _dedup_requests(conn)
        conn.commit()
    _migrate()
    integrity_check()


def _dedup_requests(conn) -> None:
    """Remove duplicate imdb_id rows before the UNIQUE index is created."""
    dupes = conn.execute(
        "SELECT imdb_id, COUNT(*) AS cnt FROM requests "
        "GROUP BY imdb_id HAVING cnt > 1"
    ).fetchall()
    if not dupes:
        return
    for row in dupes:
        ids = conn.execute(
            "SELECT id FROM requests WHERE imdb_id=? ORDER BY created_at DESC",
            (row["imdb_id"],),
        ).fetchall()
        keep = ids[0]["id"]
        conn.execute(
            "DELETE FROM requests WHERE imdb_id=? AND id!=?",
            (row["imdb_id"], keep),
        )
    conn.commit()
    log.info("Dedup: removed duplicates for %d imdb_id(s) in requests", len(dupes))


def _migrate() -> None:
    """Lightweight additive migrations for columns added after first release."""
    with _connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(monitored_series)")}
        if "monitor_mode" not in cols:
            conn.execute("ALTER TABLE monitored_series ADD COLUMN monitor_mode TEXT NOT NULL DEFAULT 'all'")
            log.info("Migration: added monitored_series.monitor_mode")
        if "added_at_date" not in cols:
            conn.execute("ALTER TABLE monitored_series ADD COLUMN added_at_date TEXT")
            log.info("Migration: added monitored_series.added_at_date")

        vi_cols = {r["name"] for r in conn.execute("PRAGMA table_info(virtual_items)")}
        for col, typedef in [
            ("imdb_id", "TEXT"),
            ("quality", "TEXT"),
            ("source", "TEXT"),
            ("size_gb", "REAL"),
            ("season", "INTEGER"),
            ("episode", "INTEGER"),
            ("year", "INTEGER"),
            ("debrid_provider", "TEXT DEFAULT 'torbox'"),
            ("rd_id", "TEXT"),
            ("spore_tracks", "TEXT"),
        ]:
            if col not in vi_cols:
                try:
                    conn.execute(f"ALTER TABLE virtual_items ADD COLUMN {col} {typedef}")
                    log.info("Migration: added virtual_items.%s", col)
                except Exception as _e:
                    log.warning("Migration: could not add virtual_items.%s: %s", col, _e)

        req_cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
        if "tmdb_id" not in req_cols:
            conn.execute("ALTER TABLE requests ADD COLUMN tmdb_id INTEGER")
            log.info("Migration: added requests.tmdb_id")
            conn.execute("""
                UPDATE requests SET tmdb_id = (
                    SELECT COALESCE(w.tmdb_id, ms.tmdb_id, ur.tmdb_id)
                    FROM requests r2
                    LEFT JOIN watchlist w ON w.imdb_id = r2.imdb_id AND w.tmdb_id IS NOT NULL
                    LEFT JOIN monitored_series ms ON ms.imdb_id = r2.imdb_id AND ms.tmdb_id IS NOT NULL
                    LEFT JOIN user_requests ur ON ur.imdb_id = r2.imdb_id AND ur.tmdb_id IS NOT NULL
                    WHERE r2.id = requests.id
                    LIMIT 1
                ) WHERE tmdb_id IS NULL
            """)
            log.info("Migration: backfilled requests.tmdb_id from related tables")

        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "region" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN region TEXT NOT NULL DEFAULT 'NL'")
            log.info("Migration: added users.region")
        if "library_click_jellyfin" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN library_click_jellyfin INTEGER NOT NULL DEFAULT 0")
            log.info("Migration: added users.library_click_jellyfin")
        for col in ("trakt_username", "trakt_access_token", "trakt_refresh_token"):
            if col not in user_cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
                log.info("Migration: added users.%s", col)
        if "trakt_token_expires" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN trakt_token_expires REAL")
            log.info("Migration: added users.trakt_token_expires")
        for col in ("discover_language_include", "discover_language_exclude"):
            if col not in user_cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                log.info("Migration: added users.%s", col)
        for col in ("mdblist_api_key", "mdblist_list_ids"):
            if col not in user_cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                log.info("Migration: added users.%s", col)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS trakt_watched (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tmdb_id     INTEGER NOT NULL,
                imdb_id     TEXT,
                media_type  TEXT    NOT NULL,
                season      INTEGER,
                episode     INTEGER,
                watched_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
                UNIQUE(user_id, tmdb_id, media_type, season, episode)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS favorite_actors (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                person_id   INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                profile_path TEXT,
                added_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
                UNIQUE(user_id, person_id)
            )
        """)

        conn.commit()


def integrity_check() -> bool:
    """Run SQLite integrity_check. Logs warnings on failure, returns True if OK."""
    try:
        with _connect() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            ok = bool(row) and (row[0] == "ok" or row["integrity_check"] == "ok")
        if ok:
            log.info("DB integrity: ok")
        else:
            log.error("DB integrity: %s", row)
        return ok
    except Exception as exc:
        log.error("DB integrity check failed: %s", exc)
        return False


def vacuum() -> None:
    try:
        with _connect() as conn:
            conn.execute("VACUUM")
        log.info("DB vacuum: done")
    except Exception as exc:
        log.warning("DB vacuum failed: %s", exc)


_PRUNE_TARGETS: dict[str, str] = {
    # table_name : timestamp_column. Whitelist so the table name never comes from input.
    "activity_log":     "created_at",
    "webhook_events":   "received_at",
    "metric_events":    "created_at",
    "playability_state": "updated_at",
}


def prune_old(days: int = 90) -> dict[str, int]:
    """Delete rows in volatile tables older than N days. Returns count per table.
    Table names come from a hardcoded whitelist; only the day count is parameterized."""
    if not isinstance(days, int) or days < 0:
        raise ValueError("days must be a non-negative int")
    out: dict[str, int] = {}
    cutoff_modifier = f"-{days} days"
    with _connect() as conn:
        for tbl, ts_col in _PRUNE_TARGETS.items():
            try:
                cur = conn.execute(
                    f"DELETE FROM {tbl} WHERE {ts_col} < datetime('now', ?)",
                    (cutoff_modifier,),
                )
                out[tbl] = cur.rowcount or 0
            except Exception as exc:
                log.debug("prune %s failed: %s", tbl, exc)
                out[tbl] = 0
        conn.commit()
    total = sum(out.values())
    if total:
        log.info("Pruned %d old row(s): %s", total, out)
    return out


# ── requests ──────────────────────────────────────────────────────────────────

def insert_request(title: str, imdb_id: str, media_type: str, seasons: list[int] | None = None,
                    tmdb_id: int | None = None) -> int:
    seasons_str = ",".join(str(s) for s in (seasons or []))
    with _connect() as conn:
        # cursor.lastrowid is unreliable here: on the ON CONFLICT/UPDATE path
        # (i.e. every retry of an existing imdb_id) SQLite does NOT update
        # last_insert_rowid(), so it can return a stale id left over from some
        # unrelated row's last real INSERT on this connection. Look the row up
        # explicitly instead of trusting lastrowid.
        conn.execute(
            "INSERT INTO requests (title, imdb_id, media_type, seasons, tmdb_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(imdb_id) DO UPDATE SET "
            "title=excluded.title, seasons=COALESCE(excluded.seasons, seasons), "
            "tmdb_id=COALESCE(excluded.tmdb_id, tmdb_id), "
            "updated_at=strftime('%Y-%m-%d %H:%M:%S', 'now')",
            (title, imdb_id, media_type, seasons_str or None, tmdb_id),
        )
        row = conn.execute("SELECT id FROM requests WHERE imdb_id=?", (imdb_id,)).fetchone()
        conn.commit()
        return row["id"]


def update_request(row_id: int, status: str, quality: str | None = None,
                   source: str | None = None, info_hash: str | None = None,
                   error: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE requests SET status=?, quality=?, source=?, info_hash=?, error=?,
               updated_at=strftime('%Y-%m-%d %H:%M:%S','now') WHERE id=?""",
            (status, quality, source, info_hash, error, row_id),
        )
        conn.commit()


def get_request_by_imdb(imdb_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM requests WHERE imdb_id=? ORDER BY created_at DESC LIMIT 1",
            (imdb_id,)
        ).fetchone()
        return dict(row) if row else None


def get_recent(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def reconcile_wanted_movies() -> int:
    """Mark wanted movies as success if they already have a virtual_item (strm)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE requests SET status='success' "
            "WHERE status='wanted' AND media_type='movie' "
            "AND EXISTS (SELECT 1 FROM virtual_items v "
            "WHERE v.imdb_id=requests.imdb_id AND v.media_type='movie')"
        )
        conn.commit()
        return cur.rowcount


def reconcile_wanted_episodes() -> int:
    """Mark wanted episodes as found if a matching strm file exists in virtual_items."""
    import re as _re
    _EP_RE = _re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})')
    with _connect() as conn:
        vis = conn.execute(
            "SELECT imdb_id, strm_path FROM virtual_items "
            "WHERE media_type='series' AND strm_path IS NOT NULL AND imdb_id IS NOT NULL"
        ).fetchall()
        have: set[tuple[str, int, int]] = set()
        for v in vis:
            m = _EP_RE.search(v["strm_path"] or "")
            if m:
                have.add((v["imdb_id"], int(m.group(1)), int(m.group(2))))
        if not have:
            return 0
        updated = 0
        for imdb_id, season, episode in have:
            cur = conn.execute(
                "UPDATE wanted_episodes SET status='found' "
                "WHERE imdb_id=? AND season=? AND episode=? AND status='wanted'",
                (imdb_id, season, episode),
            )
            updated += cur.rowcount
        conn.commit()
        return updated


# ── monitored_series ──────────────────────────────────────────────────────────

def upsert_monitored_series(imdb_id: str, tmdb_id: int | None, title: str,
                            seasons: list[int], monitor_mode: str = "all") -> None:
    seasons_str = ",".join(str(s) for s in seasons)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO monitored_series (imdb_id, tmdb_id, title, seasons, monitor_mode, added_at_date)
               VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%d','now'))
               ON CONFLICT(imdb_id) DO UPDATE SET
                 tmdb_id=COALESCE(excluded.tmdb_id, tmdb_id),
                 title=excluded.title,
                 seasons=excluded.seasons,
                 monitor_mode=excluded.monitor_mode,
                 status='active'""",
            (imdb_id, tmdb_id, title, seasons_str, monitor_mode),
        )
        conn.commit()


def get_monitored_series(status: str = "active") -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM monitored_series WHERE status=? ORDER BY title", (status,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_monitored_series() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM monitored_series ORDER BY title").fetchall()
        return [dict(r) for r in rows]


def update_monitored_series(series_id: int, tmdb_id: int | None = None,
                             seasons: list[int] | None = None) -> None:
    with _connect() as conn:
        if tmdb_id is not None:
            conn.execute("UPDATE monitored_series SET tmdb_id=? WHERE id=?", (tmdb_id, series_id))
        if seasons is not None:
            conn.execute("UPDATE monitored_series SET seasons=? WHERE id=?",
                         (",".join(str(s) for s in seasons), series_id))
        conn.execute("UPDATE monitored_series SET last_checked=strftime('%Y-%m-%d %H:%M:%S','now') WHERE id=?",
                     (series_id,))
        conn.commit()


# ── wanted_episodes ───────────────────────────────────────────────────────────

def upsert_wanted_episode(imdb_id: str, tmdb_id: int | None, title: str,
                           season: int, episode: int, air_date: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO wanted_episodes (imdb_id, tmdb_id, title, season, episode, air_date)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(imdb_id, season, episode) DO UPDATE SET
                 air_date=COALESCE(excluded.air_date, air_date),
                 status=CASE WHEN status='found' THEN 'found' ELSE status END""",
            (imdb_id, tmdb_id, title, season, episode, air_date),
        )
        conn.commit()


def get_wanted_episodes(max_attempts: int = 10) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM wanted_episodes
               WHERE status='wanted' AND attempt_count < ?
               ORDER BY title, season, episode""",
            (max_attempts,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_series_folder_imdb_map() -> dict[str, str]:
    """Map series folder names to imdb_id via virtual_items strm_path."""
    import re
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT imdb_id, strm_path FROM virtual_items "
            "WHERE media_type='series' AND imdb_id IS NOT NULL AND strm_path IS NOT NULL"
        ).fetchall()
    folder_map: dict[str, str] = {}
    for r in rows:
        parts = r["strm_path"].replace("\\", "/").split("/")
        for i, p in enumerate(parts):
            if p == "series" and i + 1 < len(parts):
                folder_map[parts[i + 1].lower()] = r["imdb_id"]
                break
    return folder_map


def get_all_wanted_episodes() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM wanted_episodes ORDER BY title, season, episode"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_episode_status(imdb_id: str, season: int, episode: int, status: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE wanted_episodes SET status=? WHERE imdb_id=? AND season=? AND episode=?",
            (status, imdb_id, season, episode),
        )
        conn.commit()


def increment_episode_attempt(episode_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE wanted_episodes SET
               attempt_count = attempt_count + 1,
               last_attempted = strftime('%Y-%m-%d %H:%M:%S','now'),
               first_attempted = COALESCE(first_attempted, strftime('%Y-%m-%d %H:%M:%S','now'))
               WHERE id=?""",
            (episode_id,),
        )
        conn.commit()


# ── media_items ───────────────────────────────────────────────────────────────

def upsert_media_item(imdb_id: str, title: str, media_type: str,
                       seerr_request_id: int | None = None,
                       requested_by: str | None = None,
                       requested_at: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO media_items (imdb_id, title, media_type, seerr_request_id,
                                        requested_by, requested_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(imdb_id, media_type) DO UPDATE SET
                 title=excluded.title,
                 seerr_request_id=COALESCE(excluded.seerr_request_id, seerr_request_id),
                 requested_by=COALESCE(excluded.requested_by, requested_by),
                 requested_at=COALESCE(excluded.requested_at, requested_at)""",
            (imdb_id, title, media_type, seerr_request_id, requested_by, requested_at),
        )
        conn.commit()


def update_media_item_status(imdb_id: str, media_type: str,
                              status: str, strm_found: bool = False) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE media_items SET status=?, strm_found=?,
               last_checked=strftime('%Y-%m-%d %H:%M:%S','now')
               WHERE imdb_id=? AND media_type=?""",
            (status, int(strm_found), imdb_id, media_type),
        )
        conn.commit()


def get_media_items(media_type: str | None = None) -> list[dict]:
    with _connect() as conn:
        if media_type:
            rows = conn.execute(
                "SELECT * FROM media_items WHERE media_type=? ORDER BY requested_at DESC",
                (media_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM media_items ORDER BY requested_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_unknown_media_items() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM media_items WHERE imdb_id LIKE 'unknown_%'"
        ).fetchall()
        return [dict(r) for r in rows]


def rekey_media_item(old_id: str, new_id: str, media_type: str) -> bool:
    with _connect() as conn:
        try:
            cur = conn.execute(
                "UPDATE media_items SET imdb_id=? WHERE imdb_id=? AND media_type=?",
                (new_id, old_id, media_type),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            # new_id already exists (UNIQUE conflict)  -  the unknown_ row is a duplicate;
            # just delete it so the canonical entry remains.
            try:
                conn.execute(
                    "DELETE FROM media_items WHERE imdb_id=? AND media_type=?",
                    (old_id, media_type),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                return False


# ── cleanup_runs ──────────────────────────────────────────────────────────────

def insert_cleanup_run() -> int:
    with _connect() as conn:
        cur = conn.execute("INSERT INTO cleanup_runs DEFAULT VALUES")
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def update_cleanup_run(run_id: int, scanned: int, repaired: int,
                        deleted: int, unfixable: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE cleanup_runs SET scanned=?, repaired=?, deleted=?, unfixable=? WHERE id=?",
            (scanned, repaired, deleted, unfixable, run_id),
        )
        conn.commit()


def get_last_cleanup_run() -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM cleanup_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


# ── repair_items ──────────────────────────────────────────────────────────────

def insert_repair_item(run_id: int, path: str, title: str | None, media_type: str | None,
                        old_torrent_id: str | None, new_info_hash: str | None,
                        status: str, reason: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO repair_items
               (cleanup_run_id, path, title, media_type, old_torrent_id, new_info_hash, status, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, path, title, media_type, old_torrent_id, new_info_hash, status, reason),
        )
        conn.commit()


# ── activity_log ──────────────────────────────────────────────────────────────

def log_activity(event: str, title: str | None = None, message: str | None = None,
                  success: bool = True) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO activity_log (event, title, message, success) VALUES (?, ?, ?, ?)",
            (event, title, message, int(success)),
        )
        conn.commit()


def get_activity(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── poster_cache ──────────────────────────────────────────────────────────────

def get_poster(imdb_id: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT poster_path FROM poster_cache WHERE imdb_id=?", (imdb_id,)
        ).fetchone()
        return row["poster_path"] if row else None


def set_poster(imdb_id: str, poster_path: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO poster_cache (imdb_id, poster_path) VALUES (?, ?)
               ON CONFLICT(imdb_id) DO UPDATE SET poster_path=excluded.poster_path,
                  cached_at=strftime('%Y-%m-%d %H:%M:%S','now')""",
            (imdb_id, poster_path),
        )
        conn.commit()


def get_posters_batch(imdb_ids: list[str]) -> dict[str, str | None]:
    """Return poster_path for each imdb_id from the poster_cache, as a dict."""
    if not imdb_ids:
        return {}
    ph = ",".join("?" * len(imdb_ids))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT imdb_id, poster_path FROM poster_cache WHERE imdb_id IN ({ph})",
            imdb_ids,
        ).fetchall()
    return {r["imdb_id"]: r["poster_path"] for r in rows}


# ── virtual_items (Catbox mode) ───────────────────────────────────────────────

def insert_virtual_item(token: str, info_hash: str, magnet: str, title: str,
                         media_type: str, strm_path: str | None = None,
                         torbox_id: int | None = None, file_id: int | None = None,
                         imdb_id: str | None = None, quality: str | None = None,
                         source: str | None = None, size_gb: float | None = None,
                         season: int | None = None, episode: int | None = None,
                         year: int | None = None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO virtual_items
               (token, info_hash, magnet, title, media_type, strm_path, torbox_id, file_id,
                imdb_id, quality, source, size_gb, season, episode, year)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (token, info_hash, magnet, title, media_type, strm_path, torbox_id, file_id,
             imdb_id, quality, source, size_gb, season, episode, year),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def update_virtual_item_upgrade(token: str, info_hash: str, magnet: str,
                                 quality: str | None, source: str | None) -> None:
    """Swap in a better torrent for an existing virtual item (upgrade). Clears the
    cached torbox_id and file_id so next playback re-materializes with new hash."""
    with _connect() as conn:
        conn.execute(
            """UPDATE virtual_items
               SET info_hash=?, magnet=?, quality=?, source=?,
                   torbox_id=NULL, file_id=NULL
               WHERE token=?""",
            (info_hash, magnet, quality, source, token),
        )
        conn.commit()


def get_upgradeable_virtual_items() -> list[dict]:
    """Return movie virtual items that have a stored quality below 2160p."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM virtual_items
               WHERE imdb_id IS NOT NULL AND media_type='movie'
               ORDER BY created_at DESC"""
        ).fetchall()
    ranks = {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1}
    return [dict(r) for r in rows if ranks.get((r["quality"] or "?"), 0) < 4]


def get_virtual_item(token: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM virtual_items WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None


def get_virtual_item_by_hash(info_hash: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM virtual_items WHERE info_hash=?", (info_hash.lower(),)
        ).fetchone()
        return dict(row) if row else None


def get_virtual_items_by_hash(info_hash: str) -> list[dict]:
    """Return ALL virtual_items with this info_hash (movies: 1, season packs: N episodes)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM virtual_items WHERE info_hash=?", (info_hash.lower(),)
        ).fetchall()
        return [dict(r) for r in rows]


def get_unprobed_spore_items() -> list[dict]:
    """Return virtual_items that have a strm_path but no spore_tracks yet."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM virtual_items WHERE strm_path IS NOT NULL AND spore_tracks IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_virtual_items() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM virtual_items ORDER BY last_played DESC, created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_virtual_items_by_imdb(imdb_id: str, media_type: str | None = None) -> list[dict]:
    with _connect() as conn:
        if media_type:
            rows = conn.execute(
                "SELECT * FROM virtual_items WHERE imdb_id=? AND media_type=? ORDER BY created_at DESC",
                (imdb_id, media_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM virtual_items WHERE imdb_id=? ORDER BY created_at DESC",
                (imdb_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_virtual_item_by_episode(imdb_id: str, season: int, episode: int) -> dict | None:
    """Return the virtual_item for a specific series episode, or None if not registered."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM virtual_items WHERE imdb_id=? AND season=? AND episode=? LIMIT 1",
            (imdb_id, season, episode),
        ).fetchone()
        return dict(row) if row else None


def update_virtual_torbox_id(token: str, torbox_id: int | None) -> None:
    with _connect() as conn:
        conn.execute("UPDATE virtual_items SET torbox_id=? WHERE token=?", (torbox_id, token))
        conn.commit()


def update_virtual_file_id(token: str, file_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE virtual_items SET file_id=? WHERE token=?", (file_id, token))
        conn.commit()


def update_virtual_item_imdb(token: str, imdb_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE virtual_items SET imdb_id=? WHERE token=?", (imdb_id, token))
        conn.commit()


def update_virtual_debrid_provider(token: str, provider: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE virtual_items SET debrid_provider=? WHERE token=?", (provider, token))
        conn.commit()


def update_virtual_rd_id(token: str, rd_id: str | None) -> None:
    with _connect() as conn:
        conn.execute("UPDATE virtual_items SET rd_id=? WHERE token=?", (rd_id, token))
        conn.commit()


def update_virtual_strm_path_prefix(old_prefix: str, new_prefix: str) -> int:
    """Update strm_path for all virtual_items whose path starts with old_prefix."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT token, strm_path FROM virtual_items WHERE strm_path LIKE ?",
            (old_prefix + "%",),
        ).fetchall()
        count = 0
        for row in rows:
            new_path = new_prefix + row["strm_path"][len(old_prefix):]
            conn.execute("UPDATE virtual_items SET strm_path=? WHERE token=?",
                         (new_path, row["token"]))
            count += 1
        conn.commit()
        return count


def save_spore_tracks(token: str, tracks: dict) -> None:
    """Persist audio/subtitle track info from ffprobe so stub regeneration can reuse it."""
    import json as _json
    with _connect() as conn:
        conn.execute(
            "UPDATE virtual_items SET spore_tracks=? WHERE token=?",
            (_json.dumps(tracks), token),
        )
        conn.commit()


def load_spore_tracks(token: str) -> dict | None:
    """Return stored probe track info, or None if not yet probed."""
    import json as _json
    with _connect() as conn:
        row = conn.execute(
            "SELECT spore_tracks FROM virtual_items WHERE token=?", (token,)
        ).fetchone()
    if row and row["spore_tracks"]:
        try:
            return _json.loads(row["spore_tracks"])
        except Exception:
            return None
    return None


def touch_virtual_item(token: str) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE virtual_items SET
               last_played=strftime('%Y-%m-%d %H:%M:%S','now'),
               play_count=play_count+1 WHERE token=?""",
            (token,),
        )
        conn.commit()


def get_idle_virtual_items(cutoff_iso: str) -> list[dict]:
    """Items with a torbox_id and either last_played < cutoff or never played + created < cutoff."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM virtual_items
               WHERE torbox_id IS NOT NULL
                 AND (
                   (last_played IS NOT NULL AND last_played < ?)
                   OR (last_played IS NULL AND created_at < ?)
                 )""",
            (cutoff_iso, cutoff_iso),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_virtual_item(token: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM virtual_items WHERE token=?", (token,))
        conn.commit()


def delete_virtual_item_by_strm_path(strm_path: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM virtual_items WHERE strm_path=?", (strm_path,))
        conn.commit()


def rename_virtual_item_paths(old_dir: str, new_dir: str) -> int:
    """Bulk-update strm_path in virtual_items when a folder is renamed.
    Replaces the old directory prefix with the new one. Returns rows updated."""
    old_prefix = old_dir.rstrip("/") + "/"
    new_prefix = new_dir.rstrip("/") + "/"
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE virtual_items
               SET strm_path = ? || SUBSTR(strm_path, ?)
               WHERE strm_path LIKE ?""",
            (new_prefix, len(old_prefix) + 1, old_prefix + "%"),
        )
        conn.commit()
        return cur.rowcount


# ── failed_hashes (blacklist) ─────────────────────────────────────────────────

def record_failed_hash(info_hash: str, error: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO failed_hashes (info_hash, last_error) VALUES (?, ?)
               ON CONFLICT(info_hash) DO UPDATE SET
                 fail_count=fail_count+1,
                 last_error=COALESCE(excluded.last_error, last_error),
                 last_attempt=strftime('%Y-%m-%d %H:%M:%S','now')""",
            (info_hash, error),
        )
        conn.commit()


def get_failed_hash(info_hash: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM failed_hashes WHERE info_hash=?", (info_hash,)
        ).fetchone()
        return dict(row) if row else None


def get_blacklisted_hashes(threshold: int = 3) -> set[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT info_hash FROM failed_hashes WHERE fail_count >= ?", (threshold,)
        ).fetchall()
        return {r["info_hash"] for r in rows}


def get_all_failed_hashes() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM failed_hashes ORDER BY fail_count DESC, last_attempt DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def clear_failed_hash(info_hash: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM failed_hashes WHERE info_hash=?", (info_hash,))
        conn.commit()


# ── webhook idempotency ───────────────────────────────────────────────────────

def webhook_seen(dedup_key: str) -> bool:
    """Record a webhook event; return True if already seen (within DB)."""
    try:
        with _connect() as conn:
            conn.execute("INSERT INTO webhook_events (dedup_key) VALUES (?)", (dedup_key,))
            conn.commit()
            return False
    except Exception:
        return True


def prune_webhook_events(max_age_hours: int = 24) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM webhook_events WHERE received_at < datetime('now', ?)",
            (f"-{int(max_age_hours)} hours",)
        )
        conn.commit()
        return cur.rowcount or 0


# ── retry queue ───────────────────────────────────────────────────────────────

def enqueue_retry(imdb_id: str, title: str, media_type: str, seasons: list[int] | None,
                   attempt: int, delay_seconds: int) -> None:
    if not isinstance(delay_seconds, int) or delay_seconds < 0:
        raise ValueError("delay_seconds must be a non-negative int")
    seasons_str = ",".join(str(s) for s in (seasons or []))
    delay_modifier = f"+{delay_seconds} seconds"
    with _connect() as conn:
        conn.execute(
            """INSERT INTO retry_queue (imdb_id, title, media_type, seasons, attempt, next_retry_at)
               VALUES (?, ?, ?, ?, ?, datetime('now', ?))""",
            (imdb_id, title, media_type, seasons_str or None, attempt, delay_modifier),
        )
        conn.commit()


def get_due_retries() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM retry_queue WHERE next_retry_at <= datetime('now') ORDER BY next_retry_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_retries() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM retry_queue ORDER BY next_retry_at"
        ).fetchall()
        return [dict(r) for r in rows]


def remove_retry(retry_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM retry_queue WHERE id=?", (retry_id,))
        conn.commit()


# ── per-show quality override ─────────────────────────────────────────────────

def upsert_show_override(imdb_id: str, quality_preference: str | None,
                          allow_4k: bool | None, prefer_hevc: bool | None,
                          notes: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO show_quality_override (imdb_id, quality_preference, allow_4k, prefer_hevc, notes)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(imdb_id) DO UPDATE SET
                 quality_preference=excluded.quality_preference,
                 allow_4k=excluded.allow_4k,
                 prefer_hevc=excluded.prefer_hevc,
                 notes=excluded.notes""",
            (imdb_id, quality_preference,
             None if allow_4k is None else int(allow_4k),
             None if prefer_hevc is None else int(prefer_hevc),
             notes),
        )
        conn.commit()


def get_show_override(imdb_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM show_quality_override WHERE imdb_id=?", (imdb_id,)
        ).fetchone()
        return dict(row) if row else None


def get_all_show_overrides() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM show_quality_override ORDER BY imdb_id"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_show_override(imdb_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM show_quality_override WHERE imdb_id=?", (imdb_id,))
        conn.commit()


# ── metrics ───────────────────────────────────────────────────────────────────

def record_metric(metric: str, label: str | None = None,
                   value_int: int | None = None, value_real: float | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO metric_events (metric, label, value_int, value_real) VALUES (?, ?, ?, ?)",
            (metric, label, value_int, value_real),
        )
        conn.commit()


def get_metric_summary(metric: str, days: int = 30) -> list[dict]:
    """Aggregate counts per label over the last N days."""
    if not isinstance(days, int) or days < 0:
        raise ValueError("days must be a non-negative int")
    with _connect() as conn:
        rows = conn.execute(
            """SELECT label, COUNT(*) as count, AVG(value_real) as avg_real,
                      SUM(value_int) as sum_int
               FROM metric_events
               WHERE metric=? AND created_at > datetime('now', ?)
               GROUP BY label ORDER BY count DESC""",
            (metric, f"-{days} days"),
        ).fetchall()
        return [dict(r) for r in rows]


# ── settings (runtime overrides for .env) ─────────────────────────────────────

def get_setting(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str | None) -> None:
    with _connect() as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value,
                     updated_at=strftime('%Y-%m-%d %H:%M:%S','now')""",
                (key, value),
            )
        conn.commit()


def get_all_settings() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


def get_repair_items(limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM repair_items ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recently_unfixable_paths(hours: int = 24) -> set[str]:
    """Return paths marked unfixable within the last N hours  -  used to skip re-trying."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT path FROM repair_items
               WHERE status='unfixable'
               AND created_at > datetime('now', ?)""",
            (f"-{hours} hours",),
        ).fetchall()
        return {r["path"] for r in rows}


# ── users / multi-user ────────────────────────────────────────────────────────

def create_user(username: str, password_hash: str, role: str = "user",
                 quota_monthly: int = 0, auto_approve: bool = False) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO users (username, password_hash, role, quota_monthly, auto_approve)
               VALUES (?, ?, ?, ?, ?)""",
            (username, password_hash, role, quota_monthly, 1 if auto_approve else 0),
        )
        return cur.lastrowid


def get_user_by_username(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def get_user(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def update_user(user_id: int, **fields) -> None:
    if not fields:
        return
    # Core columns always allowed; plugin migrations may add extra columns  - 
    # allow any column that actually exists in the table so plugin fields work.
    _CORE = {"password_hash", "role", "quota_monthly", "auto_approve", "enabled", "region"}
    with _connect() as conn:
        db_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        allowed = _CORE | db_cols
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        sql = "UPDATE users SET " + ", ".join(f"{c}=?" for c in cols) + " WHERE id=?"
        vals = [fields[c] for c in cols] + [user_id]
        conn.execute(sql, vals)


def touch_user_login(user_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET last_login=strftime('%Y-%m-%d %H:%M:%S','now') WHERE id=?",
                     (user_id,))


def delete_user(user_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def upsert_trakt_watched(user_id: int, tmdb_id: int, media_type: str,
                          imdb_id: str | None = None, season: int | None = None,
                          episode: int | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO trakt_watched (user_id, tmdb_id, imdb_id, media_type, season, episode) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, tmdb_id, media_type, season, episode) DO UPDATE SET "
            "imdb_id=COALESCE(excluded.imdb_id, imdb_id), "
            "watched_at=strftime('%Y-%m-%d %H:%M:%S', 'now')",
            (user_id, tmdb_id, imdb_id, media_type, season, episode),
        )
        conn.commit()


def get_trakt_watched_movies(user_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tmdb_id, imdb_id FROM trakt_watched WHERE user_id=? AND media_type='movie'",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_trakt_watched_episodes(user_id: int) -> dict[str, dict[str, list[int]]]:
    """Return {imdb_id: {"season": [episode, ...]}} for a user's watched episodes."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT imdb_id, season, episode FROM trakt_watched "
            "WHERE user_id=? AND media_type='episode' AND imdb_id IS NOT NULL "
            "AND season IS NOT NULL AND episode IS NOT NULL",
            (user_id,),
        ).fetchall()
    shows: dict[str, dict[str, list[int]]] = {}
    for r in rows:
        season_map = shows.setdefault(r["imdb_id"], {})
        season_map.setdefault(str(r["season"]), []).append(r["episode"])
    return shows


def add_favorite_actor(user_id: int, person_id: int, name: str, profile_path: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO favorite_actors (user_id, person_id, name, profile_path) "
            "VALUES (?, ?, ?, ?)",
            (user_id, person_id, name, profile_path),
        )
        conn.commit()


def remove_favorite_actor(user_id: int, person_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM favorite_actors WHERE user_id=? AND person_id=?", (user_id, person_id))
        conn.commit()


def get_favorite_actors(user_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM favorite_actors WHERE user_id=? ORDER BY added_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_favorite_actors() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT person_id, name FROM favorite_actors").fetchall()
        return [dict(r) for r in rows]


def user_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


# ── watchlist ─────────────────────────────────────────────────────────────────

def add_to_watchlist(user_id: int, imdb_id: str, tmdb_id: int | None,
                     media_type: str, title: str, poster_path: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO watchlist (user_id, imdb_id, tmdb_id, media_type, title, poster_path)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, imdb_id, media_type) DO NOTHING""",
            (user_id, imdb_id, tmdb_id, media_type, title, poster_path),
        )


def remove_from_watchlist(user_id: int, imdb_id: str, media_type: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM watchlist WHERE user_id=? AND imdb_id=? AND media_type=?",
            (user_id, imdb_id, media_type),
        )


def get_watchlist(user_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist WHERE user_id=? ORDER BY added_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── user_requests (approval flow) ─────────────────────────────────────────────

def create_user_request(user_id: int, imdb_id: str, tmdb_id: int | None,
                        media_type: str, title: str, seasons: list[int] | None = None,
                        status: str = "pending") -> int:
    seasons_str = ",".join(str(s) for s in (seasons or [])) or None
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO user_requests (user_id, imdb_id, tmdb_id, media_type, title, seasons, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, imdb_id, tmdb_id, media_type, title, seasons_str, status),
        )
        return cur.lastrowid


def get_user_requests(user_id: int | None = None, status: str | None = None,
                       limit: int = 200) -> list[dict]:
    sql = """SELECT ur.*, u.username
             FROM user_requests ur
             JOIN users u ON u.id = ur.user_id"""
    where = []
    args: list = []
    if user_id is not None:
        where.append("ur.user_id=?")
        args.append(user_id)
    if status:
        where.append("ur.status=?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ur.created_at DESC LIMIT ?"
    args.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def update_user_request_status(req_id: int, status: str, reviewed_by: int | None = None,
                                note: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE user_requests
               SET status=?, reviewed_by=?, reviewed_at=strftime('%Y-%m-%d %H:%M:%S','now'),
                   note=COALESCE(?, note)
               WHERE id=?""",
            (status, reviewed_by, note, req_id),
        )


def get_user_request(req_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM user_requests WHERE id=?", (req_id,)).fetchone()
        return dict(row) if row else None


def count_user_requests_this_month(user_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM user_requests
               WHERE user_id=? AND created_at >= strftime('%Y-%m-01 00:00:00','now')""",
            (user_id,),
        ).fetchone()
        return row["n"] if row else 0


# ── wanted_movies (waiting for an acceptable-quality release) ──────────────────

def upsert_wanted_movie(imdb_id: str, tmdb_id: int | None, title: str,
                        reason: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO wanted_movies (imdb_id, tmdb_id, title, reason)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(imdb_id) DO UPDATE SET
                 title=excluded.title, reason=excluded.reason""",
            (imdb_id, tmdb_id, title, reason),
        )


def get_wanted_movies() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM wanted_movies ORDER BY added_at"
        ).fetchall()
        return [dict(r) for r in rows]


def remove_wanted_movie(imdb_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM wanted_movies WHERE imdb_id=?", (imdb_id,))


def touch_wanted_movie(imdb_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE wanted_movies
               SET attempts = attempts + 1,
                   last_checked = strftime('%Y-%m-%d %H:%M:%S','now')
               WHERE imdb_id=?""",
            (imdb_id,),
        )


# ── playability_state ─────────────────────────────────────────────────────────

def get_playability_state(content_key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM playability_state WHERE content_key=?", (content_key,)
        ).fetchone()
        return dict(row) if row else None


def update_playability_ok(content_key: str, provider: str) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO playability_state
               (content_key, status, last_ok_provider, last_ok_at, consecutive_failures, updated_at)
               VALUES (?, 'playable', ?, strftime('%Y-%m-%d %H:%M:%S','now'), 0,
                       strftime('%Y-%m-%d %H:%M:%S','now'))
               ON CONFLICT(content_key) DO UPDATE SET
                 status='playable',
                 last_ok_provider=excluded.last_ok_provider,
                 last_ok_at=excluded.last_ok_at,
                 consecutive_failures=0,
                 updated_at=excluded.updated_at""",
            (content_key, provider),
        )
        conn.commit()


def update_playability_fail(content_key: str, reason: str) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO playability_state
               (content_key, status, last_fail_reason, consecutive_failures, updated_at)
               VALUES (?, 'degraded', ?, 1, strftime('%Y-%m-%d %H:%M:%S','now'))
               ON CONFLICT(content_key) DO UPDATE SET
                 status='degraded',
                 last_fail_reason=excluded.last_fail_reason,
                 consecutive_failures=consecutive_failures + 1,
                 updated_at=excluded.updated_at""",
            (content_key, reason),
        )
        conn.commit()


def reset_playability_state(content_key: str) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO playability_state (content_key, status, consecutive_failures, updated_at)
               VALUES (?, 'unknown', 0, strftime('%Y-%m-%d %H:%M:%S','now'))
               ON CONFLICT(content_key) DO UPDATE SET
                 status='unknown',
                 consecutive_failures=0,
                 last_fail_reason=NULL,
                 updated_at=excluded.updated_at""",
            (content_key,),
        )
        conn.commit()


def get_degraded_items(min_failures: int = 3) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT ps.*, vi.title, vi.token, vi.strm_path
               FROM playability_state ps
               LEFT JOIN virtual_items vi ON (
                   vi.imdb_id = ps.content_key
                   OR ps.content_key LIKE vi.imdb_id || ':%'
               )
               WHERE ps.status='degraded' AND ps.consecutive_failures >= ?
               ORDER BY ps.consecutive_failures DESC, ps.updated_at DESC
               LIMIT 100""",
            (min_failures,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── integrity report ──────────────────────────────────────────────────────────

def _valid_imdb(imdb_id: str | None) -> bool:
    """A well-formed IMDb id: 'tt' followed by digits."""
    if not imdb_id:
        return False
    imdb_id = imdb_id.strip()
    return imdb_id.startswith("tt") and imdb_id[2:].isdigit() and len(imdb_id) > 3


def integrity_report() -> dict:
    """Read-only scan for data-integrity problems that silently break playback.

    Wrong/empty imdb_id is the leading cause of 'cache exists but no play':
    without a usable imdb_id the on-play search has nothing to look up. This
    surfaces those (and other mapping issues) before a user hits Play.
    """
    report: dict[str, list[dict]] = {
        "missing_imdb": [],        # NULL / empty / malformed imdb_id
        "missing_hash": [],        # empty info_hash or magnet
        "missing_strm_path": [],   # no .strm path recorded
        "duplicate_content": [],   # >1 token for the same imdb_id+season+episode
        "orphan_playability": [],  # playability_state row with no virtual_item
    }
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT token, title, media_type, imdb_id, info_hash, magnet, "
            "strm_path, season, episode FROM virtual_items"
        ).fetchall()]

        seen: dict[tuple, str] = {}
        for r in rows:
            brief = {"token": r["token"], "title": r["title"],
                     "imdb_id": r["imdb_id"]}
            if not _valid_imdb(r["imdb_id"]):
                report["missing_imdb"].append(brief)
            if not (r["info_hash"] or "").strip() or not (r["magnet"] or "").strip():
                report["missing_hash"].append(brief)
            if not (r["strm_path"] or "").strip():
                report["missing_strm_path"].append(brief)
            if _valid_imdb(r["imdb_id"]):
                key = (r["imdb_id"], r["season"], r["episode"])
                if key in seen:
                    report["duplicate_content"].append(
                        {**brief, "season": r["season"], "episode": r["episode"],
                         "other_token": seen[key]})
                else:
                    seen[key] = r["token"]

        ps_keys = [r["content_key"] for r in conn.execute(
            "SELECT content_key FROM playability_state").fetchall()]
        known_imdb = {r["imdb_id"] for r in rows if r["imdb_id"]}
        for ck in ps_keys:
            base = ck.split(":", 1)[0]
            if base not in known_imdb:
                report["orphan_playability"].append({"content_key": ck})

    report["counts"] = {k: len(v) for k, v in report.items()}
    report["clean"] = all(not v for k, v in report.items() if k != "counts")
    return report


def log_createtorrent(ts: float, reason: str) -> None:
    """Persist a createtorrent call so the quota counter survives restarts."""
    with _connect() as conn:
        conn.execute("INSERT INTO createtorrent_log (ts, reason) VALUES (?, ?)", (ts, reason))
        conn.commit()
        # Prune entries older than 2 hours to keep the table small.
        conn.execute("DELETE FROM createtorrent_log WHERE ts < ?", (ts - 7200,))
        conn.commit()


def get_createtorrent_log(since_ts: float) -> list[tuple[float, str]]:
    """Return all createtorrent entries after since_ts as (ts, reason) tuples."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, reason FROM createtorrent_log WHERE ts >= ? ORDER BY ts",
            (since_ts,),
        ).fetchall()
    return [(r["ts"], r["reason"]) for r in rows]
