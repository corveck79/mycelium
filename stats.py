import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import db
from config import MEDIA_PATH

log = logging.getLogger(__name__)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def get_overview() -> dict:
    requests = db.get_recent(1000)
    monitored = db.get_all_monitored_series()
    wanted = db.get_all_wanted_episodes()
    movies = db.get_media_items("movie")

    media = Path(MEDIA_PATH)
    movie_strms = list((media / "movies").rglob("*.strm")) if (media / "movies").exists() else []
    series_strms = list((media / "series").rglob("*.strm")) if (media / "series").exists() else []

    # Success rate over last 7 days
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent = [r for r in requests if (_parse_ts(r.get("created_at")) or datetime.min) > cutoff]
    succeeded = sum(1 for r in recent if r["status"] == "success")
    failed = sum(1 for r in recent if r["status"] == "failed")
    success_rate = round(100 * succeeded / max(1, succeeded + failed), 1)

    # Quality distribution
    quality_counts: dict[str, int] = {}
    for r in requests:
        if r["status"] == "success" and r.get("quality"):
            quality_counts[r["quality"]] = quality_counts.get(r["quality"], 0) + 1

    return {
        "library": {
            "movie_count": len(movie_strms),
            "episode_count": len(series_strms),
            "series_count": len(monitored),
        },
        "requests": {
            "total": len(requests),
            "succeeded_7d": succeeded,
            "failed_7d": failed,
            "success_rate_7d": success_rate,
        },
        "wanted": {
            "active": len([w for w in wanted if w["status"] == "wanted"]),
            "found": len([w for w in wanted if w["status"] == "found"]),
            "give_up": len([w for w in wanted if w["status"] == "give_up"]),
        },
        "movies_pending": len([m for m in movies if not m.get("strm_found")]),
        "qualities": quality_counts,
    }


def get_storage_breakdown(limit: int = 20) -> list[dict]:
    """Top folders by strm count (proxy for content size since strm files are tiny)."""
    media = Path(MEDIA_PATH)
    counts: dict[str, int] = {}
    for sub in ("movies", "series"):
        base = media / sub
        if not base.exists():
            continue
        for entry in base.iterdir():
            if entry.is_dir():
                n = sum(1 for _ in entry.rglob("*.strm"))
                if n:
                    counts[f"{sub}/{entry.name}"] = n
    items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"path": p, "count": c} for p, c in items]
