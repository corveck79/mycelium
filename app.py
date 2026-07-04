import hmac
import json as _json
import logging
import os.path as _path
import re
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, stream_with_context, url_for

import backup
import catbox
import nfo_generator
import catchup
import cleanup
import config as cfg
import continue_watching
import db
import health
import jellyfin
import library_sync
import log_buffer
import monitor
import notify
import processor
import recovery
import retry_queue
import stats
import strm_generator
import tmdb
import torbox
import torrentio
import trending
import upgrader
import watchdog
import zilean
from config import (
    AUTO_UPGRADE_ENABLED,
    AUTO_UPGRADE_INTERVAL_HOURS,
    BACKUP_INTERVAL_HOURS,
    CATBOX_GC_INTERVAL_MINUTES,
    CATBOX_MODE,
    CATCHUP_ENABLED,
    CLEANUP_INTERVAL_HOURS,
    CONTINUE_WATCHING_INTERVAL_MINUTES,
    LISTEN_HOST,
    LISTEN_PORT,
    MERGE_VERSIONS_INTERVAL_HOURS,
    METRICS_TOKEN,
    MONITOR_INTERVAL_HOURS,
    MOVIE_SYNC_INTERVAL_MINUTES,
    QUOTA_CHECK_INTERVAL_HOURS,
    QUOTA_WARN_SIZE_GB,
    QUOTA_WARN_TORRENT_COUNT,
    RETRY_QUEUE_INTERVAL_MINUTES,
    SEASON_PACK_CHECK_INTERVAL_HOURS,
    SEASON_PACK_CONSOLIDATION_ENABLED,
    STRM_GENERATOR_INTERVAL_HOURS,
    TRENDING_CHECK_INTERVAL_HOURS,
    TRENDING_PRECACHE_COUNT,
    WEBHOOK_SECRET,
    configure_logging,
)
from webhook_parser import IgnoreEvent, MediaRequest, WebhookError, parse

configure_logging()
log_buffer.install()
log = logging.getLogger("mycelium")

APP_VERSION = "0.5.2"

with open(_path.join(_path.dirname(__file__), "releases.json"), encoding="utf-8") as _f:
    RELEASES: list[dict] = _json.load(_f)

db.init()

import settings as _settings_mod
import os as _os
LITE_MODE: bool = (
    _settings_mod.get("LITE_MODE", False)
    or _os.getenv("LITE_MODE", "").lower() in ("1", "true", "yes")
)
if LITE_MODE:
    log.info("LITE_MODE enabled  -  heavy background schedulers and startup tasks disabled")

app = Flask(__name__, static_folder="static", static_url_path="/static")
if cfg.AUTH_SESSION_SECRET == "mycelium-please-change-me":
    import warnings
    warnings.warn(
        "AUTH_SESSION_SECRET is still the default value. "
        "Set a random string in your environment for production use.",
        stacklevel=1,
    )
import secrets as _secrets_mod
if not WEBHOOK_SECRET:
    _stored = _settings_mod.get("WEBHOOK_SECRET_AUTO", "")
    if not _stored:
        _stored = _secrets_mod.token_urlsafe(32)
        _settings_mod.set("WEBHOOK_SECRET_AUTO", _stored)
        log.info("Auto-generated WEBHOOK_SECRET - copy from Admin > Settings > Webhooks to your Seerr config")
    else:
        log.debug("Using auto-generated WEBHOOK_SECRET from settings")
app.secret_key = cfg.AUTH_SESSION_SECRET
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = cfg.COOKIE_SECURE

# CSRF protection on all state-changing endpoints; external webhooks opt out below.
from flask_wtf.csrf import CSRFProtect, generate_csrf
_csrf = CSRFProtect(app)


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


@app.context_processor
def _inject_csrf_token():
    return {"csrf_token": generate_csrf}


# Rate limiter  -  applied selectively to auth endpoints.
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],  # opt-in per route
    storage_uri="memory://",
)

import plugin_loader
if not LITE_MODE:
    plugin_loader.load_all(app)

import auth
import oidc
auth.install_before_request(app)
oidc.install(app)


@app.get("/login")
def login_view():
    return render_template("login.html",
                            error=request.args.get("error"),
                            next=request.args.get("next", ""),
                            oidc_enabled=oidc.is_enabled(),
                            oidc_provider=oidc.provider_name(),
                            password_enabled=bool(cfg.AUTH_ENABLED or
                                                   __import__("settings").get("AUTH_PASSWORD_HASH", "")))


@app.post("/login")
@limiter.limit("5 per minute; 30 per hour")
def login_submit():
    from flask import session as _session
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    nxt = request.form.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if auth.attempt_login(username, password):
        _session["user"] = username
        return redirect(nxt)
    return redirect(url_for("login_view", error="1", next=nxt))


@app.get("/logout")
def logout_view():
    from flask import session as _session
    _session.clear()
    return redirect(url_for("login_view"))


@app.post("/ui/set-password")
@auth.require_auth
def ui_set_password():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    new_pw = request.form.get("password") or ""
    if len(new_pw) < 6:
        flash("Password must be at least 6 characters", "err")
        return redirect(url_for("ui_dashboard") + "#settings")
    auth.set_password(new_pw)
    flash("Password updated", "ok")
    return redirect(url_for("ui_dashboard") + "#settings")


@app.post("/ui/api/me/password")
@auth.require_auth
def ui_api_me_password():
    """Let users change their own password."""
    rec = auth.current_user_record()
    if not rec or not rec.get("id"):
        return jsonify(error="not authenticated"), 401
    p = request.get_json(silent=True) or {}
    current = p.get("current", "")
    new_pw = p.get("password", "")
    if len(new_pw) < 6:
        return jsonify(error="Password must be at least 6 characters"), 400
    if not auth._verify_hashed(current, rec.get("password_hash", "")):
        return jsonify(error="Current password is incorrect"), 400
    auth.change_user_password(rec["id"], new_pw)
    return jsonify(ok=True)


def _start_scheduler() -> BackgroundScheduler:
    # job_defaults: every interval job gets +/-60s jitter to avoid stampede when
    # multiple long-running jobs hit the same minute mark.
    scheduler = BackgroundScheduler(
        daemon=True,
        job_defaults={"jitter": 60, "coalesce": True, "max_instances": 1},
    )

    if MONITOR_INTERVAL_HOURS > 0:
        scheduler.add_job(
            monitor.run_series_check,
            trigger="interval", hours=MONITOR_INTERVAL_HOURS,
            id="series_monitor", next_run_time=None,
        )
        log.info("Scheduled series monitor every %dh", MONITOR_INTERVAL_HOURS)

    import seerr as _seerr
    if MOVIE_SYNC_INTERVAL_MINUTES > 0 and _seerr.is_configured():
        scheduler.add_job(
            monitor.sync_movies,
            trigger="interval", minutes=MOVIE_SYNC_INTERVAL_MINUTES,
            id="movie_sync", next_run_time=None,
        )
        scheduler.add_job(
            monitor.sync_series,
            trigger="interval", minutes=MOVIE_SYNC_INTERVAL_MINUTES,
            id="series_sync", next_run_time=None,
        )
        log.info("Scheduled Seerr movie+series sync every %dm", MOVIE_SYNC_INTERVAL_MINUTES)
    elif MOVIE_SYNC_INTERVAL_MINUTES > 0:
        log.info("Seerr sync skipped  -  SEERR_URL not configured (using SPA discovery instead)")

    if STRM_GENERATOR_INTERVAL_HOURS > 0:
        scheduler.add_job(
            strm_generator.run_and_refresh,
            trigger="interval", hours=STRM_GENERATOR_INTERVAL_HOURS,
            id="strm_generator", next_run_time=None,
        )
        log.info("Scheduled strm generator every %dh", STRM_GENERATOR_INTERVAL_HOURS)

    if CLEANUP_INTERVAL_HOURS > 0:
        scheduler.add_job(
            cleanup.run_cleanup,
            trigger="interval", hours=CLEANUP_INTERVAL_HOURS,
            id="strm_cleanup", next_run_time=None,
        )
        log.info("Scheduled strm cleanup every %dh", CLEANUP_INTERVAL_HOURS)

    if CATBOX_MODE:
        scheduler.add_job(
            strm_generator.repair_expired_strms,
            trigger="interval", hours=6,
            id="strm_repair", next_run_time=None,
        )
        log.info("Scheduled automatic .strm repair every 6h")

    if CATBOX_MODE and CATBOX_GC_INTERVAL_MINUTES > 0:
        scheduler.add_job(
            catbox.release_idle,
            trigger="interval", minutes=CATBOX_GC_INTERVAL_MINUTES,
            id="catbox_gc", next_run_time=None,
        )
        log.info("Scheduled Catbox GC every %dm (idle threshold %dm)",
                 CATBOX_GC_INTERVAL_MINUTES, cfg.CATBOX_IDLE_MINUTES)

    if BACKUP_INTERVAL_HOURS > 0:
        scheduler.add_job(
            backup.run,
            trigger="interval", hours=BACKUP_INTERVAL_HOURS,
            id="db_backup", next_run_time=None,
        )
        log.info("Scheduled DB backup every %dh", BACKUP_INTERVAL_HOURS)

    if RETRY_QUEUE_INTERVAL_MINUTES > 0:
        scheduler.add_job(
            retry_queue.run_due,
            trigger="interval", minutes=RETRY_QUEUE_INTERVAL_MINUTES,
            id="retry_queue", next_run_time=None,
        )
        log.info("Scheduled retry queue every %dm", RETRY_QUEUE_INTERVAL_MINUTES)

    # Probe CDN files for Plex stubs that have no track info yet (duration, audio, subs).
    # Runs every 30 min in a background thread to avoid blocking the scheduler.
    # build_fsh=False: only ffprobe, no 32MB download per file.
    if cfg.SPORE_ENABLED:
        def _run_probe_pending():
            import threading as _t
            _t.Thread(
                target=strm_generator.probe_pending_stubs,
                daemon=True,
                name="probe-pending-stubs",
            ).start()

        scheduler.add_job(
            _run_probe_pending,
            trigger="interval", minutes=30,
            id="probe_pending_stubs",
            next_run_time=None,
        )
        log.info("Scheduled probe_pending_stubs every 30m")

    if not LITE_MODE:
        if AUTO_UPGRADE_ENABLED and AUTO_UPGRADE_INTERVAL_HOURS > 0:
            scheduler.add_job(
                upgrader.run_auto_upgrade,
                trigger="interval", hours=AUTO_UPGRADE_INTERVAL_HOURS,
                id="auto_upgrade", next_run_time=None,
            )
            log.info("Scheduled auto-upgrade every %dh", AUTO_UPGRADE_INTERVAL_HOURS)

        if SEASON_PACK_CONSOLIDATION_ENABLED and SEASON_PACK_CHECK_INTERVAL_HOURS > 0:
            scheduler.add_job(
                upgrader.run_pack_consolidation,
                trigger="interval", hours=SEASON_PACK_CHECK_INTERVAL_HOURS,
                id="pack_consolidation", next_run_time=None,
            )
            log.info("Scheduled season-pack consolidation every %dh", SEASON_PACK_CHECK_INTERVAL_HOURS)

        if getattr(cfg, "WANTED_RECHECK_INTERVAL_HOURS", 0) > 0:
            scheduler.add_job(
                upgrader.recheck_wanted,
                trigger="interval", hours=cfg.WANTED_RECHECK_INTERVAL_HOURS,
                id="wanted_recheck", next_run_time=None,
            )
            log.info("Scheduled wanted-movie recheck every %dh", cfg.WANTED_RECHECK_INTERVAL_HOURS)

        _auto_add_total = (
            TRENDING_PRECACHE_COUNT
            + getattr(cfg, "TRENDING_TV_COUNT", 0)
            + getattr(cfg, "POPULAR_MOVIE_COUNT", 0)
            + getattr(cfg, "POPULAR_TV_COUNT", 0)
            + getattr(cfg, "NETFLIX_NL_TOP_COUNT", 0)
            + getattr(cfg, "PRIME_NL_TOP_COUNT", 0)
            + getattr(cfg, "DISNEY_NL_TOP_COUNT", 0)
        )
        if _auto_add_total > 0 and TRENDING_CHECK_INTERVAL_HOURS > 0:
            scheduler.add_job(
                trending.run,
                trigger="interval", hours=TRENDING_CHECK_INTERVAL_HOURS,
                id="trending_precache", next_run_time=None,
            )
            log.info("Scheduled auto-add every %dh (total slots: %d)",
                     TRENDING_CHECK_INTERVAL_HOURS, _auto_add_total)

        if CONTINUE_WATCHING_INTERVAL_MINUTES > 0:
            scheduler.add_job(
                continue_watching.prioritize_next_episodes,
                trigger="interval", minutes=CONTINUE_WATCHING_INTERVAL_MINUTES,
                id="continue_watching", next_run_time=None,
            )
            log.info("Scheduled continue-watching priority every %dm", CONTINUE_WATCHING_INTERVAL_MINUTES)

        if MERGE_VERSIONS_INTERVAL_HOURS > 0:
            scheduler.add_job(
                jellyfin.merge_duplicate_versions,
                trigger="interval", hours=MERGE_VERSIONS_INTERVAL_HOURS,
                id="merge_versions", next_run_time=None,
            )
            log.info("Scheduled MergeVersions every %dh", MERGE_VERSIONS_INTERVAL_HOURS)

    if QUOTA_CHECK_INTERVAL_HOURS > 0:
        scheduler.add_job(
            lambda: torbox.check_quota_and_warn(QUOTA_WARN_TORRENT_COUNT, QUOTA_WARN_SIZE_GB),
            trigger="interval", hours=QUOTA_CHECK_INTERVAL_HOURS,
            id="quota_warn", next_run_time=None,
        )
        log.info("Scheduled TorBox quota check every %dh", QUOTA_CHECK_INTERVAL_HOURS)

    # Watchdogs + maintenance
    scheduler.add_job(watchdog.deadman_check, trigger="interval", hours=2,
                       id="deadman", next_run_time=None, max_instances=1)
    scheduler.add_job(watchdog.disk_check, trigger="interval", hours=1,
                       id="disk_check", next_run_time=None, max_instances=1)
    # Aggressive pruning so volatile tables don't grow unbounded between scrapes.
    scheduler.add_job(lambda: db.prune_old(14), trigger="interval", hours=6,
                       id="prune_old", next_run_time=None, max_instances=1)
    scheduler.add_job(lambda: db.prune_webhook_events(24), trigger="interval", hours=6,
                       id="prune_webhooks", next_run_time=None, max_instances=1)
    scheduler.add_job(db.vacuum, trigger="interval", hours=24 * 7,
                       id="vacuum", next_run_time=None, max_instances=1)
    log.info("Scheduled watchdogs: deadman/2h, disk/1h, prune/24h, vacuum/weekly")

    # Apply max_instances=1 to all overlap-sensitive jobs already added
    for jid in ("strm_generator", "strm_cleanup", "series_monitor", "movie_sync",
                 "retry_queue", "auto_upgrade", "pack_consolidation",
                 "trending_precache", "continue_watching", "db_backup",
                 "catbox_gc", "merge_versions", "quota_warn"):
        try:
            scheduler.modify_job(jid, max_instances=1)
        except Exception as exc:
            # Job may not exist if a feature is disabled  -  that's expected.
            log.debug("modify_job(%s): %s", jid, exc)

    scheduler.start()
    return scheduler


scheduler = _start_scheduler()
if not LITE_MODE:
    plugin_loader.register_jobs(scheduler)

if cfg.SPORE_ENABLED:
    try:
        import spore_server
        spore_server.start(port=cfg.SPORE_PORT)
    except Exception as _spore_exc:
        log.warning("Mycelium Spore server failed to start: %s", _spore_exc)

# Fast-start cache in dedicated subdir so media root stays clean
_fsh_cache_dir = cfg.SPORE_MEDIA_PATH + "/.fsh"
try:
    import mp4_faststart
    mp4_faststart.init(_fsh_cache_dir)
    log.info("MP4 fast-start cache dir: %s", _fsh_cache_dir)
except Exception as _fsh_exc:
    log.warning("MP4 fast-start init failed: %s", _fsh_exc)

if CATCHUP_ENABLED:
    catchup.schedule()

def _backfill_tmdb_ids() -> None:
    """Resolve tmdb_id for requests that only have imdb_id (e.g. Seerr imports)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, imdb_id, media_type FROM requests WHERE tmdb_id IS NULL AND imdb_id IS NOT NULL"
        ).fetchall()
    if not rows:
        return
    log.info("Backfilling tmdb_id for %d requests", len(rows))
    filled = 0
    for row in rows:
        kind = "tv" if row["media_type"] == "tv" else "movie"
        data = tmdb._get(f"/find/{row['imdb_id']}", params={"external_source": "imdb_id"})
        if not data:
            continue
        results = data.get(f"{kind}_results") or []
        if not results:
            other = "movie" if kind == "tv" else "tv"
            results = data.get(f"{other}_results") or []
        if results:
            tid = results[0].get("id")
            if tid:
                with db._connect() as conn:
                    conn.execute("UPDATE requests SET tmdb_id = ? WHERE id = ?", (tid, row["id"]))
                    conn.commit()
                filled += 1
    log.info("Backfilled tmdb_id for %d/%d requests", filled, len(rows))


# Kick off initial movie sync + strm scan ~10s after startup so /health
# answers fast on cold start and the scheduler isn't elbow-to-elbow with
# the wizard's first request.
def _delayed(seconds: float, target, name: str) -> None:
    def _run():
        import time as _t
        _t.sleep(seconds)
        try:
            target()
        except Exception:
            log.exception("startup task %s failed", name)
    threading.Thread(target=_run, name=name, daemon=True).start()


_delayed(15.0, monitor.sync_movies, "movie-sync-init")
_delayed(20.0, monitor.sync_series, "series-sync-init")
_delayed(30.0, strm_generator.run_and_refresh, "strm-init")
_delayed(60.0, library_sync.resolve_unknowns, "resolve-unknowns-init")
_delayed(90.0, library_sync.import_series_to_monitored, "series-monitored-init")
_delayed(120.0, nfo_generator.generate_all, "nfo-init")
_delayed(150.0, nfo_generator.fetch_local_images, "images-init")
_delayed(45.0, _backfill_tmdb_ids, "tmdb-id-backfill")


# ── Auth ──────────────────────────────────────────────────────────────────────

def _effective_webhook_secret() -> str:
    """Return the active webhook secret: env var takes priority, else auto-generated."""
    return WEBHOOK_SECRET or _settings_mod.get("WEBHOOK_SECRET_AUTO", "")


def _check_auth() -> None:
    secret = _effective_webhook_secret()
    if not secret:
        return
    header_secret = request.headers.get("X-Webhook-Secret")
    query_secret  = request.args.get("secret")
    provided = header_secret or query_secret
    if query_secret and not header_secret:
        # Deprecated: secret in query string leaks via access logs and proxy history.
        # Migrate to the X-Webhook-Secret header.
        log.warning("Webhook secret passed via ?secret= query param from %s"
                    " - migrate to X-Webhook-Secret header", request.remote_addr)
    if provided != secret:
        log.warning("Rejected webhook with bad/missing secret from %s", request.remote_addr)
        abort(401)


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health_simple():
    """Liveness probe used by Docker HEALTHCHECK  -  process up + DB reachable."""
    try:
        db.get_recent(1)
        return jsonify(status="ok")
    except Exception:
        return jsonify(status="degraded"), 503


@app.get("/metrics")
def metrics_export():
    """Prometheus scrape endpoint. Requires admin session or X-Metrics-Token header."""
    if METRICS_TOKEN:
        provided = request.headers.get("X-Metrics-Token") or request.args.get("metrics_token")
        if not provided or not hmac.compare_digest(provided, METRICS_TOKEN):
            abort(401)
    elif not auth.is_admin():
        abort(401)
    import metrics_prom
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    metrics_prom.refresh_gauges()
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.get("/healthz")
def health_deep():
    """Real readiness probe. Returns 503 if DB unreachable OR both scrapers down."""
    import health_cache
    status = "ok"
    failures = []
    try:
        db.get_recent(1)
    except Exception as exc:
        status = "down"
        failures.append(f"db: {exc}")
    zilean_ok = health_cache.is_up("zilean")
    torrentio_ok = health_cache.is_up("torrentio")
    if not zilean_ok and not torrentio_ok:
        status = "down"
        failures.append("no scraper reachable")
    code = 200 if status == "ok" else 503
    return jsonify(status=status, failures=failures,
                    zilean=zilean_ok, torrentio=torrentio_ok), code


@app.post("/webhook")
@_csrf.exempt
def webhook():
    _check_auth()
    payload = request.get_json(silent=True) or {}
    log.info("Received webhook: notification_type=%s subject=%s",
             payload.get("notification_type"), payload.get("subject"))
    try:
        media_request = parse(payload)
    except IgnoreEvent as exc:
        log.info("Ignoring event: %s", exc)
        return jsonify(status="ignored", reason=str(exc))
    except WebhookError as exc:
        log.error("Bad webhook payload: %s", exc)
        return jsonify(status="error", error=str(exc)), 400

    # Idempotency: dedup by imdb_id + media_type + seasons within DB
    dedup_key = f"{media_request.imdb_id}:{media_request.media_type}:{','.join(map(str, media_request.seasons))}"
    if db.webhook_seen(dedup_key):
        log.info("Webhook duplicate ignored: %s", dedup_key)
        return jsonify(status="duplicate", imdb_id=media_request.imdb_id), 200

    thread = threading.Thread(
        target=processor.process,
        args=(media_request,),
        name=f"process-{media_request.imdb_id}",
        daemon=True,
    )
    thread.start()
    return jsonify(status="accepted", imdb_id=media_request.imdb_id, title=media_request.title), 202


@app.post("/torbox-webhook")
@_csrf.exempt
def torbox_webhook():
    """Endpoint for TorBox to push completion notifications.
    Triggers strm_generator to catch the newly-ready torrent."""
    _check_auth()
    payload = request.get_json(silent=True) or {}
    log.info("TorBox webhook: %s", payload)
    threading.Thread(target=strm_generator.run_and_refresh, name="torbox-push", daemon=True).start()
    return jsonify(status="ok")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/admin")
def ui_dashboard():
    import settings as _settings
    if not _settings.get("SETUP_COMPLETE", False):
        return redirect(url_for("setup_wizard"))
    return render_template(
        "ui.html",
        repair_items=db.get_repair_items(200),
        last_cleanup=db.get_last_cleanup_run(),
        activity=db.get_activity(50),
        config=cfg,
        app_version=APP_VERSION,
        releases=RELEASES,
    )


@app.get("/ui")
def ui_redirect():
    return redirect("/admin", code=301)


# ── Setup wizard ──────────────────────────────────────────────────────────────

@app.get("/setup")
def setup_wizard():
    import settings as _settings
    # First run: no users yet - always allow
    if db.user_count() == 0:
        return render_template("setup.html")
    # After first run: require admin login
    if not auth.is_admin():
        return redirect(url_for("login_view", next="/setup?rerun=1"))
    if _settings.get("SETUP_COMPLETE", False) and request.args.get("rerun") != "1":
        return redirect(url_for("ui_dashboard"))
    return render_template("setup.html")


@app.post("/setup/skip")
@limiter.limit("10 per minute")
def setup_skip():
    if db.user_count() > 0 and not auth.is_admin():
        return jsonify(error="unauthorized"), 401
    import settings as _settings
    _settings.set("SETUP_COMPLETE", True)
    return jsonify(ok=True)


@app.post("/setup/save")
@limiter.limit("10 per minute")
def setup_save():
    if db.user_count() > 0 and not auth.is_admin():
        return jsonify(error="unauthorized"), 401
    import settings as _settings
    _allowed_keys = {k for g in _settings.SETTING_GROUPS for k in g["keys"]} | {"SETUP_COMPLETE"}
    saved = 0
    for key, value in request.form.items():
        if key not in _allowed_keys:
            log.warning("setup_save: rejected unknown key %r", key)
            continue
        # Treat empty strings as "clear override"
        if value == "":
            _settings.set(key, None)
        elif key in _settings._BOOL_KEYS:
            _settings.set(key, str(value).lower() in ("1", "true", "yes", "on"))
        else:
            _settings.set(key, value)
        saved += 1
    _settings.set("SETUP_COMPLETE", True)
    log.info("Setup wizard saved %d settings", saved)
    return jsonify(ok=True, saved=saved)


@app.post("/setup/test/<kind>")
@limiter.limit("20 per minute")
def setup_test(kind: str):
    """Test a single integration using values posted from the wizard form."""
    if db.user_count() > 0 and not auth.is_admin():
        return jsonify(error="unauthorized"), 401
    f = request.form
    try:
        if kind == "torbox":
            api_key = (f.get("TORBOX_API_KEY") or "").strip()
            base = (f.get("TORBOX_BASE_URL") or cfg.TORBOX_BASE_URL).rstrip("/")
            if not api_key:
                return jsonify(ok=False, error="API key empty")
            r = __import__("requests").get(
                f"{base}/torrents/mylist",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=8,
            )
            return jsonify(ok=r.status_code < 400, detail=f"HTTP {r.status_code}")

        if kind == "jellyfin":
            url = (f.get("JELLYFIN_URL") or "").rstrip("/")
            api_key = (f.get("JELLYFIN_API_KEY") or "").strip()
            if not url:
                return jsonify(ok=False, error="URL empty")
            hdr = {"X-Emby-Token": api_key} if api_key else {}
            r = __import__("requests").get(f"{url}/System/Info/Public", headers=hdr, timeout=6)
            return jsonify(ok=r.status_code < 400,
                            detail=(r.json() or {}).get("ServerName", "reachable") if r.headers.get("content-type", "").startswith("application/json") else "reachable")

        if kind == "seerr":
            url = (f.get("SEERR_URL") or "").rstrip("/")
            api_key = (f.get("SEERR_API_KEY") or "").strip()
            if not url:
                return jsonify(ok=False, error="URL empty")
            hdr = {"X-Api-Key": api_key} if api_key else {}
            r = __import__("requests").get(f"{url}/api/v1/status", headers=hdr, timeout=6)
            return jsonify(ok=r.status_code < 400, detail=f"HTTP {r.status_code}")

        if kind == "discord":
            url = (f.get("DISCORD_WEBHOOK_URL") or "").strip()
            if not url:
                return jsonify(ok=False, error="URL empty")
            r = __import__("requests").post(
                url, json={"content": "🧪 Mycelium setup test"}, timeout=6,
            )
            return jsonify(ok=r.status_code < 400, detail=f"HTTP {r.status_code}")

        if kind == "telegram":
            tok = (f.get("TELEGRAM_BOT_TOKEN") or "").strip()
            chat = (f.get("TELEGRAM_CHAT_ID") or "").strip()
            if not tok or not chat:
                return jsonify(ok=False, error="token or chat empty")
            r = __import__("requests").post(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                json={"chat_id": chat, "text": "🧪 Mycelium setup test"},
                timeout=6,
            )
            return jsonify(ok=r.status_code < 400, detail=f"HTTP {r.status_code}")

        if kind == "trakt":
            client_id = (f.get("TRAKT_CLIENT_ID") or "").strip()
            if not client_id:
                return jsonify(ok=False, error="Client ID empty")
            r = __import__("requests").get(
                "https://api.trakt.tv/movies/trending",
                headers={"trakt-api-key": client_id, "trakt-api-version": "2"},
                timeout=8,
            )
            return jsonify(ok=r.status_code < 400, detail=f"HTTP {r.status_code}")

        if kind == "opensubtitles":
            api_key = (f.get("OPENSUBTITLES_API_KEY") or "").strip()
            if not api_key:
                return jsonify(ok=False, error="API key empty")
            r = __import__("requests").get(
                "https://api.opensubtitles.com/api/v1/infos/user",
                headers={"Api-Key": api_key, "Content-Type": "application/json"},
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json().get("data", {})
                remaining = data.get("remaining_downloads", "?")
                return jsonify(ok=True, detail=f"OK  -  {remaining} downloads remaining today")
            return jsonify(ok=r.status_code < 400, detail=f"HTTP {r.status_code}")

        if kind == "zilean":
            url = (f.get("ZILEAN_URL") or "").rstrip("/")
            if not url:
                return jsonify(ok=False, error="URL empty")
            r = __import__("requests").get(f"{url}/healthcheck", timeout=6)
            return jsonify(ok=r.status_code < 400, detail=f"HTTP {r.status_code}")

        if kind == "radarr":
            url = (f.get("RADARR_URL") or "").rstrip("/")
            api_key = (f.get("RADARR_API_KEY") or "").strip()
            if not url:
                return jsonify(ok=False, error="URL empty")
            r = __import__("requests").get(
                f"{url}/api/v3/system/status",
                headers={"X-Api-Key": api_key} if api_key else {},
                timeout=6,
            )
            if r.status_code < 400:
                version = r.json().get("version", "")
                return jsonify(ok=True, detail=f"Radarr {version}")
            return jsonify(ok=False, detail=f"HTTP {r.status_code}")

        if kind == "sonarr":
            url = (f.get("SONARR_URL") or "").rstrip("/")
            api_key = (f.get("SONARR_API_KEY") or "").strip()
            if not url:
                return jsonify(ok=False, error="URL empty")
            r = __import__("requests").get(
                f"{url}/api/v3/system/status",
                headers={"X-Api-Key": api_key} if api_key else {},
                timeout=6,
            )
            if r.status_code < 400:
                version = r.json().get("version", "")
                return jsonify(ok=True, detail=f"Sonarr {version}")
            return jsonify(ok=False, detail=f"HTTP {r.status_code}")

        return jsonify(ok=False, error="unknown test"), 400
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)[:120])


@app.post("/ui/submit")
def ui_submit():
    imdb_id = (request.form.get("imdb_id") or "").strip()
    media_type = request.form.get("media_type", "movie")
    seasons_raw = request.form.get("seasons", "1")

    if not re.fullmatch(r"tt\d{6,10}", imdb_id):
        flash("Invalid IMDB ID  -  must be tt followed by 6-10 digits.", "err")
        return redirect(url_for("ui_dashboard"))
    if media_type not in ("movie", "series"):
        flash("Invalid media type.", "err")
        return redirect(url_for("ui_dashboard"))

    seasons = [int(s.strip()) for s in re.split(r"[,\s]+", seasons_raw) if s.strip().isdigit()]
    if media_type == "series" and not seasons:
        seasons = [1]

    display_title = tmdb.display_title(imdb_id, media_type) or imdb_id
    media_request = MediaRequest(
        title=display_title, media_type=media_type, imdb_id=imdb_id, seasons=seasons,
    )
    threading.Thread(target=processor.process, args=(media_request,),
                     name=f"manual-{imdb_id}", daemon=True).start()
    flash(f"Queued: {display_title} ({media_type})", "ok")
    return redirect(url_for("ui_dashboard"))


@app.post("/ui/search-episode")
def ui_search_episode():
    imdb_id = request.form.get("imdb_id", "")
    title = request.form.get("title", imdb_id)
    season = int(request.form.get("season", 1))
    episode = int(request.form.get("episode", 1))
    threading.Thread(
        target=monitor.search_episode_now,
        args=(imdb_id, title, season, episode),
        name=f"ep-{imdb_id}-s{season}e{episode}", daemon=True,
    ).start()
    flash(f"Searching {title} S{season:02d}E{episode:02d}…", "ok")
    return redirect(url_for("ui_dashboard") + "#wanted")


@app.post("/ui/download-movie")
def ui_download_movie():
    imdb_id = request.form.get("imdb_id", "")
    display_title = tmdb.display_title(imdb_id, "movie") or imdb_id
    media_request = MediaRequest(
        title=display_title, media_type="movie", imdb_id=imdb_id, seasons=[],
    )
    db.update_media_item_status(imdb_id, "movie", "processing")
    threading.Thread(target=processor.process, args=(media_request,),
                     name=f"movie-{imdb_id}", daemon=True).start()
    flash(f"Download queued for {display_title}", "ok")
    return redirect(url_for("ui_dashboard") + "#movies")


@app.post("/ui/sync-movies")
def ui_sync_movies():
    threading.Thread(target=monitor.sync_movies, name="movie-sync-manual", daemon=True).start()
    flash("Movie sync started", "ok")
    return redirect(url_for("ui_dashboard") + "#movies")


@app.get("/ui/logs")
def ui_logs():
    return jsonify(lines=log_buffer.get_lines(100))


@app.post("/ui/run-cleanup")
def ui_run_cleanup():
    threading.Thread(target=cleanup.run_cleanup, name="cleanup-manual", daemon=True).start()
    flash("Cleanup scan started  -  check Repair tab for results", "ok")
    return redirect(url_for("ui_dashboard") + "#repair")


@app.post("/ui/repair-all")
def ui_repair_all():
    threading.Thread(target=cleanup.run_cleanup, name="repair-all-manual", daemon=True).start()
    flash("Repair All started  -  check Repair tab for results", "ok")
    return redirect(url_for("ui_dashboard") + "#repair")


@app.post("/ui/refresh-images")
def ui_refresh_images():
    threading.Thread(target=jellyfin.refresh_missing_images, name="jf-images", daemon=True).start()
    flash("Jellyfin image refresh started  -  missing posters will be fetched", "ok")
    return redirect(url_for("ui_dashboard") + "#repair")


@app.post("/ui/merge-series")
def ui_merge_series():
    threading.Thread(target=cleanup.merge_series_duplicates, name="merge-series", daemon=True).start()
    flash("Series merge started  -  duplicate folders will be consolidated", "ok")
    return redirect(url_for("ui_dashboard") + "#repair")


@app.post("/ui/api/repair-tvshow-titles")
@_csrf.exempt
@auth.require_role("admin")
def ui_api_repair_tvshow_titles():
    """Rewrite tvshow.nfo files whose title is 'Season XX' instead of the real show name."""
    result = nfo_generator.repair_tvshow_titles()
    return jsonify(**result)


@app.post("/ui/api/fix-imdb-titles")
@_csrf.exempt
@auth.require_role("admin")
def ui_api_fix_imdb_titles():
    """Find items whose title is still a raw IMDB code, fetch real title from TMDB,
    rename folders on disk and update DB + strm paths."""
    result = strm_generator.fix_imdb_titles()
    return jsonify(**result)


@app.post("/ui/generate-nfos")
def ui_generate_nfos():
    def _run():
        nfo_generator.generate_all()
        nfo_generator.fetch_local_images()
    threading.Thread(target=_run, name="nfo-manual", daemon=True).start()
    flash("NFO + image download started  -  Jellyfin will pick up metadata on next scan", "ok")
    return redirect(url_for("ui_dashboard") + "#repair")


@app.post("/api/run-cleanup")
@_csrf.exempt
def api_run_cleanup():
    threading.Thread(target=cleanup.run_cleanup, name="cleanup-api", daemon=True).start()
    return jsonify(ok=True, started="run_cleanup")


@app.post("/api/generate-nfos")
@_csrf.exempt
def api_generate_nfos():
    def _run():
        nfo_generator.generate_all()
        nfo_generator.fetch_local_images()
    threading.Thread(target=_run, name="nfo-api", daemon=True).start()
    return jsonify(ok=True, started="generate_nfos")


@app.post("/ui/api/repair-strms")
@_csrf.exempt
@auth.require_auth
def ui_api_repair_strms():
    """Scan movie .strm files for expired direct TorBox CDN URLs and repair them.
    Files with a catbox token → left alone. Files with a direct URL:
      - if a virtual_item exists for that imdb_id → rewrite to catbox proxy URL
      - otherwise → delete the .strm and immediately requeue via processor
    """
    if not auth.is_admin():
        return jsonify(error="unauthorized"), 401
    result = strm_generator.repair_expired_strms(media_type="movie")
    return jsonify(**result)


@app.post("/ui/api/torbox/scan-library")
@_csrf.exempt
@auth.require_auth
def ui_api_torbox_scan_library():
    """Scan TorBox's own library for torrents we have no record of (e.g. after
    a DB reset) and materialize .strm files for them, resolving titles via
    TMDB where possible."""
    if not auth.is_admin():
        return jsonify(error="unauthorized"), 401
    result = strm_generator.scan_torbox_library()
    return jsonify(**result)


@app.post("/ui/api/spore/backfill")
@_csrf.exempt
@auth.require_role("admin")
def ui_api_spore_backfill():
    """Generate missing Spore stub .mkv + .minfo files for all existing virtual_items."""
    result = strm_generator.backfill_spore_stubs()
    return jsonify(**result)


@app.post("/ui/api/spore/regenerate")
@_csrf.exempt
@auth.require_role("admin")
def ui_api_spore_regenerate():
    """Force-regenerate stub MKVs with correct codec metadata.
    Pass ?token=<token> to regenerate a single item, or omit for all items."""
    token = request.args.get("token") or (request.json or {}).get("token")
    result = strm_generator.regenerate_spore_stubs(token=token)
    return jsonify(**result)


@app.post("/ui/api/migrate-canonical")
@_csrf.exempt
def ui_api_migrate_canonical():
    """Rename all movie folders to TMDB canonical names and merge duplicates."""
    result = strm_generator.migrate_to_canonical_names()
    return jsonify(**result)


@app.post("/ui/api/cleanup-duplicate-strms")
@_csrf.exempt
def ui_api_cleanup_duplicate_strms():
    """Remove extra .strm files from folders that have more than one."""
    result = strm_generator.cleanup_duplicate_strms()
    return jsonify(**result)


@app.post("/ui/api/series-backfill")
@_csrf.exempt
def ui_api_series_backfill():
    """Import all Sonarr series + run series check to create .strm files for all episodes."""
    threading.Thread(target=monitor.run_series_backfill, name="series-backfill", daemon=True).start()
    return jsonify(ok=True, started="series_backfill")


# ── New JSON APIs ─────────────────────────────────────────────────────────────

@app.get("/ui/api/health")
def ui_api_health():
    return jsonify(services=health.check_all())


@app.get("/ui/api/webhook-secret")
def ui_api_webhook_secret():
    """Return the effective webhook secret for display in the admin UI. Admin only."""
    if not auth.is_admin():
        return jsonify(error="unauthorized"), 401
    secret = _effective_webhook_secret()
    source = "env" if WEBHOOK_SECRET else "auto"
    return jsonify(secret=secret, source=source)


@app.get("/ui/api/stats")
def ui_api_stats():
    return jsonify(stats.get_overview())


@app.get("/ui/api/storage")
def ui_api_storage():
    return jsonify(folders=stats.get_storage_breakdown(30))


@app.get("/ui/api/activity")
def ui_api_activity():
    return jsonify(events=db.get_activity(50))


@app.get("/ui/api/torbox-list")
def ui_api_torbox_list():
    try:
        items = torbox.list_torrents()
        out = [{
            "id": t.get("id"),
            "name": t.get("name"),
            "hash": t.get("hash"),
            "size": t.get("size"),
            "download_state": t.get("download_state"),
            "download_finished": t.get("download_finished"),
            "progress": t.get("progress"),
            "created_at": t.get("created_at"),
            "file_count": len(t.get("files") or []),
        } for t in items]
        return jsonify(torrents=out)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.post("/ui/torbox-delete")
def ui_torbox_delete():
    torrent_id = request.form.get("torrent_id")
    if not torrent_id:
        return jsonify(error="missing torrent_id"), 400
    ok = torbox.delete_torrent(int(torrent_id))
    if ok:
        flash(f"Deleted torrent {torrent_id} from TorBox", "ok")
    else:
        flash(f"Failed to delete torrent {torrent_id}", "err")
    return redirect(url_for("ui_dashboard") + "#torbox")


@app.post("/ui/strm-rescan")
def ui_strm_rescan():
    threading.Thread(target=strm_generator.run_and_refresh, name="strm-manual", daemon=True).start()
    flash("strm rescan started", "ok")
    return redirect(url_for("ui_dashboard"))


@app.post("/ui/test-notify")
def ui_test_notify():
    results = notify.test()
    return jsonify(results)


@app.post("/ui/api/search-candidates")
def ui_api_search_candidates():
    imdb_id = (request.form.get("imdb_id") or "").strip()
    media_type = request.form.get("media_type", "movie")
    season = int(request.form.get("season", 1))
    episode = int(request.form.get("episode", 1))
    if not re.fullmatch(r"tt\d{6,10}", imdb_id):
        return jsonify(error="invalid imdb id"), 400

    if media_type == "movie":
        streams = zilean.fetch_streams(imdb_id) if _settings_mod.get("ZILEAN_ENABLED", cfg.ZILEAN_ENABLED) else []
        candidates = torrentio.rank_streams(streams)
        if not candidates:
            streams = torrentio.fetch_streams("movie", imdb_id)
            candidates = torrentio.rank_streams(streams)
    else:
        streams = zilean.fetch_streams(imdb_id, season=season, episode=episode) if _settings_mod.get("ZILEAN_ENABLED", cfg.ZILEAN_ENABLED) else []
        candidates = torrentio.rank_streams(streams)
        if not candidates:
            streams = torrentio.fetch_streams("series", imdb_id, season=season, episode=episode)
            candidates = torrentio.rank_streams(streams)

    cached_hashes = torbox.check_cached([c.info_hash for c in candidates[:30]]) if candidates else set()
    out = [{
        "name": c.name,
        "info_hash": c.info_hash,
        "magnet": c.magnet,
        "quality": c.quality,
        "size": c.size,
        "seeders": c.seeders,
        "is_season_pack": getattr(c, "is_season_pack", False),
        "cached": c.info_hash in cached_hashes,
    } for c in candidates[:30]]
    return jsonify(candidates=out)


@app.post("/ui/add-magnet")
def ui_add_magnet():
    magnet = (request.form.get("magnet") or "").strip()
    if not magnet.startswith("magnet:"):
        flash("Not a magnet link", "err")
        return redirect(url_for("ui_dashboard") + "#search")
    try:
        torbox.add_magnet(magnet, reason="manual")
        flash("Magnet added to TorBox  -  rescan will create .strm shortly", "ok")
        threading.Thread(target=strm_generator.run_and_refresh, name="strm-after-add", daemon=True).start()
    except Exception as exc:
        flash(f"Add failed: {exc}", "err")
    return redirect(url_for("ui_dashboard") + "#search")


@app.post("/ui/retry-request/<int:row_id>")
def ui_retry_request(row_id: int):
    rows = [r for r in db.get_recent(1000) if r["id"] == row_id]
    if not rows:
        flash("Request not found", "err")
        return redirect(url_for("ui_dashboard"))
    r = rows[0]
    seasons = [int(s) for s in (r.get("seasons") or "").split(",") if s.strip().isdigit()]
    media_request = MediaRequest(
        title=r["title"], media_type=r["media_type"], imdb_id=r["imdb_id"], seasons=seasons,
    )
    threading.Thread(target=processor.process, args=(media_request,),
                     name=f"retry-{r['imdb_id']}", daemon=True).start()
    flash(f"Retrying {r['title']}", "ok")
    return redirect(url_for("ui_dashboard"))


@app.get("/ui/api/poster/<imdb_id>")
def ui_api_poster(imdb_id: str):
    media_type = request.args.get("type", "movie")
    path = tmdb.get_poster_path(imdb_id, media_type)
    return jsonify(poster=f"https://image.tmdb.org/t/p/w154{path}" if path else None)


# ── Catbox lazy materialization ───────────────────────────────────────────────

@app.get("/stream/<token>")
def stream_redirect(token: str):
    """Catbox endpoint: 302 → /spore-stream/<token> (moov-first proxy).

    Redirecting to spore-stream instead of the raw CDN URL ensures all clients
    (Jellyfin, Plex direct-stream, Android TV) get moov-first MP4 so playback
    starts immediately. spore-stream handles catbox materialization itself.
    """
    ua  = request.headers.get("User-Agent", "?")[:80]
    rng = request.headers.get("Range", "-")
    log.info("stream: token=%s → /spore-stream/ ua=%r range=%s", token, ua, rng)
    return redirect(f"/spore-stream/{token}", code=302)


_spore_cold_sizes: dict = {}  # token -> file_size, avoids repeated HEAD on CDN
_spore_probing: set  = set()  # tokens currently running a background probe


@app.get("/spore-stream/<token>")
def spore_stream_proxy(token: str):
    """Plex Spore proxy: serves moov-first MP4 with Range support.

    Cold cache: pass-through Range proxy to CDN while building .fsh in background.
    Warm cache: serve virtual moov-first layout so FFmpeg never seeks 15GB.
    """
    import time as _t
    import mp4_faststart

    started = _t.monotonic()
    ua  = request.headers.get("User-Agent", "?")[:80]
    rng = request.headers.get("Range", "-")

    url = catbox.materialize(token)
    if not url:
        log.warning("spore-stream: materialize FAILED token=%s ua=%r range=%s (%.1fs)",
                    token, ua, rng, _t.monotonic() - started)
        abort(404)

    info = mp4_faststart.load(token)

    cdn_url = url

    def _build_then_probe(cdn_url_: str, tok: str) -> None:
        import json as _json, subprocess as _sp
        import strm_generator as _sg, db as _db, mp4_faststart as _fs
        try:
            ok = mp4_faststart.build_and_cache(cdn_url_, tok)
            if not ok:
                return

            # Skip if already probed and preferred_audio detection is done
            existing = _db.load_spore_tracks(tok)
            if existing and "preferred_audio_idx" in existing:
                return

            cp = _fs.extract_codec_private(tok)
            v_extra_hex = cp.hex() if cp else ""

            res = _sp.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-show_format", cdn_url_],
                capture_output=True, timeout=60,
            )
            if res.returncode != 0:
                return
            data    = _json.loads(res.stdout)
            streams = data.get("streams", [])
            audio   = [s for s in streams if s.get("codec_type") == "audio"]
            subs    = [s for s in streams if s.get("codec_type") == "subtitle"]
            dur     = float(data.get("format", {}).get("duration", 0) or 0)
            preferred_idx = _sg._preferred_audio_index(audio)
            _db.save_spore_tracks(tok, {
                "audio": audio, "subs": subs, "duration_s": dur,
                "video_extradata_hex": v_extra_hex,
                "preferred_audio_idx": preferred_idx,
            })
            if audio or subs or dur or v_extra_hex:
                _sg.update_stub_from_probe(tok, audio, subs, duration_s=dur or None)
            if preferred_idx > 0:
                _sg.update_minfo_preferred_audio(tok, preferred_idx)
                log.info("spore-stream: preferred_audio=%d for token=%s (TrueHD -> fallback)",
                         preferred_idx, tok)
        except Exception as exc:
            log.warning("spore-stream: post-build probe failed for %s: %s", tok, exc)
        finally:
            _spore_probing.discard(tok)

    if info is None:
        # Cold cache: build .fsh in background, immediately proxy Range requests
        # to CDN so FFmpeg doesn't stall. _spore_cold_sizes caches file_size so
        # repeated Range requests (FFmpeg seeks) skip the HEAD round-trip.
        if token not in _spore_cold_sizes:
            threading.Thread(
                target=_build_then_probe,
                args=(cdn_url, token),
                daemon=True,
                name=f"fsh-{token[:8]}",
            ).start()
            import requests as _req
            try:
                head = _req.head(cdn_url, timeout=10, allow_redirects=True)
                _spore_cold_sizes[token] = int(head.headers.get("Content-Length", 0))
            except Exception as exc:
                log.warning("spore-stream: HEAD failed for cold token=%s: %s", token, exc)
                abort(502)

        file_size = _spore_cold_sizes.get(token, 0)
        if not file_size:
            abort(502)

        range_hdr = request.headers.get("Range")
        if range_hdr:
            try:
                _, ranges_str = range_hdr.split("=", 1)
                r_start_s, r_end_s = ranges_str.split("-", 1)
                r_start = int(r_start_s) if r_start_s else 0
                r_end   = int(r_end_s)   if r_end_s   else file_size - 1
            except Exception:
                abort(416)
            r_end  = min(r_end, file_size - 1)
            status = 206
        else:
            r_start, r_end, status = 0, file_size - 1, 200

        length = r_end - r_start + 1

        import requests as _req

        def _gen_passthrough():
            CHUNK = 2 << 20
            pos = r_start
            while pos <= r_end:
                end = min(pos + CHUNK - 1, r_end)
                hdrs = {"Range": f"bytes={pos}-{end}"}
                try:
                    resp = _req.get(cdn_url, headers=hdrs, timeout=(10, 60), stream=True)
                    for chunk in resp.iter_content(65536):
                        yield chunk
                    pos = end + 1
                except Exception as exc:
                    log.warning("spore-stream cold proxy: error pos=%d token=%s: %s",
                                pos, token, exc)
                    break

        resp = Response(
            stream_with_context(_gen_passthrough()),
            status=status,
            mimetype="video/mp4",
            direct_passthrough=True,
        )
        resp.headers["Accept-Ranges"]  = "bytes"
        resp.headers["Content-Length"] = str(length)
        if status == 206:
            resp.headers["Content-Range"] = f"bytes {r_start}-{r_end}/{file_size}"
        log.info("spore-stream: token=%s cold-proxy bytes=%d-%d/%d ua=%r",
                 token, r_start, r_end, file_size, ua)

        # Remove cold size once .fsh is ready so next request uses warm path
        if mp4_faststart.load(token) is not None:
            _spore_cold_sizes.pop(token, None)

        return resp

    # CDN file is already moov-first (or MKV redirect sentinel).
    # MKV files (ftyp_size == 0): redirect to CDN — FFmpeg reads MKV from byte 0,
    #   no seeking needed, and CDN redirect avoids unnecessary proxy bandwidth.
    # Already fast-start MP4 (ftyp_size > 0): proxy bytes through our server so
    #   Plex Server cannot cache the raw CDN URL. Plex stores our /spore-stream/
    #   URL instead; when any client (MiTV, Shield, etc.) plays, they always hit
    #   our server which resolves a fresh CDN URL — expired URLs never reach clients.
    if info.get("already_fast"):
        existing = db.load_spore_tracks(token)
        if (not existing or "preferred_audio_idx" not in existing) and token not in _spore_probing:
            _spore_probing.add(token)
            threading.Thread(
                target=_build_then_probe,
                args=(cdn_url, token),
                daemon=True,
                name=f"probe-{token[:8]}",
            ).start()
            log.info("spore-stream: token=%s triggering background probe", token)
        if info["ftyp_size"] == 0:
            # Non-MP4 sentinel (MKV/other): 302 to CDN, no moov seeking required.
            _spore_cold_sizes.pop(token, None)
            log.info("spore-stream: token=%s non-MP4 sentinel, 302 to CDN", token)
            return redirect(cdn_url, code=302)
        # Already fast-start MP4: proxy bytes; Plex stores our URL not the CDN URL.
        log.info("spore-stream: token=%s already fast-start MP4, proxying bytes", token)
        _spore_cold_sizes[token] = info["cdn_size"]
        info = None  # fall through to cold-proxy path below

    file_size = info["cdn_size"]
    range_hdr = request.headers.get("Range")

    if range_hdr:
        try:
            _, ranges_str    = range_hdr.split("=", 1)
            r_start_s, r_end_s = ranges_str.split("-", 1)
            v_start = int(r_start_s) if r_start_s else 0
            v_end   = int(r_end_s)   if r_end_s   else file_size - 1
        except Exception:
            abort(416)
        v_end  = min(v_end, file_size - 1)
        status = 206
    else:
        v_start, v_end, status = 0, file_size - 1, 200

    length = v_end - v_start + 1

    def _generate():
        CHUNK = 2 << 20
        pos = v_start
        while pos <= v_end:
            end = min(pos + CHUNK - 1, v_end)
            try:
                data = mp4_faststart.serve_bytes(info, cdn_url, pos, end)
            except Exception as exc:
                log.warning("spore-stream proxy: error v=%d token=%s: %s", pos, token, exc)
                break
            if not data:
                break
            yield data
            pos += len(data)

    resp = Response(
        stream_with_context(_generate()),
        status=status,
        mimetype="video/mp4",
        direct_passthrough=True,
    )
    resp.headers["Accept-Ranges"]  = "bytes"
    resp.headers["Content-Length"] = str(length)
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {v_start}-{v_end}/{file_size}"
    log.info("spore-stream: token=%s bytes=%d-%d/%d (%.1fs) ua=%r",
             token, v_start, v_end, file_size, _t.monotonic() - started, ua)
    return resp


@app.get("/ui/api/virtual-items")
def ui_api_virtual_items():
    items = db.get_all_virtual_items()
    return jsonify(items=[{
        "id": i["id"], "token": i["token"], "title": i["title"], "media_type": i["media_type"],
        "torbox_id": i["torbox_id"], "in_torbox": bool(i["torbox_id"]),
        "play_count": i["play_count"], "last_played": i["last_played"],
        "created_at": i["created_at"], "info_hash": i["info_hash"],
    } for i in items])


@app.post("/ui/api/virtual-items/<token>/re-resolve")
@_csrf.exempt
def ui_api_re_resolve(token: str):
    """Clear fail state for a token and trigger a fresh materialize attempt."""
    item = db.get_virtual_item(token)
    if not item:
        return jsonify(error="unknown token"), 404
    # Clear in-memory caches
    catbox.invalidate_url_cache(token)
    with catbox._fail_cache_lock:
        catbox._fail_cache.pop(token, None)
    # Reset persistent state
    import catbox as _catbox
    ckey = _catbox._content_key(item)
    if ckey:
        db.reset_playability_state(ckey)
    # Attempt fresh materialize in background, return immediately
    result: dict = {}
    import threading as _threading
    def _try():
        url = catbox.materialize(token, allow_readd=True)
        result["url"] = url
    t = _threading.Thread(target=_try, daemon=True)
    t.start()
    t.join(timeout=50)
    if result.get("url"):
        return jsonify(ok=True, resolved=True, title=item["title"])
    return jsonify(ok=True, resolved=False, title=item["title"],
                   hint="check logs  -  re-resolve attempted but no URL returned")


@app.get("/ui/api/playability-state")
def ui_api_playability_state():
    """Return degraded items with 3+ consecutive failures."""
    items = db.get_degraded_items(min_failures=3)
    return jsonify(items=items)


@app.get("/ui/api/integrity")
def ui_api_integrity():
    """Read-only data-integrity scan: surfaces empty/malformed imdb_id,
    missing hashes, duplicate content and orphan playability rows."""
    return jsonify(db.integrity_report())


@app.post("/ui/catbox-gc")
def ui_catbox_gc():
    n = catbox.release_idle()
    flash(f"Released {n} idle torrent(s)", "ok")
    return redirect(url_for("ui_dashboard") + "#catbox")


@app.get("/ui/api/blacklist")
def ui_api_blacklist():
    return jsonify(items=db.get_all_failed_hashes())


@app.post("/ui/blacklist-clear/<info_hash>")
def ui_blacklist_clear(info_hash: str):
    db.clear_failed_hash(info_hash)
    flash(f"Cleared blacklist for {info_hash[:12]}…", "ok")
    return redirect(url_for("ui_dashboard") + "#blacklist")


@app.post("/ui/backup-now")
def ui_backup_now():
    threading.Thread(target=backup.run, name="backup-manual", daemon=True).start()
    flash("DB backup started", "ok")
    return redirect(url_for("ui_dashboard"))


@app.get("/ui/api/backups")
def ui_api_backups():
    return jsonify(backups=backup.list_backups())


@app.post("/ui/backup-restore")
def ui_backup_restore():
    name = request.form.get("name", "").strip()
    if not backup.restore(name):
        flash(f"Restore failed for {name}", "err")
        return redirect(url_for("ui_dashboard") + "#catbox")
    flash(f"Restored {name}. Restart the container to load the new DB.", "ok")
    return redirect(url_for("ui_dashboard") + "#catbox")


# ── Upgrader / consolidation / trending triggers ──────────────────────────────

@app.post("/ui/auto-upgrade")
def ui_auto_upgrade():
    threading.Thread(target=upgrader.run_auto_upgrade, name="upgrade-manual", daemon=True).start()
    flash("Auto-upgrade scan started", "ok")
    return redirect(url_for("ui_dashboard") + "#overview")


@app.post("/ui/pack-consolidate")
def ui_pack_consolidate():
    threading.Thread(target=upgrader.run_pack_consolidation, name="pack-manual", daemon=True).start()
    flash("Season-pack consolidation started", "ok")
    return redirect(url_for("ui_dashboard") + "#overview")


@app.post("/ui/trending-now")
def ui_trending_now():
    threading.Thread(target=trending.run, name="trending-manual", daemon=True).start()
    flash("Trending pre-cache started", "ok")
    return redirect(url_for("ui_dashboard") + "#overview")


@app.post("/ui/continue-watching")
def ui_continue_watching():
    threading.Thread(target=continue_watching.prioritize_next_episodes,
                     name="cw-manual", daemon=True).start()
    flash("Continue-watching scan started", "ok")
    return redirect(url_for("ui_dashboard") + "#overview")


@app.get("/ui/api/settings")
def ui_api_settings():
    import settings
    return jsonify(groups=settings.all_for_ui(), hot_reload=list(settings.HOT_RELOAD))


@app.post("/ui/settings")
def ui_save_settings():
    import settings
    saved = 0
    for raw_key, raw_value in request.form.items():
        if not raw_key.startswith("setting_"):
            continue
        key = raw_key[8:]
        # Checkbox semantics: only the box's value if checked; we emit hidden "false"
        # before each checkbox so the value always arrives. Handle multi-value here.
        values = request.form.getlist(raw_key)
        value = values[-1] if values else raw_value
        if key in settings._BOOL_KEYS:
            settings.set(key, str(value).lower() in ("1", "true", "yes", "on"))
        elif value == "":
            settings.set(key, None)
        else:
            settings.set(key, value)
        saved += 1
    flash(f"Saved {saved} setting(s). Hot-reload settings apply immediately; others need a restart.", "ok")
    return redirect(url_for("ui_dashboard") + "#settings")


@app.post("/ui/settings-reset/<key>")
def ui_settings_reset(key: str):
    import settings
    settings.set(key, None)
    flash(f"Reset {key} to .env default", "ok")
    return redirect(url_for("ui_dashboard") + "#settings")


# ── Robustness endpoints ──────────────────────────────────────────────────────

@app.get("/ui/api/orphans")
def ui_api_orphans():
    return jsonify(library_sync.orphans())


def _library_import_and_resolve():
    library_sync.import_existing()
    library_sync.resolve_unknowns()
    library_sync.import_series_to_monitored()
    nfo_generator.generate_all()
    nfo_generator.fetch_local_images()


@app.post("/ui/library-import")
def ui_library_import():
    threading.Thread(target=_library_import_and_resolve,
                     name="lib-import", daemon=True).start()
    flash("Library import started  -  check Logs for progress", "ok")
    return redirect(url_for("ui_dashboard") + "#overview")


@app.post("/ui/recovery")
def ui_recovery():
    threading.Thread(target=recovery.run, name="recovery-wizard", daemon=True).start()
    flash("Recovery wizard started  -  runs integrity check + cleanup + import + strm scan", "ok")
    return redirect(url_for("ui_dashboard") + "#overview")


@app.post("/ui/db-vacuum")
def ui_db_vacuum():
    threading.Thread(target=db.vacuum, name="db-vacuum", daemon=True).start()
    flash("DB vacuum started", "ok")
    return redirect(url_for("ui_dashboard") + "#overview")


@app.post("/ui/db-prune")
def ui_db_prune():
    threading.Thread(target=lambda: db.prune_old(90), name="db-prune", daemon=True).start()
    flash("Pruning rows older than 90 days", "ok")
    return redirect(url_for("ui_dashboard") + "#overview")


@app.post("/ui/quota-check")
def ui_quota_check():
    threading.Thread(
        target=lambda: torbox.check_quota_and_warn(QUOTA_WARN_TORRENT_COUNT, QUOTA_WARN_SIZE_GB),
        name="quota-manual", daemon=True,
    ).start()
    flash("Quota check started", "ok")
    return redirect(url_for("ui_dashboard") + "#overview")


# ── Retry queue + show overrides + metrics + TorBox usage ─────────────────────

@app.get("/ui/api/retry-queue")
def ui_api_retry_queue():
    return jsonify(items=db.get_pending_retries())


@app.get("/ui/api/requests/all")
def ui_api_all_requests():
    rows = db.get_recent(5000)
    return jsonify(items=rows)


@app.get("/ui/api/requests/status")
def ui_api_request_status():
    imdb_id = request.args.get("imdb_id")
    if not imdb_id:
        return jsonify(error="imdb_id required"), 400
    row = db.get_request_by_imdb(imdb_id)
    if not row:
        return jsonify(status="not_found")
    return jsonify(status=row.get("status"), imdb_id=imdb_id)


@app.get("/ui/api/requests/failed")
def ui_api_failed_requests():
    rows = [r for r in db.get_recent(500) if r.get("status") == "failed"]
    return jsonify(items=rows)


@app.post("/ui/api/requests/<int:row_id>/retry")
@_csrf.exempt
def ui_api_retry_request(row_id: int):
    rows = [r for r in db.get_recent(1000) if r["id"] == row_id]
    if not rows:
        return jsonify(error="not found"), 404
    r = rows[0]
    seasons = [int(s) for s in (r.get("seasons") or "").split(",") if s.strip().isdigit()]
    media_request = MediaRequest(
        title=r["title"], media_type=r["media_type"], imdb_id=r["imdb_id"], seasons=seasons,
    )
    db.update_request(row_id, "pending")
    threading.Thread(target=processor.process, args=(media_request,),
                     name=f"retry-{r['imdb_id']}", daemon=True).start()
    return jsonify(ok=True, title=r["title"])


@app.post("/ui/api/requests/<int:row_id>/delete")
@_csrf.exempt
def ui_api_delete_request(row_id: int):
    with db._connect() as conn:
        cur = conn.execute("DELETE FROM requests WHERE id=?", (row_id,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify(error="not found"), 404
    return jsonify(ok=True)


@app.get("/ui/api/torbox-usage")
def ui_api_torbox_usage():
    summary = torbox.get_usage_summary()
    user = torbox.get_user_info() or {}
    return jsonify(usage=summary, plan=user.get("plan") if isinstance(user, dict) else None)


@app.get("/ui/api/metrics-summary")
def ui_api_metrics_summary():
    return jsonify(
        quality=db.get_metric_summary("quality_added", days=30),
        sources=db.get_metric_summary("source_win", days=30),
        latency=db.get_metric_summary("latency_seconds", days=30),
        failures=db.get_metric_summary("request_failed", days=30),
    )


@app.get("/ui/api/show-overrides")
def ui_api_show_overrides():
    return jsonify(items=db.get_all_show_overrides())


@app.post("/ui/show-override")
def ui_show_override():
    imdb_id = (request.form.get("imdb_id") or "").strip()
    if not re.fullmatch(r"tt\d{6,10}", imdb_id):
        flash("Invalid IMDB ID", "err")
        return redirect(url_for("ui_dashboard") + "#overrides")
    quality = request.form.get("quality_preference") or None
    allow_4k_raw = request.form.get("allow_4k")
    prefer_hevc_raw = request.form.get("prefer_hevc")
    allow_4k = None if not allow_4k_raw else allow_4k_raw == "true"
    prefer_hevc = None if not prefer_hevc_raw else prefer_hevc_raw == "true"
    notes = request.form.get("notes") or None
    db.upsert_show_override(imdb_id, quality, allow_4k, prefer_hevc, notes)
    flash(f"Saved override for {imdb_id}", "ok")
    return redirect(url_for("ui_dashboard") + "#overrides")


@app.post("/ui/show-override-delete/<imdb_id>")
def ui_show_override_delete(imdb_id: str):
    db.delete_show_override(imdb_id)
    flash(f"Cleared override for {imdb_id}", "ok")
    return redirect(url_for("ui_dashboard") + "#overrides")


# ── WebDAV (opt-in, mount via davfs2 from DSM host) ───────────────────────────

import webdav

_WEBDAV_METHODS = ["OPTIONS", "GET", "HEAD", "PROPFIND"]


# ── Discover (TMDB) ──────────────────────────────────────────────────────────

@app.get("/ui/api/discover/search")
def ui_api_discover_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(results=[])
    page = int(request.args.get("page") or "1")
    results = tmdb.multi_search(q, page=page)
    _enrich_library_status(results)
    return jsonify(results=results)


_STATUS_PRIORITY = {"success": 0, "available": 0, "wanted": 1, "pending": 2, "upcoming": 3, "failed": 4}


def _enrich_library_status(items: list[dict]) -> None:
    """Add library_status and imdb_id to TMDB result dicts by checking all tables with tmdb_id."""
    tmdb_ids = [it["tmdb_id"] for it in items if it.get("tmdb_id")]
    if not tmdb_ids:
        return
    ph = ",".join("?" * len(tmdb_ids))
    with db._connect() as conn:
        rows = conn.execute(f"""
            SELECT tmdb_id, status FROM requests WHERE tmdb_id IN ({ph})
            UNION ALL
            SELECT w.tmdb_id, r.status
            FROM watchlist w JOIN requests r ON r.imdb_id = w.imdb_id
            WHERE w.tmdb_id IN ({ph})
            UNION ALL
            SELECT ms.tmdb_id, r.status
            FROM monitored_series ms JOIN requests r ON r.imdb_id = ms.imdb_id
            WHERE ms.tmdb_id IN ({ph})
            UNION ALL
            SELECT ur.tmdb_id, COALESCE(r.status, ur.status)
            FROM user_requests ur LEFT JOIN requests r ON r.imdb_id = ur.imdb_id
            WHERE ur.tmdb_id IN ({ph})
        """, tmdb_ids * 4).fetchall()
        # Fetch imdb_ids from library so PosterCard can show watched badges
        imdb_rows = conn.execute(
            f"SELECT tmdb_id, imdb_id FROM requests WHERE tmdb_id IN ({ph}) AND imdb_id IS NOT NULL",
            tmdb_ids,
        ).fetchall()
        # Also from trakt_watched for items watched but not in library
        rec = auth.current_user_record()
        trakt_imdb_rows = []
        if rec and rec.get("id"):
            try:
                trakt_imdb_rows = conn.execute(
                    f"SELECT tmdb_id, imdb_id FROM trakt_watched WHERE user_id=? AND tmdb_id IN ({ph}) AND imdb_id IS NOT NULL",
                    [rec["id"]] + tmdb_ids,
                ).fetchall()
            except Exception:
                pass
    status_map: dict[int, str] = {}
    for r in rows:
        tid, st = r["tmdb_id"], r["status"]
        prev = status_map.get(tid)
        if prev is None or _STATUS_PRIORITY.get(st, 9) < _STATUS_PRIORITY.get(prev, 9):
            status_map[tid] = st
    imdb_map = {r["tmdb_id"]: r["imdb_id"] for r in imdb_rows}
    for r in trakt_imdb_rows:
        imdb_map.setdefault(r["tmdb_id"], r["imdb_id"])
    for it in items:
        it["library_status"] = status_map.get(it.get("tmdb_id"))
        if not it.get("imdb_id"):
            it["imdb_id"] = imdb_map.get(it.get("tmdb_id"))


def _user_region() -> str:
    """Region from ?region= param, or from the logged-in user's profile, or system default."""
    r = request.args.get("region")
    if r:
        return r
    rec = auth.current_user_record()
    if rec and rec.get("region"):
        return rec["region"]
    return cfg.AUTO_ADD_REGION


@app.get("/ui/api/discover/trending")
def ui_api_discover_trending():
    media = request.args.get("type", "all")  # all | movie | tv
    window = request.args.get("window", "week")  # day | week
    results = tmdb.trending(media, window)
    _enrich_library_status(results)
    return jsonify(results=results)


@app.get("/ui/api/discover/popular")
def ui_api_discover_popular():
    media = request.args.get("type", "movie")
    region = _user_region()
    results = tmdb.popular(media, region=region)
    _enrich_library_status(results)
    return jsonify(results=results)


@app.get("/ui/api/discover/top-rated")
def ui_api_discover_top_rated():
    media = request.args.get("type", "movie")
    results = tmdb.top_rated(media)
    _enrich_library_status(results)
    return jsonify(results=results)


@app.get("/ui/api/discover/now-playing")
def ui_api_discover_now_playing():
    region = _user_region()
    results = tmdb.now_playing(region=region)
    _enrich_library_status(results)
    return jsonify(results=results)


@app.get("/ui/api/discover/upcoming")
def ui_api_discover_upcoming():
    region = _user_region()
    results = tmdb.upcoming(region=region)
    _enrich_library_status(results)
    return jsonify(results=results)


@app.get("/ui/api/discover/on-the-air")
def ui_api_discover_on_the_air():
    results = tmdb.on_the_air()
    _enrich_library_status(results)
    return jsonify(results=results)


@app.get("/ui/api/discover/providers")
def ui_api_discover_providers():
    media = request.args.get("type", "movie")
    region = _user_region()
    return jsonify(providers=tmdb.list_providers(media, region=region))


@app.get("/ui/api/discover/by-provider")
def ui_api_discover_by_provider():
    media = request.args.get("type", "movie")
    pid = int(request.args.get("provider_id") or "0")
    region = _user_region()
    sort = request.args.get("sort_by", "popularity.desc")
    if not pid:
        return jsonify(error="provider_id required"), 400
    results = tmdb.discover_by_provider(media, pid, region=region, sort_by=sort)
    _enrich_library_status(results)
    return jsonify(results=results)


@app.get("/ui/api/discover/details")
def ui_api_discover_details():
    media = request.args.get("type", "movie")
    tmdb_id = int(request.args.get("id") or "0")
    region = _user_region()
    if not tmdb_id:
        return jsonify(error="id required"), 400
    detail = tmdb.details(media, tmdb_id, region=region)
    if not detail:
        return jsonify(error="not found"), 404
    imdb_id = detail.get("imdb_id")
    if imdb_id:
        vi = db.get_virtual_items_by_imdb(imdb_id)
        if vi:
            detail["library_status"] = "available"
        else:
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT status FROM requests WHERE imdb_id=?", (imdb_id,)
                ).fetchone()
            if row:
                detail["library_status"] = row["status"]
    return jsonify(detail)


@app.post("/ui/api/discover/add")
def ui_api_discover_add():
    """One-click add: resolve TMDB→IMDB, queue for processing.
    For TV the payload may include monitor_mode ('all'|'future'|'selected')
    and seasons (list of season numbers, used when mode is 'selected')."""
    payload = request.get_json(silent=True) or {}
    tmdb_id = payload.get("tmdb_id")
    media_type = payload.get("media_type") or "movie"
    title = payload.get("title") or ""
    monitor_mode = payload.get("monitor_mode") or "all"
    seasons = payload.get("seasons") or None
    if not tmdb_id or not title:
        return jsonify(error="tmdb_id and title required"), 400

    imdb_id = tmdb.tmdb_to_imdb(tmdb_id, media_type=media_type)
    if not imdb_id:
        return jsonify(error="could not resolve imdb_id"), 400

    user_rec = auth.current_user_record()
    if user_rec and user_rec.get("id"):
        # Multi-user mode: go through approval flow
        auto = bool(user_rec.get("auto_approve")) or user_rec.get("role") == "admin"
        status = "approved" if auto else "pending"
        rid = db.create_user_request(user_rec["id"], imdb_id, tmdb_id, media_type,
                                       title, status=status)
        if status == "approved":
            _kick_off_processing(title, imdb_id, media_type, tmdb_id, monitor_mode, seasons)
        return jsonify(status=status, request_id=rid, imdb_id=imdb_id)

    # Single-user / legacy mode: process immediately
    _kick_off_processing(title, imdb_id, media_type, tmdb_id, monitor_mode, seasons)
    return jsonify(status="queued", imdb_id=imdb_id)


def _kick_off_processing(title: str, imdb_id: str, media_type: str,
                          tmdb_id: int | None = None,
                          monitor_mode: str = "all",
                          seasons: list[int] | None = None) -> None:
    from webhook_parser import MediaRequest
    if media_type == "tv":
        try:
            show = tmdb.get_show_info(tmdb_id) if tmdb_id else None
            n_seasons = (show or {}).get("number_of_seasons") or 1
            all_seasons = list(range(1, n_seasons + 1))
            if monitor_mode == "selected" and seasons:
                monitored = [int(s) for s in seasons if int(s) in all_seasons]
            else:
                monitored = all_seasons
            db.upsert_monitored_series(imdb_id, tmdb_id, title, monitored, monitor_mode=monitor_mode)
        except Exception as exc:
            log.warning("upsert_monitored_series failed: %s", exc)
            monitored = seasons or [1]
        # 'future' mode: don't eagerly fetch the back-catalog  -  let the monitor
        # pick up episodes as they air. Eagerly process only for all/selected.
        process_seasons = [] if monitor_mode == "future" else monitored
        req = MediaRequest(title=title, media_type="series",
                            imdb_id=imdb_id, seasons=process_seasons, tmdb_id=tmdb_id)
        if not process_seasons:
            # Nothing to fetch now; the series is monitored and the periodic
            # check will grab future episodes.
            return
    else:
        req = MediaRequest(title=title, media_type="movie", imdb_id=imdb_id, seasons=[], tmdb_id=tmdb_id)
    threading.Thread(
        target=processor.process, args=(req,),
        name=f"discover-{imdb_id}", daemon=True,
    ).start()


# ── Watchlist (per user) ─────────────────────────────────────────────────────

@app.get("/ui/api/watchlist")
def ui_api_watchlist_get():
    rec = auth.current_user_record()
    if not rec or not rec.get("id"):
        return jsonify(items=[])
    items = db.get_watchlist(rec["id"])
    imdb_ids = [w["imdb_id"] for w in items if w.get("imdb_id")]
    if imdb_ids:
        with db._connect() as conn:
            ph = ",".join("?" * len(imdb_ids))
            rows = conn.execute(
                f"SELECT imdb_id, status FROM requests WHERE imdb_id IN ({ph})",
                imdb_ids,
            ).fetchall()
            lib_map = {r["imdb_id"]: r["status"] for r in rows}
            # Enrich poster_path from poster_cache for items missing it
            poster_rows = conn.execute(
                f"SELECT imdb_id, poster_path FROM poster_cache WHERE imdb_id IN ({ph})",
                imdb_ids,
            ).fetchall()
            poster_map = {r["imdb_id"]: r["poster_path"] for r in poster_rows}
        for w in items:
            w["library_status"] = lib_map.get(w.get("imdb_id"))
            if not w.get("poster_path") and w.get("imdb_id"):
                w["poster_path"] = poster_map.get(w["imdb_id"])
    else:
        for w in items:
            w["library_status"] = None
    return jsonify(items=items)


@app.post("/ui/api/watchlist/add")
def ui_api_watchlist_add():
    rec = auth.current_user_record()
    if not rec or not rec.get("id"):
        return jsonify(error="login required"), 401
    p = request.get_json(silent=True) or {}
    imdb = (p.get("imdb_id") or "").strip()
    if not imdb:
        return jsonify(error="imdb_id required"), 400
    db.add_to_watchlist(rec["id"], imdb, p.get("tmdb_id"),
                         p.get("media_type") or "movie",
                         p.get("title") or "", p.get("poster_path"))
    return jsonify(ok=True)


@app.post("/ui/api/watchlist/remove")
def ui_api_watchlist_remove():
    rec = auth.current_user_record()
    if not rec or not rec.get("id"):
        return jsonify(error="login required"), 401
    p = request.get_json(silent=True) or {}
    db.remove_from_watchlist(rec["id"], (p.get("imdb_id") or "").strip(),
                              p.get("media_type") or "movie")
    return jsonify(ok=True)


# ── User requests (approval flow) ────────────────────────────────────────────

@app.get("/ui/api/user-requests")
def ui_api_user_requests():
    rec = auth.current_user_record()
    if not rec:
        return jsonify(items=[])
    status = request.args.get("status") or None
    mine_only = request.args.get("mine") == "1"
    if mine_only or rec.get("role") != "admin":
        items = db.get_user_requests(user_id=rec["id"], status=status)
    else:
        items = db.get_user_requests(status=status)
    imdb_ids = {r["imdb_id"] for r in items}
    if imdb_ids:
        with db._connect() as conn:
            ph = ",".join("?" * len(imdb_ids))
            rows = conn.execute(
                f"SELECT imdb_id, status FROM requests WHERE imdb_id IN ({ph})",
                list(imdb_ids),
            ).fetchall()
            lib_map = {r["imdb_id"]: r["status"] for r in rows}
        for r in items:
            r["library_status"] = lib_map.get(r["imdb_id"])
    else:
        for r in items:
            r["library_status"] = None
    return jsonify(items=items)


@app.post("/ui/api/user-requests/<int:req_id>/approve")
def ui_api_user_request_approve(req_id: int):
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    rec = auth.current_user_record()
    r = db.get_user_request(req_id)
    if not r:
        return jsonify(error="not found"), 404
    db.update_user_request_status(req_id, "approved",
                                   reviewed_by=(rec or {}).get("id"))
    _kick_off_processing(r["title"], r["imdb_id"], r["media_type"], r.get("tmdb_id"))
    return jsonify(ok=True)


@app.post("/ui/api/user-requests/<int:req_id>/deny")
def ui_api_user_request_deny(req_id: int):
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    rec = auth.current_user_record()
    p = request.get_json(silent=True) or {}
    db.update_user_request_status(req_id, "denied",
                                   reviewed_by=(rec or {}).get("id"),
                                   note=p.get("note"))
    return jsonify(ok=True)


# ── User management (admin) ──────────────────────────────────────────────────

@app.get("/ui/api/wanted-movies")
def ui_api_wanted_movies():
    return jsonify(items=db.get_wanted_movies())


@app.post("/ui/api/wanted-recheck")
@_csrf.exempt
def ui_api_wanted_recheck():
    def _run():
        try:
            upgrader.recheck_wanted()
            monitor.run_series_check()
        except Exception as exc:
            logging.getLogger(__name__).error("wanted-recheck-manual failed: %s", exc)
    threading.Thread(target=_run, name="wanted-recheck-manual", daemon=True).start()
    return jsonify(ok=True, message="wanted recheck started")


@app.post("/ui/search-all-wanted")
def ui_search_all_wanted():
    def _run():
        upgrader.recheck_wanted()
        monitor.run_series_check()
    threading.Thread(target=_run, name="wanted-search-all", daemon=True).start()
    flash("Search all wanted started  -  this may take a while.", "info")
    return redirect(url_for("ui_dashboard") + "#wanted")


@app.get("/ui/api/wanted-episodes")
def ui_api_wanted_episodes():
    db.reconcile_wanted_episodes()
    return jsonify(items=db.get_all_wanted_episodes())


@app.get("/ui/api/library/status-map")
def ui_api_library_status_map():
    """Map of tmdb_id -> library status for badge display on poster cards."""
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT tmdb_id, status FROM requests WHERE tmdb_id IS NOT NULL
            UNION
            SELECT w.tmdb_id, r.status
            FROM watchlist w
            JOIN requests r ON r.imdb_id = w.imdb_id
            WHERE w.tmdb_id IS NOT NULL
            UNION
            SELECT ms.tmdb_id, r.status
            FROM monitored_series ms
            JOIN requests r ON r.imdb_id = ms.imdb_id
            WHERE ms.tmdb_id IS NOT NULL
            UNION
            SELECT ur.tmdb_id,
                   COALESCE(r.status, ur.status) AS status
            FROM user_requests ur
            LEFT JOIN requests r ON r.imdb_id = ur.imdb_id
            WHERE ur.tmdb_id IS NOT NULL
        """).fetchall()
    return jsonify({str(r["tmdb_id"]): r["status"] for r in rows})


@app.get("/ui/api/library/movies")
def ui_api_library_movies():
    """Return all movie requests with status info, reconciling stale wanted status."""
    db.reconcile_wanted_movies()
    rows = db.get_recent(10000)
    seen = set()
    items = []
    for r in rows:
        if r.get("media_type") != "movie":
            continue
        imdb = r.get("imdb_id", "")
        if imdb in seen:
            continue
        seen.add(imdb)
        items.append({
            "title": r.get("title") or "Unknown",
            "imdb_id": imdb,
            "tmdb_id": r.get("tmdb_id"),
            "quality": r.get("quality"),
            "status": r.get("status"),
            "source": r.get("source"),
            "created_at": r.get("created_at"),
            "year": r.get("year"),
        })
    # Enrich with cached poster paths (single batch query)
    imdb_ids = [it["imdb_id"] for it in items if it.get("imdb_id")]
    poster_map = db.get_posters_batch(imdb_ids)
    for it in items:
        it["poster_path"] = poster_map.get(it["imdb_id"])
    return jsonify(items=items)


@app.get("/ui/api/library/series-episodes")
def ui_api_library_series_episodes():
    """Return available episodes per series with wanted info."""
    db.reconcile_wanted_episodes()
    import re as _re
    from pathlib import Path as _Path
    _EP_RE = _re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})')
    series_dir = _Path(cfg.MEDIA_PATH) / "series"

    folder_imdb = db.get_series_folder_imdb_map()

    all_monitored = db.get_all_monitored_series()
    mon_by_title: dict[str, dict] = {}
    for s in all_monitored:
        mon_by_title[s["title"].lower()] = s

    wanted_eps = db.get_all_wanted_episodes()
    wanted_by_imdb: dict[str, list[dict]] = {}
    for ep in wanted_eps:
        if ep.get("status") != "wanted":
            continue
        wanted_by_imdb.setdefault(ep["imdb_id"], []).append(ep)

    all_eps_by_imdb: dict[str, list[dict]] = {}
    for ep in wanted_eps:
        all_eps_by_imdb.setdefault(ep["imdb_id"], []).append(ep)

    out = []
    if not series_dir.is_dir():
        return jsonify(series=[])
    for show in sorted(series_dir.iterdir()):
        if not show.is_dir():
            continue
        seasons_map: dict[int, list[int]] = {}
        for season_dir in sorted(show.iterdir()):
            if not season_dir.is_dir():
                continue
            try:
                s_num = int("".join(c for c in season_dir.name if c.isdigit()))
            except ValueError:
                continue
            episodes = []
            for strm in sorted(season_dir.glob("*.strm")):
                m = _EP_RE.search(strm.stem)
                if m:
                    episodes.append(int(m.group(2)))
            if episodes:
                seasons_map[s_num] = sorted(set(episodes))

        folder_lower = show.name.lower()
        clean = _re.sub(r'\s*\(\d{4}\)\s*$', '', folder_lower)

        imdb_id = folder_imdb.get(folder_lower) or folder_imdb.get(clean)
        if not imdb_id:
            mon = mon_by_title.get(folder_lower) or mon_by_title.get(clean)
            if not mon:
                for title, s in mon_by_title.items():
                    if title.startswith(clean) or clean.startswith(title):
                        mon = s
                        break
            imdb_id = mon["imdb_id"] if mon else None

        missing_eps = wanted_by_imdb.get(imdb_id, []) if imdb_id else []
        missing = [{"season": ep["season"], "episode": ep["episode"]} for ep in missing_eps]

        for m in missing:
            if m["season"] not in seasons_map:
                seasons_map[m["season"]] = []

        season_years: dict[int, str] = {}
        if imdb_id and imdb_id in all_eps_by_imdb:
            for ep in all_eps_by_imdb[imdb_id]:
                s = ep["season"]
                ad = ep.get("air_date") or ""
                if ad and s not in season_years:
                    season_years[s] = ad[:4]

        seasons = [
            {"season": s, "episodes": eps, "year": season_years.get(s, "")}
            for s, eps in sorted(seasons_map.items())
        ]

        if seasons or missing:
            out.append({
                "title": show.name,
                "imdb_id": imdb_id,
                "seasons": seasons,
                "missing": missing,
            })
    return jsonify(series=out)


@app.get("/ui/api/torbox-quota")
def ui_api_torbox_quota():
    """createtorrent usage in the last hour, broken down by reason  -  explains
    why TorBox 429 rate limits are being hit."""
    return jsonify(torbox.createtorrent_usage())


@app.get("/ui/api/session")
def ui_api_session():
    """Current session info: who am I, what role, do I have auto_approve."""
    rec = auth.current_user_record()
    if not rec:
        return jsonify(authenticated=False, user=None)
    import settings as _settings
    user: dict = {
        "id": rec.get("id"),
        "username": rec.get("username"),
        "role": rec.get("role"),
        "auto_approve": bool(rec.get("auto_approve")),
        "region": rec.get("region", "NL"),
        "library_click_jellyfin": bool(rec.get("library_click_jellyfin")),
    }
    user.update(plugin_loader.session_fields(rec))
    jellyfin_url = (_settings.get("JELLYFIN_URL") or cfg.JELLYFIN_URL or "").rstrip("/")
    return jsonify(authenticated=True, user=user, jellyfin_url=jellyfin_url or None)


@app.get("/ui/api/plugins")
def ui_api_plugins():
    return jsonify(plugins=plugin_loader.loaded_plugins())


@app.get("/ui/api/users")
def ui_api_users():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    return jsonify(users=db.list_users())


@app.post("/ui/api/users/create")
def ui_api_users_create():
    import settings as _settings
    # Bootstrap: only allow unauthenticated first-admin creation when no users exist AND
    # SETUP_COMPLETE has never been set. This prevents re-opening the window if all
    # users are somehow deleted after initial setup.
    setup_done = bool(_settings.get("SETUP_COMPLETE", False))
    if db.user_count() == 0 and not setup_done:
        p = request.get_json(silent=True) or {}
        username = (p.get("username") or "").strip()
        password = p.get("password") or ""
        if not username or len(password) < 4:
            return jsonify(error="username + password (≥4 chars) required"), 400
        try:
            uid = auth.create_user_account(username, password, role="admin",
                                            auto_approve=True)
            _settings.set("SETUP_COMPLETE", True)
            log.info("Bootstrap: first admin '%s' created, setup_complete=true", username)
            return jsonify(ok=True, user_id=uid, message="first admin created")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    p = request.get_json(silent=True) or {}
    username = (p.get("username") or "").strip()
    password = p.get("password") or ""
    role = p.get("role") or "user"
    auto = bool(p.get("auto_approve"))
    if not username or len(password) < 4:
        return jsonify(error="username + password (≥4 chars) required"), 400
    try:
        uid = auth.create_user_account(username, password, role=role, auto_approve=auto)
        return jsonify(ok=True, user_id=uid)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.post("/ui/api/users/<int:user_id>/update")
def ui_api_users_update(user_id: int):
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    p = request.get_json(silent=True) or {}
    fields: dict = {}
    if "role" in p: fields["role"] = p["role"]
    if "quota_monthly" in p: fields["quota_monthly"] = int(p["quota_monthly"])
    if "auto_approve" in p: fields["auto_approve"] = 1 if p["auto_approve"] else 0
    if "enabled" in p: fields["enabled"] = 1 if p["enabled"] else 0
    if "region" in p: fields["region"] = str(p["region"]).upper()[:5]
    for field in plugin_loader.user_fields():
        if field in p: fields[field] = 1 if p[field] else 0
    if p.get("password"):
        if len(p["password"]) < 4:
            return jsonify(error="password too short"), 400
        fields["password_hash"] = auth.hash_password(p["password"])
    db.update_user(user_id, **fields)
    return jsonify(ok=True)


@app.post("/ui/api/me/plugin-fields")
@auth.require_auth
def ui_api_me_plugin_fields():
    """Let users toggle their own plugin user_fields (e.g. webplayer_enabled)."""
    rec = auth.current_user_record()
    if not rec:
        return jsonify(error="not authenticated"), 401
    p = request.get_json(silent=True) or {}
    allowed = set(plugin_loader.user_fields())
    fields = {k: (1 if v else 0) for k, v in p.items() if k in allowed}
    if not fields:
        return jsonify(error="no valid fields"), 400
    db.update_user(rec["id"], **fields)
    return jsonify(ok=True)


@app.post("/ui/api/me/region")
@auth.require_auth
def ui_api_me_region():
    """Let users change their own region."""
    rec = auth.current_user_record()
    if not rec:
        return jsonify(error="not authenticated"), 401
    p = request.get_json(silent=True) or {}
    region = str(p.get("region", "")).upper().strip()[:5]
    if not region:
        return jsonify(error="region required"), 400
    db.update_user(rec["id"], region=region)
    return jsonify(ok=True, region=region)


@app.post("/ui/api/me/preferences")
@auth.require_auth
def ui_api_me_preferences():
    """Let users update their own UI preferences."""
    rec = auth.current_user_record()
    if not rec:
        return jsonify(error="not authenticated"), 401
    p = request.get_json(silent=True) or {}
    _ALLOWED = {"library_click_jellyfin"}
    fields = {k: (1 if v else 0) for k, v in p.items() if k in _ALLOWED}
    if not fields:
        return jsonify(error="no valid fields"), 400
    db.update_user(rec["id"], **fields)
    return jsonify(ok=True)


# Cache Jellyfin item IDs: imdb_id -> jellyfin_item_id (or None if not found)
_jellyfin_item_cache: dict[str, str | None] = {}


@app.get("/ui/api/jellyfin/item")
@auth.require_auth
def ui_api_jellyfin_item():
    """Look up Jellyfin item ID for a given IMDB id. Result is cached in memory."""
    import settings as _settings
    imdb_id = request.args.get("imdb_id", "").strip()
    if not imdb_id:
        return jsonify(error="imdb_id required"), 400
    if imdb_id in _jellyfin_item_cache:
        jid = _jellyfin_item_cache[imdb_id]
        jurl = (_settings.get("JELLYFIN_URL") or cfg.JELLYFIN_URL or "").rstrip("/")
        return jsonify(jellyfin_id=jid, jellyfin_url=jurl or None)
    jurl = (_settings.get("JELLYFIN_URL") or cfg.JELLYFIN_URL or "").rstrip("/")
    jkey = _settings.get("JELLYFIN_API_KEY") or cfg.JELLYFIN_API_KEY or ""
    if not jurl or not jkey:
        return jsonify(jellyfin_id=None, jellyfin_url=None)
    try:
        import requests as _req
        resp = _req.get(
            f"{jurl}/Items",
            params={"AnyProviderIdEquals": f"imdb.{imdb_id}", "includeItemTypes": "Movie,Series"},
            headers={"X-Emby-Token": jkey},
            timeout=5,
        )
        resp.raise_for_status()
        items = (resp.json() or {}).get("Items") or []
        jid = items[0]["Id"] if items else None
    except Exception as exc:
        log.debug("Jellyfin item lookup %s failed: %s", imdb_id, exc)
        return jsonify(jellyfin_id=None, jellyfin_url=jurl or None)
    _jellyfin_item_cache[imdb_id] = jid
    return jsonify(jellyfin_id=jid, jellyfin_url=jurl or None)


@app.get("/ui/api/jellyfin/items")
@auth.require_auth
def ui_api_jellyfin_items():
    """Build imdb_id -> jellyfin_id map by fetching the full Jellyfin library once.
    Cached in memory; returns {jellyfin_url, items: {imdb_id: jellyfin_id_or_null}}."""
    import settings as _settings
    raw = request.args.get("imdb_ids", "").strip()
    if not raw:
        return jsonify(jellyfin_url=None, items={})
    want = {x.strip() for x in raw.split(",") if x.strip()}
    jurl = (_settings.get("JELLYFIN_URL") or cfg.JELLYFIN_URL or "").rstrip("/")
    jkey = _settings.get("JELLYFIN_API_KEY") or cfg.JELLYFIN_API_KEY or ""
    # Serve fully from cache when all requested IDs are already known
    if want.issubset(_jellyfin_item_cache):
        return jsonify(jellyfin_url=jurl or None,
                       items={iid: _jellyfin_item_cache[iid] for iid in want})
    if jurl and jkey:
        try:
            import requests as _req
            # Fetch ALL movies+series from Jellyfin with their provider IDs in one call.
            # Jellyfin does not support filtering by multiple IMDb IDs simultaneously,
            # so we pull the whole library and match locally.
            resp = _req.get(
                f"{jurl}/Items",
                params={
                    "includeItemTypes": "Movie,Series",
                    "Fields": "ProviderIds",
                    "Recursive": "true",
                    "Limit": 10000,
                },
                headers={"X-Emby-Token": jkey},
                timeout=15,
            )
            resp.raise_for_status()
            for it in (resp.json() or {}).get("Items") or []:
                iid = (it.get("ProviderIds") or {}).get("Imdb") or ""
                if iid:
                    _jellyfin_item_cache[iid] = it["Id"]
        except Exception as exc:
            log.debug("Jellyfin batch lookup failed: %s", exc)
    return jsonify(jellyfin_url=jurl or None,
                   items={iid: _jellyfin_item_cache.get(iid) for iid in want})


@app.get("/ui/api/spore-minfo/<token>")
def spore_minfo_api(token: str):
    """Return .minfo sidecar data for a token as plain text key=value pairs.

    Used by the Plex transcoder wrapper when playing .strm files (no .minfo
    file path is known from the URL alone, so the wrapper fetches it here).
    No auth required: only token=hex is returned, no secrets.
    """
    item = db.get_virtual_item(token)
    if not item or not item.get("strm_path"):
        return f"token={token}\n", 404, {"Content-Type": "text/plain"}
    from pathlib import Path as _Path
    strm_path = _Path(item["strm_path"])
    minfo_path = strm_generator._spore_stub_dir(strm_path) / (strm_path.stem + ".minfo")
    if minfo_path.exists():
        try:
            return minfo_path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/plain"}
        except Exception:
            pass
    return f"token={token}\n", 200, {"Content-Type": "text/plain"}


@app.post("/ui/api/backfill-nfo")
@auth.require_auth
def ui_api_backfill_nfo():
    """Add/update <fileinfo><streamdetails> in all existing NFO files.

    For items with probed audio tracks: uses real codec/language data.
    For unprobed items: uses quality-based defaults (hevc/h264, EAC3 6ch).
    Safe to run multiple times. Triggers a Plex library refresh afterward if
    PLEX_URL and PLEX_TOKEN are configured.
    """
    result = strm_generator.backfill_nfo_streamdetails()
    return jsonify(result)


@app.get("/ui/api/tmdb/find")
@auth.require_auth
def ui_api_tmdb_find():
    """Resolve imdb_id to tmdb_id + media_type via TMDB /find endpoint."""
    imdb_id = request.args.get("imdb_id", "").strip()
    if not imdb_id:
        return jsonify(tmdb_id=None, media_type=None)
    # Check DB first
    with db._connect() as conn:
        row = conn.execute(
            "SELECT tmdb_id, media_type FROM requests WHERE imdb_id=? AND tmdb_id IS NOT NULL LIMIT 1",
            (imdb_id,),
        ).fetchone()
    if row:
        return jsonify(tmdb_id=row["tmdb_id"], media_type=row["media_type"])
    try:
        data = tmdb._get(f"/find/{imdb_id}", params={"external_source": "imdb_id"})
        movie_results = data.get("movie_results") or []
        tv_results    = data.get("tv_results") or []
        if movie_results:
            return jsonify(tmdb_id=movie_results[0]["id"], media_type="movie")
        if tv_results:
            return jsonify(tmdb_id=tv_results[0]["id"], media_type="tv")
    except Exception as exc:
        log.debug("TMDB find %s failed: %s", imdb_id, exc)
    return jsonify(tmdb_id=None, media_type=None)


@app.post("/ui/api/users/<int:user_id>/delete")
def ui_api_users_delete(user_id: int):
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    db.delete_user(user_id)
    return jsonify(ok=True)


# ── Auto-add now (trigger immediately) ───────────────────────────────────────

@app.post("/ui/api/auto-add-now")
def ui_api_auto_add_now():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    threading.Thread(target=trending.run, name="auto-add-manual", daemon=True).start()
    return jsonify(ok=True, message="auto-add started in background")


# ── Radarr / Sonarr import ───────────────────────────────────────────────────

@app.get("/ui/api/arr-import/status")
def ui_api_arr_import_status():
    import arr_import
    return jsonify(arr_import.get_status())


@app.post("/ui/api/arr-import/radarr")
def ui_api_arr_import_radarr():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    import arr_import
    only_monitored = (request.get_json(silent=True) or {}).get("only_monitored", True)
    threading.Thread(target=arr_import.import_radarr,
                      kwargs={"only_monitored": only_monitored},
                      name="radarr-import", daemon=True).start()
    return jsonify(ok=True)


@app.post("/ui/api/arr-import/sonarr")
def ui_api_arr_import_sonarr():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    import arr_import
    only_monitored = (request.get_json(silent=True) or {}).get("only_monitored", True)
    threading.Thread(target=arr_import.import_sonarr,
                      kwargs={"only_monitored": only_monitored},
                      name="sonarr-import", daemon=True).start()
    return jsonify(ok=True)


@app.post("/ui/api/arr-import/test-radarr")
def ui_api_arr_import_test_radarr():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    p = request.get_json(silent=True) or {}
    url = p.get("url") or settings.get("RADARR_URL", cfg.RADARR_URL)
    key = p.get("api_key") or settings.get("RADARR_API_KEY", cfg.RADARR_API_KEY)
    if not url or not key:
        return jsonify(ok=False, error="url + api_key required"), 400
    import radarr
    return jsonify(ok=radarr.ping(url, key))


@app.post("/ui/api/arr-import/test-sonarr")
def ui_api_arr_import_test_sonarr():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    p = request.get_json(silent=True) or {}
    url = p.get("url") or settings.get("SONARR_URL", cfg.SONARR_URL)
    key = p.get("api_key") or settings.get("SONARR_API_KEY", cfg.SONARR_API_KEY)
    if not url or not key:
        return jsonify(ok=False, error="url + api_key required"), 400
    import sonarr
    return jsonify(ok=sonarr.ping(url, key))


# ── Modern SPA (React + Vite) served at /app/* ───────────────────────────────

import os as _os
from flask import send_from_directory as _send

_SPA_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static", "app")
_SPA_ASSET_DIR = _os.path.join(_SPA_DIR, "assets")


def _spa_index():
    """Serve the SPA index with a fresh CSRF meta tag injected.
    Falls back to a friendly message if the build is missing."""
    index_path = _os.path.join(_SPA_DIR, "index.html")
    if not _os.path.exists(index_path):
        return (
            "<h1>Mycelium SPA not built</h1>"
            "<p>Run <code>cd frontend && npm install && npm run build</code> "
            "or rebuild the Docker image. The classic UI is at "
            "<a href='/admin'>/admin</a>.</p>"
        ), 503
    with open(index_path, encoding="utf-8") as f:
        html = f.read()
    token = generate_csrf()
    html = html.replace(
        '<meta name="csrf-token" content="" />',
        f'<meta name="csrf-token" content="{token}" />',
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/")
def root_index():
    import settings as _settings
    if LITE_MODE:
        return redirect(url_for("ui_dashboard"))
    if not _settings.get("SETUP_COMPLETE", False):
        return redirect(url_for("setup_wizard"))
    return _spa_index()


@app.get("/app")
@app.get("/app/")
def app_root():
    return _spa_index()


@app.get("/app/assets/<path:filename>")
def app_assets(filename: str):
    return _send(_SPA_ASSET_DIR, filename)


@app.get("/app/<path:subpath>")
def app_catchall(subpath: str):
    full = _os.path.join(_SPA_DIR, subpath)
    if _os.path.isfile(full):
        return _send(_SPA_DIR, subpath)
    return _spa_index()


@app.get("/assets/<path:filename>")
def root_assets(filename: str):
    return _send(_SPA_ASSET_DIR, filename)


_DOCS_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "docs")

@app.get("/docs/<path:filename>")
def docs_file(filename: str):
    return _send(_DOCS_DIR, filename)


@app.get("/<path:subpath>")
def root_catchall(subpath: str):
    full = _os.path.join(_SPA_DIR, subpath)
    if _os.path.isfile(full):
        return _send(_SPA_DIR, subpath)
    return _spa_index()


@app.route(
    f"{cfg.WEBDAV_PATH_PREFIX}/",
    defaults={"path_suffix": ""},
    methods=_WEBDAV_METHODS,
)
@app.route(
    f"{cfg.WEBDAV_PATH_PREFIX}/<path:path_suffix>",
    methods=_WEBDAV_METHODS,
)
def webdav_handler(path_suffix: str):
    return webdav.dispatch(path_suffix)


if __name__ == "__main__":
    log.info("Starting Mycelium on %s:%d", LISTEN_HOST, LISTEN_PORT)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
