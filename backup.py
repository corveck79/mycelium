import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from config import DB_PATH

log = logging.getLogger(__name__)

_BACKUP_DIR = Path(DB_PATH).parent / "backups"
_KEEP = 14


def _snapshot_db(src_path: Path, dst_path: Path) -> None:
    """Take a consistent snapshot of a WAL-mode SQLite database.

    shutil.copy2 only copies the .db file, not the -wal / -shm siblings, so
    between checkpoints it produces a torn or stale image that may fail to
    open after restore. sqlite3.Connection.backup() uses SQLite's online
    backup API which handles WAL correctly under concurrent writes and
    writes a fully self-contained file (no -wal/-shm needed alongside)."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def run() -> Path | None:
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.warning("Backup: cannot create %s: %s", _BACKUP_DIR, exc)
        return None

    src = Path(DB_PATH)
    if not src.exists():
        log.info("Backup: source DB %s does not exist yet", src)
        return None

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dst = _BACKUP_DIR / f"requests_{stamp}.db"
    try:
        _snapshot_db(src, dst)
        log.info("Backup: wrote %s (%.1f KB)", dst.name, dst.stat().st_size / 1024)
    except Exception as exc:
        log.warning("Backup: snapshot failed: %s", exc)
        # Remove partial file if SQLite created one before failing.
        try:
            if dst.exists():
                dst.unlink()
        except Exception:
            pass
        return None

    # Prune oldest, keep _KEEP most recent
    backups = sorted(_BACKUP_DIR.glob("requests_*.db"))
    for old in backups[:-_KEEP]:
        try:
            old.unlink()
            log.debug("Backup: pruned %s", old.name)
        except Exception:
            pass
    return dst


def list_backups() -> list[dict]:
    """Return available backups, newest first, with size and timestamp."""
    if not _BACKUP_DIR.is_dir():
        return []
    out = []
    for p in sorted(_BACKUP_DIR.glob("requests_*.db"), reverse=True):
        try:
            st = p.stat()
            out.append({
                "name": p.name,
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception:
            continue
    return out


def restore(name: str) -> bool:
    """Restore a named backup over the live DB. The current DB is renamed
    to .pre-restore.<ts> so you can undo. Returns True on success."""
    if not name or "/" in name or ".." in name:
        log.warning("restore: rejected unsafe name %r", name)
        return False
    src = _BACKUP_DIR / name
    if not src.is_file():
        log.warning("restore: backup %s not found", name)
        return False
    dst = Path(DB_PATH)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safety = dst.with_suffix(f".pre-restore.{stamp}.db")
    try:
        if dst.exists():
            # Use the online-backup API for the safety copy too so the
            # rollback target is a consistent snapshot even if a writer is
            # still mid-transaction when restore runs.
            _snapshot_db(dst, safety)
            log.info("Restore: stashed current DB at %s", safety.name)
        shutil.copy2(src, dst)
        log.warning("Restore: replaced live DB with %s  -  restart Mycelium", name)
        return True
    except Exception as exc:
        log.error("Restore failed: %s", exc)
        return False
