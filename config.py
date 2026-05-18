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

TORRENTIO_BASE_URL = _env("TORRENTIO_BASE_URL", "https://torrentio.strem.fun")
# Torrentio config string controls trackers/providers; the default is the public defaults.
TORRENTIO_OPTS = _env("TORRENTIO_OPTS", "")

JELLYFIN_URL = _env("JELLYFIN_URL", "http://10.0.0.10:8096")
JELLYFIN_API_KEY = _env("JELLYFIN_API_KEY", "")

LISTEN_HOST = _env("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = _env_int("LISTEN_PORT", 8088)

# Quality preferences. 4K is excluded by default per spec (HDD constraint).
QUALITY_PREFERENCE = [q.strip() for q in _env("QUALITY_PREFERENCE", "1080p,720p").split(",") if q.strip()]
ALLOW_4K = _env("ALLOW_4K", "false").lower() in ("1", "true", "yes")

# How long to wait for Torbox to make the torrent available before triggering Jellyfin scan.
TORBOX_POLL_INTERVAL_SEC = _env_int("TORBOX_POLL_INTERVAL_SEC", 10)
TORBOX_POLL_TIMEOUT_SEC = _env_int("TORBOX_POLL_TIMEOUT_SEC", 600)

WEBHOOK_SECRET = _env("WEBHOOK_SECRET", "")

LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
