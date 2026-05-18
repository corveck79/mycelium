import logging
import os


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


TORBOX_API_KEY = _env("TORBOX_API_KEY", required=True)
TORBOX_BASE_URL = _env("TORBOX_BASE_URL", "https://api.torbox.app/v1/api")

ZILEAN_URL = _env("ZILEAN_URL", "http://10.0.0.10:8181")
ZILEAN_ENABLED = _env("ZILEAN_ENABLED", "true").lower() in ("1", "true", "yes")

TORRENTIO_BASE_URL = _env("TORRENTIO_BASE_URL", "https://torrentio.strem.fun")
TORRENTIO_OPTS = _env("TORRENTIO_OPTS", "")

JELLYFIN_URL = _env("JELLYFIN_URL", "http://10.0.0.10:8096")
JELLYFIN_API_KEY = _env("JELLYFIN_API_KEY", "")
# Seconds to wait after TorBox reports ready before triggering Jellyfin scan.
JELLYFIN_REFRESH_DELAY_SEC = _env_int("JELLYFIN_REFRESH_DELAY_SEC", 60)

SEERR_URL = _env("SEERR_URL", "http://10.0.0.10:5055")
SEERR_API_KEY = _env("SEERR_API_KEY", "")

TMDB_API_KEY = _env("TMDB_API_KEY", "")

LISTEN_HOST = _env("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = _env_int("LISTEN_PORT", 8088)

# Quality preferences — 1080p first, 4K fallback, 720p last resort.
QUALITY_PREFERENCE = [q.strip() for q in _env("QUALITY_PREFERENCE", "1080p,2160p,720p").split(",") if q.strip()]
ALLOW_4K = _env("ALLOW_4K", "true").lower() in ("1", "true", "yes")
EXCLUDE_REMUX = _env("EXCLUDE_REMUX", "true").lower() in ("1", "true", "yes")
EXCLUDE_CAM = _env("EXCLUDE_CAM", "true").lower() in ("1", "true", "yes")
PREFER_WEBDL = _env("PREFER_WEBDL", "true").lower() in ("1", "true", "yes")
PREFER_HEVC = _env("PREFER_HEVC", "true").lower() in ("1", "true", "yes")
# Minimum seeders to include a candidate (0 = no filter; unknown seeders always pass).
MIN_SEEDERS = _env_int("MIN_SEEDERS", 3)
# Maximum file size in GB to include a candidate (0 = no limit; unknown size always passes).
MAX_SIZE_GB = _env_int("MAX_SIZE_GB", 0)

# How long to wait for Torbox to make the torrent available before triggering Jellyfin scan.
TORBOX_POLL_INTERVAL_SEC = _env_int("TORBOX_POLL_INTERVAL_SEC", 10)
TORBOX_POLL_TIMEOUT_SEC = _env_int("TORBOX_POLL_TIMEOUT_SEC", 600)

WEBHOOK_SECRET = _env("WEBHOOK_SECRET", "")

DB_PATH = _env("DB_PATH", "/data/requests.db")

CATCHUP_ENABLED = _env("CATCHUP_ENABLED", "true").lower() in ("1", "true", "yes")
CATCHUP_DELAY_SEC = _env_int("CATCHUP_DELAY_SEC", 30)
CATCHUP_TAKE = _env_int("CATCHUP_TAKE", 20)

# Path where TMC writes .strm files (inside container, mount /data/media there).
MEDIA_PATH = _env("MEDIA_PATH", "/data/media")
MONITOR_INTERVAL_HOURS = _env_int("MONITOR_INTERVAL_HOURS", 6)
MOVIE_SYNC_INTERVAL_MINUTES = _env_int("MOVIE_SYNC_INTERVAL_MINUTES", 30)
MAX_RETRY_ATTEMPTS = _env_int("MAX_RETRY_ATTEMPTS", 10)

TMC_CONTAINER_NAME = _env("TMC_CONTAINER_NAME", "tmc")

# Automatic Jellyfin merge of duplicate movie versions (every N hours; 0 disables).
MERGE_VERSIONS_INTERVAL_HOURS = _env_int("MERGE_VERSIONS_INTERVAL_HOURS", 6)

# Cleanup/repair scan every N hours (0 disables).
CLEANUP_INTERVAL_HOURS = _env_int("CLEANUP_INTERVAL_HOURS", 24)

LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
