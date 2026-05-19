import logging
import sqlite3
from config import DB_PATH

log = logging.getLogger(__name__)

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
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        # WAL gives concurrent reads while a writer runs — much friendlier under
        # the background scheduler + webhook traffic mix.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception as exc:
            log.warning("PRAGMA setup failed: %s", exc)
        for stmt in _DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()
    integrity_check()


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
    "activity_log":   "created_at",
    "webhook_events": "received_at",
    "metric_events":  "created_at",
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


# ── virtual_items (Catbox mode) ───────────────────────────────────────────────

def insert_virtual_item(token: str, info_hash: str, magnet: str, title: str,
                         media_type: str, strm_path: str | None = None,
                         torbox_id: int | None = None, file_id: int | None = None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO virtual_items (token, info_hash, magnet, title, media_type,
                                          strm_path, torbox_id, file_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (token, info_hash, magnet, title, media_type, strm_path, torbox_id, file_id),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def get_virtual_item(token: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM virtual_items WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None


def get_all_virtual_items() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM virtual_items ORDER BY last_played DESC, created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_virtual_torbox_id(token: str, torbox_id: int | None) -> None:
    with _connect() as conn:
        conn.execute("UPDATE virtual_items SET torbox_id=? WHERE token=?", (torbox_id, token))
        conn.commit()


def update_virtual_file_id(token: str, file_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE virtual_items SET file_id=? WHERE token=?", (file_id, token))
        conn.commit()


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
