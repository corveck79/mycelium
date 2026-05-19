import logging
import shutil
from datetime import datetime
from pathlib import Path

from config import DB_PATH

log = logging.getLogger(__name__)

_BACKUP_DIR = Path(DB_PATH).parent / "backups"
_KEEP = 14


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
        shutil.copy2(src, dst)
        log.info("Backup: wrote %s (%.1f KB)", dst.name, dst.stat().st_size / 1024)
    except Exception as exc:
        log.warning("Backup: copy failed: %s", exc)
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
