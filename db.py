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
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _connect() as conn:
        conn.execute(_DDL)
        conn.commit()


def insert_request(title: str, imdb_id: str, media_type: str, seasons: list[int] | None = None) -> int:
    seasons_str = ",".join(str(s) for s in (seasons or []))
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO requests (title, imdb_id, media_type, seasons) VALUES (?, ?, ?, ?)",
            (title, imdb_id, media_type, seasons_str or None),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def update_request(
    row_id: int,
    status: str,
    quality: str | None = None,
    source: str | None = None,
    info_hash: str | None = None,
    error: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE requests
               SET status=?, quality=?, source=?, info_hash=?, error=?,
                   updated_at=strftime('%Y-%m-%d %H:%M:%S', 'now')
               WHERE id=?""",
            (status, quality, source, info_hash, error, row_id),
        )
        conn.commit()


def get_recent(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
