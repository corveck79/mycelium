"""Self-monitoring: deadman switch + disk-space warnings.

deadman_check(): if no successful add or strm_generator run has happened in
DEADMAN_HOURS, fire a notification once per debounce window.

disk_check(): warn if MEDIA_PATH or the DB volume crosses the configured
fill percentage. De-bounced so it doesn't spam.
"""
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

import db
import notify
from config import DB_PATH, MEDIA_PATH

log = logging.getLogger(__name__)


_last_warn: dict[str, float] = {}
_WARN_DEBOUNCE_SEC = 6 * 3600  # 6h between repeated warnings per metric


def _warn(metric: str, title: str, message: str) -> None:
    now = time.monotonic()
    if now - _last_warn.get(metric, 0) < _WARN_DEBOUNCE_SEC:
        return
    _last_warn[metric] = now
    log.warning(message)
    db.log_activity("watchdog", title, message, False)
    notify.send(title, message, success=False)


# ── Deadman switch ────────────────────────────────────────────────────────────

DEADMAN_HOURS = 24


def _last_success_age_hours() -> float | None:
    """Hours since the most recent successful add (or 'added' activity event)."""
    try:
        activity = db.get_activity(50)
    except Exception:
        return None
    for ev in activity:
        if ev.get("success") and ev.get("event") in ("added", "upgraded"):
            ts = ev.get("created_at")
            if not ts:
                continue
            try:
                t = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            return (datetime.utcnow() - t).total_seconds() / 3600
    return None


def deadman_check() -> None:
    age = _last_success_age_hours()
    if age is None or age < DEADMAN_HOURS:
        return
    _warn(
        "deadman",
        "Deadman: no activity",
        f"No successful add in the last {age:.1f} hours  -  scheduler stuck or services unreachable?",
    )


# ── Disk space ────────────────────────────────────────────────────────────────

DISK_WARN_PERCENT = 90


def _check_path(path: str, name: str) -> None:
    try:
        p = Path(path)
        target = p if p.exists() else p.parent
        usage = shutil.disk_usage(str(target))
    except Exception as exc:
        log.debug("Disk check %s failed: %s", path, exc)
        return
    pct = 100 * usage.used / max(1, usage.total)
    if pct >= DISK_WARN_PERCENT:
        gb_free = usage.free / 1e9
        _warn(
            f"disk:{name}",
            f"Disk almost full ({name})",
            f"{name} volume {pct:.0f}% full · {gb_free:.1f} GB free",
        )


def disk_check() -> None:
    _check_path(MEDIA_PATH, "media")
    _check_path(DB_PATH, "db")
