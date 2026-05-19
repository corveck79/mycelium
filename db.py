import sqlite3
from config import DB_PATH

_DDL = """
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    imdb_id     TEXT    NOT NULL,
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
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        for stmt in _DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()


# ── requests ──────────────────────────────────────────────────────────────────

def insert_request(title: str, imdb_id: str, media_type: str, seasons: list[int] | None = None) -> int:
    seasons_str = ",".join(str(s) for s in (seasons or []))
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO requests (title, imdb_id, media_type, seasons) VALUES (?, ?, ?, ?)",
            (title, imdb_id, media_type, seasons_str or None),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


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


def get_recent(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── monitored_series ──────────────────────────────────────────────────────────

def upsert_monitored_series(imdb_id: str, tmdb_id: int | None, title: str, seasons: list[int]) -> None:
    seasons_str = ",".join(str(s) for s in seasons)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO monitored_series (imdb_id, tmdb_id, title, seasons)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(imdb_id) DO UPDATE SET
                 tmdb_id=COALESCE(excluded.tmdb_id, tmdb_id),
                 title=excluded.title,
                 seasons=excluded.seasons,
                 status='active'""",
            (imdb_id, tmdb_id, title, seasons_str),
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


def get_repair_items(limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM repair_items ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
