VERSION = "0.1.0-beta.1"

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


TORBOX_API_KEY = _env("TORBOX_API_KEY", "")
TORBOX_BASE_URL = _env("TORBOX_BASE_URL", "https://api.torbox.app/v1/api")

ZILEAN_URL = _env("ZILEAN_URL", "")
ZILEAN_ENABLED = _env("ZILEAN_ENABLED", "false").lower() in ("1", "true", "yes")

TORRENTIO_BASE_URL = _env("TORRENTIO_BASE_URL", "https://torrentio.strem.fun")
TORRENTIO_OPTS = _env("TORRENTIO_OPTS", "")

JELLYFIN_URL = _env("JELLYFIN_URL", "")
JELLYFIN_API_KEY = _env("JELLYFIN_API_KEY", "")

SEERR_URL = _env("SEERR_URL", "")
SEERR_API_KEY = _env("SEERR_API_KEY", "")

TMDB_API_KEY = _env("TMDB_API_KEY", "")

LISTEN_HOST = _env("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = _env_int("LISTEN_PORT", 8088)

# Quality preferences — 1080p first, 4K fallback, 720p last resort.
QUALITY_PREFERENCE = [q.strip() for q in _env("QUALITY_PREFERENCE", "1080p,2160p,720p").split(",") if q.strip()]
ALLOW_4K = _env("ALLOW_4K", "true").lower() in ("1", "true", "yes")
EXCLUDE_REMUX = _env("EXCLUDE_REMUX", "true").lower() in ("1", "true", "yes")
EXCLUDE_BLURAY = _env("EXCLUDE_BLURAY", "false").lower() in ("1", "true", "yes")
EXCLUDE_CAM = _env("EXCLUDE_CAM", "true").lower() in ("1", "true", "yes")
# Exclude Dolby Vision Profile 5 (single-layer, no HDR10 fallback). Profile 5
# releases have DV/DoVi in the name but no HDR10 alongside — they fail direct
# play on Jellyfin Android TV, Linux app, and most browser clients. Profile 8
# (DV + HDR10) works fine because it has an HDR10 base layer as fallback.
EXCLUDE_DV_P5 = _env("EXCLUDE_DV_P5", "true").lower() in ("1", "true", "yes")
# When true, never accept cam/telesync/screener even as a last resort (no
# "only cam available, allowing them" fallback). When false, cam is allowed
# only if it's the sole option.
STRICT_NO_CAM = _env("STRICT_NO_CAM", "false").lower() in ("1", "true", "yes")
PREFER_WEBDL = _env("PREFER_WEBDL", "true").lower() in ("1", "true", "yes")
PREFER_HEVC = _env("PREFER_HEVC", "true").lower() in ("1", "true", "yes")
# Minimum seeders to include a candidate (0 = no filter; unknown seeders always pass).
MIN_SEEDERS = _env_int("MIN_SEEDERS", 3)
# Maximum file size in GB to include a candidate (0 = no limit; unknown size always passes).
MAX_SIZE_GB = _env_int("MAX_SIZE_GB", 0)
# Audio language preference (comma-separated codes: nl, en, multi). Empty = no preference.
AUDIO_LANGUAGE_PREFERENCE = [l.strip().lower() for l in _env("AUDIO_LANGUAGE_PREFERENCE", "").split(",") if l.strip()]
# Languages to hard-block (comma-separated codes, e.g. ru). Torrents detected as
# exclusively in a blocked language are filtered out before sorting.
EXCLUDE_LANGUAGES = [l.strip().lower() for l in _env("EXCLUDE_LANGUAGES", "").split(",") if l.strip()]

# How long to wait for Torbox to make the torrent available before triggering Jellyfin scan.
TORBOX_POLL_INTERVAL_SEC = _env_int("TORBOX_POLL_INTERVAL_SEC", 2)
TORBOX_POLL_TIMEOUT_SEC = _env_int("TORBOX_POLL_TIMEOUT_SEC", 600)

WEBHOOK_SECRET = _env("WEBHOOK_SECRET", "")

DB_PATH = _env("DB_PATH", "/data/requests.db")

CATCHUP_ENABLED = _env("CATCHUP_ENABLED", "true").lower() in ("1", "true", "yes")
CATCHUP_DELAY_SEC = _env_int("CATCHUP_DELAY_SEC", 30)
CATCHUP_TAKE = _env_int("CATCHUP_TAKE", 20)

MEDIA_PATH = _env("MEDIA_PATH", "/data/media")
# strm_generator scans TorBox mylist and creates missing .strm files every N hours.
STRM_GENERATOR_INTERVAL_HOURS = _env_int("STRM_GENERATOR_INTERVAL_HOURS", 1)
MONITOR_INTERVAL_HOURS = _env_int("MONITOR_INTERVAL_HOURS", 6)
MOVIE_SYNC_INTERVAL_MINUTES = _env_int("MOVIE_SYNC_INTERVAL_MINUTES", 30)
MAX_RETRY_ATTEMPTS = _env_int("MAX_RETRY_ATTEMPTS", 10)

# Automatic Jellyfin merge of duplicate movie versions (every N hours; 0 disables).
MERGE_VERSIONS_INTERVAL_HOURS = _env_int("MERGE_VERSIONS_INTERVAL_HOURS", 6)

# Cleanup/repair scan every N hours (0 disables).
CLEANUP_INTERVAL_HOURS = _env_int("CLEANUP_INTERVAL_HOURS", 24)

LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()

# ── Notifications ─────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")
NOTIFY_ON_SUCCESS = _env("NOTIFY_ON_SUCCESS", "true").lower() in ("1", "true", "yes")
NOTIFY_ON_FAILURE = _env("NOTIFY_ON_FAILURE", "true").lower() in ("1", "true", "yes")

# ── Failed-hash blacklist ─────────────────────────────────────────────────────
# Hashes that fail to add to TorBox this many times are skipped on future searches.
BLACKLIST_FAIL_THRESHOLD = _env_int("BLACKLIST_FAIL_THRESHOLD", 3)

# ── Catbox-style lazy materialization ─────────────────────────────────────────
# When enabled, .strm files contain a proxy URL pointing to /stream/<token>.
# The torrent is only added to TorBox when playback starts, and released after
# CATBOX_IDLE_MINUTES of inactivity. Stays within TorBox 30-day retention.
CATBOX_MODE = _env("CATBOX_MODE", "false").lower() in ("1", "true", "yes")
# Externally reachable host for the proxy URL written into .strm files.
# Example: http://192.168.1.50:8088 (must be reachable from Jellyfin).
# No default — set this in the setup wizard or .env, or .strm files will be broken.
CATBOX_HOST = _env("CATBOX_HOST", "")
CATBOX_IDLE_MINUTES = _env_int("CATBOX_IDLE_MINUTES", 1440)
CATBOX_GC_INTERVAL_MINUTES = _env_int("CATBOX_GC_INTERVAL_MINUTES", 10)
# True lazy materialization: on import, only verify the release is cached and
# register a virtual .strm — defer createtorrent to first playback. Massively
# reduces createtorrent (60/hour) usage for large libraries. Movies only for now.
CATBOX_LAZY_ADD = _env("CATBOX_LAZY_ADD", "false").lower() in ("1", "true", "yes")
# Pre-add the torrent to TorBox in the background when a .strm is created,
# so first playback is instant instead of waiting for createtorrent.
# Only fires for cached torrents (no slot cost). Falls back to catbox on failure.
CATBOX_PRELOAD = _env("CATBOX_PRELOAD", "true").lower() in ("1", "true", "yes")

# ── DB backup ─────────────────────────────────────────────────────────────────
BACKUP_INTERVAL_HOURS = _env_int("BACKUP_INTERVAL_HOURS", 24)

# ── Retry backoff ─────────────────────────────────────────────────────────────
# Schedule failed requests for retry on exponential backoff (minutes).
RETRY_BACKOFF_MINUTES = [int(x) for x in _env("RETRY_BACKOFF_MINUTES", "60,360,1440").split(",") if x.strip()]
RETRY_QUEUE_INTERVAL_MINUTES = _env_int("RETRY_QUEUE_INTERVAL_MINUTES", 15)

# ── Auto-upgrade ──────────────────────────────────────────────────────────────
# Periodically check for higher-quality cached releases and upgrade existing strm.
AUTO_UPGRADE_ENABLED = _env("AUTO_UPGRADE_ENABLED", "true").lower() in ("1", "true", "yes")
AUTO_UPGRADE_INTERVAL_HOURS = _env_int("AUTO_UPGRADE_INTERVAL_HOURS", 24)

# Re-search "wanted" movies (no acceptable release found yet) on this interval.
WANTED_RECHECK_INTERVAL_HOURS = _env_int("WANTED_RECHECK_INTERVAL_HOURS", 12)

# ── Season pack consolidation ─────────────────────────────────────────────────
# When a season is complete, try to swap N per-episode torrents for 1 season pack.
SEASON_PACK_CONSOLIDATION_ENABLED = _env("SEASON_PACK_CONSOLIDATION_ENABLED", "true").lower() in ("1","true","yes")
SEASON_PACK_CHECK_INTERVAL_HOURS = _env_int("SEASON_PACK_CHECK_INTERVAL_HOURS", 12)

# ── Trending pre-cache ────────────────────────────────────────────────────────
# Auto-add TMDB trending movies if cached on TorBox. 0 disables.
TRENDING_PRECACHE_COUNT = _env_int("TRENDING_PRECACHE_COUNT", 0)
TRENDING_CHECK_INTERVAL_HOURS = _env_int("TRENDING_CHECK_INTERVAL_HOURS", 24)

# Optional extra auto-add categories. 0 disables each.
TRENDING_TV_COUNT = _env_int("TRENDING_TV_COUNT", 0)
POPULAR_MOVIE_COUNT = _env_int("POPULAR_MOVIE_COUNT", 0)
POPULAR_TV_COUNT = _env_int("POPULAR_TV_COUNT", 0)
NETFLIX_NL_TOP_COUNT = _env_int("NETFLIX_NL_TOP_COUNT", 0)
PRIME_NL_TOP_COUNT = _env_int("PRIME_NL_TOP_COUNT", 0)
DISNEY_NL_TOP_COUNT = _env_int("DISNEY_NL_TOP_COUNT", 0)
AUTO_ADD_MIN_RATING = float(_env("AUTO_ADD_MIN_RATING", "6.0"))
AUTO_ADD_MIN_VOTES = _env_int("AUTO_ADD_MIN_VOTES", 100)
AUTO_ADD_REGION = _env("AUTO_ADD_REGION", "NL")

# ── Radarr / Sonarr import ────────────────────────────────────────────────────
RADARR_URL = _env("RADARR_URL", "")
RADARR_API_KEY = _env("RADARR_API_KEY", "")
SONARR_URL = _env("SONARR_URL", "")
SONARR_API_KEY = _env("SONARR_API_KEY", "")

# ── Health-aware processing ───────────────────────────────────────────────────
# Cache health status for this many seconds; skip services that recently failed.
HEALTH_CACHE_SECONDS = _env_int("HEALTH_CACHE_SECONDS", 60)

# ── OpenSubtitles ─────────────────────────────────────────────────────────────
OPENSUBTITLES_API_KEY = _env("OPENSUBTITLES_API_KEY", "")
OPENSUBTITLES_USER_AGENT = _env("OPENSUBTITLES_USER_AGENT", "Mycelium v1.0")
OPENSUBTITLES_LANGUAGES = [l.strip().lower() for l in _env("OPENSUBTITLES_LANGUAGES", "").split(",") if l.strip()]

# ── Continue-watching priority ────────────────────────────────────────────────
CONTINUE_WATCHING_INTERVAL_MINUTES = _env_int("CONTINUE_WATCHING_INTERVAL_MINUTES", 60)

# ── TorBox quota warning ──────────────────────────────────────────────────────
# Disabled by default — TorBox paid plans don't have hard storage limits.
# Set QUOTA_CHECK_INTERVAL_HOURS > 0 to enable.
QUOTA_WARN_TORRENT_COUNT = _env_int("QUOTA_WARN_TORRENT_COUNT", 999999)
QUOTA_WARN_SIZE_GB = _env_int("QUOTA_WARN_SIZE_GB", 999999)
QUOTA_CHECK_INTERVAL_HOURS = _env_int("QUOTA_CHECK_INTERVAL_HOURS", 0)

# ── Multi-debrid (RealDebrid fallback) ────────────────────────────────────────
MULTI_DEBRID_ENABLED = _env("MULTI_DEBRID_ENABLED", "false").lower() in ("1", "true", "yes")
REALDEBRID_API_KEY = _env("REALDEBRID_API_KEY", "")
REALDEBRID_BASE_URL = _env("REALDEBRID_BASE_URL", "https://api.real-debrid.com/rest/1.0")

# ── WebDAV server (Plex / Emby compatibility) ─────────────────────────────────
# When enabled, serves the .strm library as virtual .mkv files at /dav/...
# Mount via davfs2 on the DSM host so Plex (or any other media server) can
# scan and stream from a normal-looking filesystem path.
WEBDAV_ENABLED = _env("WEBDAV_ENABLED", "false").lower() in ("1", "true", "yes")
WEBDAV_PATH_PREFIX = _env("WEBDAV_PATH_PREFIX", "/dav")
WEBDAV_URL_CACHE_TTL_SECONDS = _env_int("WEBDAV_URL_CACHE_TTL_SECONDS", 3600)

# ── Authentication (opt-in) ──────────────────────────────────────────────────
# When AUTH_ENABLED is true the dashboard and JSON APIs require a session
# cookie. Set the password via the setup wizard or the Settings tab; on
# first login a plain AUTH_PASSWORD is upgraded to a scrypt hash and the
# plain copy is wiped.
AUTH_ENABLED = _env("AUTH_ENABLED", "false").lower() in ("1", "true", "yes")
AUTH_USERNAME = _env("AUTH_USERNAME", "admin")
AUTH_PASSWORD = _env("AUTH_PASSWORD", "")  # plain (will be upgraded), or empty
AUTH_SESSION_SECRET = _env("AUTH_SESSION_SECRET", "mycelium-please-change-me")
COOKIE_SECURE = _env("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")

# Trust an upstream proxy that does auth (Authelia / Authentik / Traefik /
# Cloudflare Access). Only honoured when the request originates from a
# trusted network so headers can't be spoofed from the public internet.
TRUSTED_PROXY_AUTH = _env("TRUSTED_PROXY_AUTH", "false").lower() in ("1", "true", "yes")
TRUSTED_PROXY_USER_HEADER = _env("TRUSTED_PROXY_USER_HEADER", "X-Forwarded-User")
TRUSTED_PROXY_NETWORKS = _env(
    "TRUSTED_PROXY_NETWORKS",
    "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
)

# ── OIDC (native single sign-on, opt-in) ──────────────────────────────────────
# Works with Authelia, Authentik, Keycloak, Google Workspace, Auth0, Okta, etc.
# Register a redirect URI of <public-url>/oidc/callback at your provider.
OIDC_ENABLED = _env("OIDC_ENABLED", "false").lower() in ("1", "true", "yes")
OIDC_ISSUER_URL = _env("OIDC_ISSUER_URL", "")
OIDC_CLIENT_ID = _env("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = _env("OIDC_CLIENT_SECRET", "")
OIDC_SCOPES = _env("OIDC_SCOPES", "openid email profile")
OIDC_USER_CLAIM = _env("OIDC_USER_CLAIM", "preferred_username")
OIDC_PROVIDER_NAME = _env("OIDC_PROVIDER_NAME", "SSO")


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
