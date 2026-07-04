"""Native DMM hash index -- an in-process alternative to the external
iPromKnight/zilean service (ZILEAN_MODE=native vs the default "external").

Downloads the community-shared hashlist snapshots from
github.com/debridmediamanager/hashlists, decodes each page's embedded
LZString payload (filename + info_hash + size) and indexes them in a local
SQLite database. Runs entirely in-process; no separate container, no
Postgres, no network dependency at query time (only during the periodic
sync).

Also supports one-time bulk import from an existing external Zilean's
Postgres database, for users switching from external to native mode who
don't want to re-scrape everything from scratch.
"""
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import requests

import lz_string
from config import ZILEAN_DB_PATH
from torrentio import _looks_like_season_pack

log = logging.getLogger(__name__)

_HASHLISTS_ZIP_URL = "https://github.com/debridmediamanager/hashlists/archive/refs/heads/main.zip"
_IFRAME_RE = re.compile(r'src="https://debridmediamanager\.com/hashlist#([^"]+)"')
_WORD_RE = re.compile(r"[a-z0-9]+")
_HASH_RE = re.compile(r"^[0-9a-f]{40}$")
_SIZE_STR_RE = re.compile(r"([\d.]+)\s*(TB|GB|MB|KB)", re.IGNORECASE)
_HTTP_HEADERS = {"User-Agent": "Mycelium (zilean-native-index)"}

_DB_PATH = ZILEAN_DB_PATH

_sync_lock = threading.Lock()
_import_lock = threading.Lock()
_tls = threading.local()


def _connect() -> sqlite3.Connection:
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        return conn
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_schema(conn)
    _tls.conn = conn
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hashes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            info_hash   TEXT    NOT NULL UNIQUE,
            raw_title   TEXT    NOT NULL,
            title_norm  TEXT    NOT NULL,
            size_bytes  INTEGER NOT NULL DEFAULT 0,
            imdb_id     TEXT,
            added_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hashes_title_norm ON hashes(title_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hashes_imdb_id ON hashes(imdb_id)")
    conn.execute("CREATE TABLE IF NOT EXISTS seen_pages (filename TEXT PRIMARY KEY)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            id                    INTEGER PRIMARY KEY CHECK (id = 1),
            last_synced_at        TEXT,
            last_status           TEXT NOT NULL DEFAULT 'never',
            last_new_hashes       INTEGER NOT NULL DEFAULT 0,
            last_pages_processed  INTEGER NOT NULL DEFAULT 0,
            last_error            TEXT,
            last_import_at        TEXT,
            last_import_count     INTEGER NOT NULL DEFAULT 0,
            last_import_error     TEXT
        )
    """)
    conn.execute("INSERT OR IGNORE INTO sync_state (id) VALUES (1)")

    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS hashes_fts USING fts5"
            "(title_norm, content='hashes', content_rowid='id')"
        )
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS hashes_ai AFTER INSERT ON hashes BEGIN
                INSERT INTO hashes_fts(rowid, title_norm) VALUES (new.id, new.title_norm);
            END
        """)
        _tls.fts_available = True
    except sqlite3.OperationalError as exc:
        log.warning("Zilean index: FTS5 unavailable (%s), falling back to LIKE search", exc)
        _tls.fts_available = False


def _normalize(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _parse_size_to_bytes(size: str | int | float | None) -> int:
    """Handles both a plain byte count and a formatted string like '1.64 GB'
    (the format an external Zilean's Postgres 'Size' column uses)."""
    if size is None:
        return 0
    if isinstance(size, (int, float)):
        return int(size)
    m = _SIZE_STR_RE.search(str(size))
    if not m:
        try:
            return int(size)
        except (TypeError, ValueError):
            return 0
    value = float(m.group(1))
    unit = m.group(2).upper()
    multiplier = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}[unit]
    return int(value * multiplier)


def _extract_entries(html: str) -> list[dict]:
    m = _IFRAME_RE.search(html)
    if not m:
        return []
    try:
        decompressed = lz_string.decompress_from_encoded_uri_component(m.group(1))
    except Exception:
        return []
    if not decompressed:
        return []
    try:
        entries = json.loads(decompressed)
    except (ValueError, TypeError):
        return []
    return entries if isinstance(entries, list) else []


def _download(url: str, dest: str) -> None:
    with requests.get(url, headers=_HTTP_HEADERS, stream=True, timeout=600) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                fh.write(chunk)


def sync(force: bool = False, min_interval_hours: float = 6.0) -> dict:
    """Download the DMM hashlist snapshot and index any pages not seen before.

    Only ever downloads/parses pages this instance hasn't processed yet
    (tracked in seen_pages), so repeated runs are cheap once the initial
    backfill is done. Safe to call from a scheduler; a lock keeps concurrent
    calls from running the (multi-minute) sync twice at once.
    """
    if not _sync_lock.acquire(blocking=False):
        log.info("Zilean sync already running, skipping")
        return {"status": "already_running"}
    try:
        conn = _connect()
        state = conn.execute("SELECT * FROM sync_state WHERE id=1").fetchone()
        if not force and state and state["last_synced_at"]:
            try:
                last = time.mktime(time.strptime(state["last_synced_at"], "%Y-%m-%d %H:%M:%S"))
                if (time.time() - last) < min_interval_hours * 3600:
                    return {"status": "skipped_recent"}
            except ValueError:
                pass

        conn.execute("UPDATE sync_state SET last_status='running' WHERE id=1")

        tmp_dir = tempfile.mkdtemp(prefix="zilean-dmm-")
        try:
            zip_path = os.path.join(tmp_dir, "hashlists.zip")
            log.info("Zilean sync: downloading DMM hashlist snapshot")
            _download(_HASHLISTS_ZIP_URL, zip_path)

            seen = {r["filename"] for r in conn.execute("SELECT filename FROM seen_pages")}
            new_hashes = 0
            pages_processed = 0

            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    name = os.path.basename(info.filename)
                    if not name.endswith(".html") or name in seen:
                        continue
                    try:
                        html = zf.read(info).decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    for entry in _extract_entries(html):
                        if not isinstance(entry, dict):
                            continue
                        info_hash = (entry.get("hash") or "").strip().lower()
                        raw_title = entry.get("filename") or ""
                        if not raw_title or not _HASH_RE.match(info_hash):
                            continue
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO hashes (info_hash, raw_title, title_norm, size_bytes) "
                            "VALUES (?, ?, ?, ?)",
                            (info_hash, raw_title, _normalize(raw_title), int(entry.get("bytes") or 0)),
                        )
                        if cur.rowcount:
                            new_hashes += 1
                    conn.execute("INSERT OR IGNORE INTO seen_pages (filename) VALUES (?)", (name,))
                    pages_processed += 1
                    if pages_processed % 500 == 0:
                        conn.commit()
                        log.info("Zilean sync: %d new page(s) processed so far (%d new hashes)",
                                  pages_processed, new_hashes)

            conn.execute(
                "UPDATE sync_state SET last_synced_at=strftime('%Y-%m-%d %H:%M:%S','now'), "
                "last_status='ok', last_new_hashes=?, last_pages_processed=?, last_error=NULL WHERE id=1",
                (new_hashes, pages_processed),
            )
            conn.commit()
            log.info("Zilean sync complete: %d new page(s), %d new hash(es)", pages_processed, new_hashes)
            return {"status": "ok", "new_pages": pages_processed, "new_hashes": new_hashes}
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as exc:
        log.error("Zilean sync failed: %s", exc)
        try:
            _connect().execute(
                "UPDATE sync_state SET last_status='error', last_error=? WHERE id=1",
                (str(exc)[:500],),
            )
            _connect().commit()
        except Exception:
            pass
        return {"status": "error", "error": str(exc)}
    finally:
        _sync_lock.release()


def import_from_postgres(host: str, port: int, dbname: str, user: str, password: str,
                          batch_size: int = 5000) -> dict:
    """One-time bulk import from an existing external iPromKnight/zilean Postgres
    database (its "Torrents" table) into the native index, for users switching
    from external to native mode without re-scraping everything from scratch.

    Uses a server-side cursor to stream rows without loading all of them (can
    be 1M+) into memory at once, and batches SQLite inserts in one transaction
    per batch for speed.
    """
    if not _import_lock.acquire(blocking=False):
        log.info("Zilean Postgres import already running, skipping")
        return {"status": "already_running"}
    try:
        import psycopg2
        import psycopg2.extras

        conn = _connect()
        imported = 0
        skipped = 0
        try:
            with psycopg2.connect(host=host, port=port, dbname=dbname, user=user,
                                   password=password, connect_timeout=10) as pg_conn:
                with pg_conn.cursor(name="mycelium_zilean_import",
                                    cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.itersize = batch_size
                    cur.execute(
                        'SELECT "InfoHash", "RawTitle", "ParsedTitle", "Size", "ImdbId" FROM "Torrents"'
                    )
                    batch = []
                    for row in cur:
                        info_hash = (row.get("InfoHash") or "").strip().lower()
                        raw_title = row.get("RawTitle") or row.get("ParsedTitle") or ""
                        if not info_hash or not raw_title or not _HASH_RE.match(info_hash):
                            skipped += 1
                            continue
                        batch.append((
                            info_hash, raw_title, _normalize(raw_title),
                            _parse_size_to_bytes(row.get("Size")), row.get("ImdbId"),
                        ))
                        if len(batch) >= batch_size:
                            imported += _flush_import_batch(conn, batch)
                            batch = []
                            if imported % (batch_size * 10) < batch_size:
                                log.info("Zilean Postgres import: %d rows so far", imported)
                    if batch:
                        imported += _flush_import_batch(conn, batch)
        except Exception as exc:
            log.error("Zilean Postgres import failed: %s", exc)
            conn.execute(
                "UPDATE sync_state SET last_import_error=? WHERE id=1", (str(exc)[:500],),
            )
            conn.commit()
            return {"status": "error", "error": str(exc), "imported": imported, "skipped": skipped}

        conn.execute(
            "UPDATE sync_state SET last_import_at=strftime('%Y-%m-%d %H:%M:%S','now'), "
            "last_import_count=?, last_import_error=NULL WHERE id=1",
            (imported,),
        )
        conn.commit()
        log.info("Zilean Postgres import complete: %d imported, %d skipped", imported, skipped)
        return {"status": "ok", "imported": imported, "skipped": skipped}
    finally:
        _import_lock.release()


def _flush_import_batch(conn: sqlite3.Connection, batch: list[tuple]) -> int:
    cur = conn.executemany(
        "INSERT OR IGNORE INTO hashes (info_hash, raw_title, title_norm, size_bytes, imdb_id) "
        "VALUES (?, ?, ?, ?, ?)",
        batch,
    )
    conn.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(batch)


_EPISODE_RE_CACHE: dict[tuple[int, int], re.Pattern] = {}
_SEASON_ANY_EP_RE_CACHE: dict[int, re.Pattern] = {}


def _episode_pattern(season: int, episode: int) -> re.Pattern:
    key = (season, episode)
    pat = _EPISODE_RE_CACHE.get(key)
    if pat is None:
        pat = re.compile(rf"\bs0*{season}e0*{episode}\b", re.IGNORECASE)
        _EPISODE_RE_CACHE[key] = pat
    return pat


def _season_any_episode_pattern(season: int) -> re.Pattern:
    pat = _SEASON_ANY_EP_RE_CACHE.get(season)
    if pat is None:
        pat = re.compile(rf"\bs0*{season}e0*\d+\b", re.IGNORECASE)
        _SEASON_ANY_EP_RE_CACHE[season] = pat
    return pat


def _fts_query(conn: sqlite3.Connection, tokens: list[str], limit: int) -> list[sqlite3.Row]:
    match = " ".join(f'"{t}"' for t in tokens)
    return conn.execute(
        "SELECT h.info_hash, h.raw_title, h.size_bytes FROM hashes_fts f "
        "JOIN hashes h ON h.id = f.rowid WHERE f.title_norm MATCH ? LIMIT ?",
        (match, limit),
    ).fetchall()


def _like_query(conn: sqlite3.Connection, tokens: list[str], limit: int) -> list[sqlite3.Row]:
    where = " AND ".join("title_norm LIKE ?" for _ in tokens)
    params = [f"%{t}%" for t in tokens] + [limit]
    return conn.execute(
        f"SELECT info_hash, raw_title, size_bytes FROM hashes WHERE {where} LIMIT ?",
        params,
    ).fetchall()


def search(title: str, season: int | None = None, episode: int | None = None, limit: int = 200) -> list[dict]:
    tokens = _normalize(title).split()
    if not tokens:
        return []
    conn = _connect()
    fetch_limit = limit * 5 if season is not None else limit
    try:
        rows = (
            _fts_query(conn, tokens, fetch_limit) if getattr(_tls, "fts_available", False)
            else _like_query(conn, tokens, fetch_limit)
        )
    except sqlite3.OperationalError as exc:
        log.warning("Zilean search failed for %r: %s", title, exc)
        return []

    if season is None:
        return [dict(r) for r in rows[:limit]]

    out = []
    if episode is not None:
        # A specific episode is wanted: either that exact episode, or a
        # season pack (which necessarily contains it). A release for some
        # *other* episode of the same season is not a match.
        ep_re = _episode_pattern(season, episode)
        for r in rows:
            raw = r["raw_title"]
            if ep_re.search(raw) or _looks_like_season_pack(raw, season):
                out.append(dict(r))
            if len(out) >= limit:
                break
    else:
        # Season only: a pack, or any individual episode within it.
        any_ep_re = _season_any_episode_pattern(season)
        for r in rows:
            raw = r["raw_title"]
            if _looks_like_season_pack(raw, season) or any_ep_re.search(raw):
                out.append(dict(r))
            if len(out) >= limit:
                break
    return out


def get_status() -> dict:
    conn = _connect()
    state = conn.execute("SELECT * FROM sync_state WHERE id=1").fetchone()
    total = conn.execute("SELECT COUNT(*) AS n FROM hashes").fetchone()["n"]
    return {
        "total_hashes": total,
        "last_synced_at": state["last_synced_at"] if state else None,
        "last_status": state["last_status"] if state else "never",
        "last_new_hashes": state["last_new_hashes"] if state else 0,
        "last_pages_processed": state["last_pages_processed"] if state else 0,
        "last_error": state["last_error"] if state else None,
        "last_import_at": state["last_import_at"] if state else None,
        "last_import_count": state["last_import_count"] if state else 0,
        "last_import_error": state["last_import_error"] if state else None,
        "syncing": _sync_lock.locked(),
        "importing": _import_lock.locked(),
    }
