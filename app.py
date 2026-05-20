import logging
import re
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

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

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = cfg.AUTH_SESSION_SECRET
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

# CSRF protection on all state-changing endpoints; external webhooks opt out below.
from flask_wtf.csrf import CSRFProtect, generate_csrf
_csrf = CSRFProtect(app)


@app.context_processor
def _inject_csrf_token():
    return {"csrf_token": generate_csrf}


# Rate limiter — applied selectively to auth endpoints.
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],  # opt-in per route
    storage_uri="memory://",
)

db.init()

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
    nxt = request.form.get("next") or "/ui"
    if auth.attempt_login(username, password):
        _session["user"] = username
        return redirect(nxt)
    return redirect(url_for("login_view", error="1", next=nxt))


@app.get("/logout")
def logout_view():
    from flask import session as _session
    _session.pop("user", None)
    return redirect(url_for("login_view"))


@app.post("/ui/set-password")
def ui_set_password():
    if not auth.is_enabled() and not auth.current_user() and not request.form.get("force_first"):
        # Don't expose this when auth is off — the user wouldn't get gated anyway
        return jsonify(error="auth disabled"), 400
    new_pw = request.form.get("password") or ""
    if len(new_pw) < 6:
        flash("Password must be at least 6 characters", "err")
        return redirect(url_for("ui_dashboard") + "#settings")
    auth.set_password(new_pw)
    flash("Password updated", "ok")
    return redirect(url_for("ui_dashboard") + "#settings")


def _start_scheduler() -> BackgroundScheduler:
    # job_defaults: every interval job gets +/-60s jitter to avoid stampede when
    # multiple long-running jobs hit the same minute mark.
    scheduler = BackgroundScheduler(
        daemon=True,
        job_defaults={"jitter": 60, "coalesce": True, "max_instances": 1},
    )

    if MERGE_VERSIONS_INTERVAL_HOURS > 0:
        scheduler.add_job(
            jellyfin.merge_duplicate_versions,
            trigger="interval", hours=MERGE_VERSIONS_INTERVAL_HOURS,
            id="merge_versions", next_run_time=None,
        )
        log.info("Scheduled MergeVersions every %dh", MERGE_VERSIONS_INTERVAL_HOURS)

    if MONITOR_INTERVAL_HOURS > 0:
        scheduler.add_job(
            monitor.run_series_check,
            trigger="interval", hours=MONITOR_INTERVAL_HOURS,
            id="series_monitor", next_run_time=None,
        )
        log.info("Scheduled series monitor every %dh", MONITOR_INTERVAL_HOURS)

    if MOVIE_SYNC_INTERVAL_MINUTES > 0 and cfg.SEERR_URL:
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
        log.info("Seerr sync skipped — SEERR_URL not configured (using SPA discovery instead)")

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
            # Job may not exist if a feature is disabled — that's expected.
            log.debug("modify_job(%s): %s", jid, exc)

    scheduler.start()
    return scheduler


scheduler = _start_scheduler()

if CATCHUP_ENABLED:
    catchup.schedule()

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


# ── Auth ──────────────────────────────────────────────────────────────────────

def _check_auth() -> None:
    if not WEBHOOK_SECRET:
        return
    provided = request.headers.get("X-Webhook-Secret") or request.args.get("secret")
    if provided != WEBHOOK_SECRET:
        log.warning("Rejected webhook with bad/missing secret from %s", request.remote_addr)
        abort(401)


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health_simple():
    """Liveness probe used by Docker HEALTHCHECK — process up + DB reachable."""
    try:
        db.get_recent(1)
        return jsonify(status="ok")
    except Exception as exc:
        return jsonify(status="degraded", error=str(exc)[:120]), 503


@app.get("/metrics")
def metrics_export():
    """Prometheus scrape endpoint."""
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
    payload = request.get_json(silent=True) or {}
    log.info("TorBox webhook: %s", payload)
    threading.Thread(target=strm_generator.run_and_refresh, name="torbox-push", daemon=True).start()
    return jsonify(status="ok")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/ui")
def ui_dashboard():
    import settings as _settings
    if not _settings.get("SETUP_COMPLETE", False):
        return redirect(url_for("setup_wizard"))
    return render_template(
        "ui.html",
        requests=db.get_recent(100),
        monitored=db.get_all_monitored_series(),
        wanted=db.get_all_wanted_episodes(),
        movies=db.get_media_items("movie"),
        repair_items=db.get_repair_items(200),
        last_cleanup=db.get_last_cleanup_run(),
        activity=db.get_activity(50),
        config=cfg,
    )


# ── Setup wizard ──────────────────────────────────────────────────────────────

@app.get("/setup")
def setup_wizard():
    return render_template("setup.html")


@app.post("/setup/skip")
def setup_skip():
    import settings as _settings
    _settings.set("SETUP_COMPLETE", True)
    return jsonify(ok=True)


@app.post("/setup/save")
def setup_save():
    import settings as _settings
    saved = 0
    for key, value in request.form.items():
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
def setup_test(kind: str):
    """Test a single integration using values posted from the wizard form."""
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

        return jsonify(ok=False, error="unknown test"), 400
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)[:120])


@app.post("/ui/submit")
def ui_submit():
    imdb_id = (request.form.get("imdb_id") or "").strip()
    media_type = request.form.get("media_type", "movie")
    seasons_raw = request.form.get("seasons", "1")

    if not re.fullmatch(r"tt\d{6,10}", imdb_id):
        flash("Invalid IMDB ID — must be tt followed by 6-10 digits.", "err")
        return redirect(url_for("ui_dashboard"))
    if media_type not in ("movie", "series"):
        flash("Invalid media type.", "err")
        return redirect(url_for("ui_dashboard"))

    seasons = [int(s.strip()) for s in re.split(r"[,\s]+", seasons_raw) if s.strip().isdigit()]
    if media_type == "series" and not seasons:
        seasons = [1]

    media_request = MediaRequest(
        title=imdb_id, media_type=media_type, imdb_id=imdb_id, seasons=seasons,
    )
    threading.Thread(target=processor.process, args=(media_request,),
                     name=f"manual-{imdb_id}", daemon=True).start()
    flash(f"Queued: {imdb_id} ({media_type})", "ok")
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
    media_request = MediaRequest(
        title=imdb_id, media_type="movie", imdb_id=imdb_id, seasons=[],
    )
    db.update_media_item_status(imdb_id, "movie", "processing")
    threading.Thread(target=processor.process, args=(media_request,),
                     name=f"movie-{imdb_id}", daemon=True).start()
    flash(f"Download queued for {imdb_id}", "ok")
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
    flash("Cleanup scan started — check Repair tab for results", "ok")
    return redirect(url_for("ui_dashboard") + "#repair")


@app.post("/ui/repair-all")
def ui_repair_all():
    threading.Thread(target=cleanup.run_cleanup, name="repair-all-manual", daemon=True).start()
    flash("Repair All started — check Repair tab for results", "ok")
    return redirect(url_for("ui_dashboard") + "#repair")


@app.post("/ui/refresh-images")
def ui_refresh_images():
    threading.Thread(target=jellyfin.refresh_missing_images, name="jf-images", daemon=True).start()
    flash("Jellyfin image refresh started — missing posters will be fetched", "ok")
    return redirect(url_for("ui_dashboard") + "#repair")


@app.post("/ui/merge-series")
def ui_merge_series():
    threading.Thread(target=cleanup.merge_series_duplicates, name="merge-series", daemon=True).start()
    flash("Series merge started — duplicate folders will be consolidated", "ok")
    return redirect(url_for("ui_dashboard") + "#repair")


@app.post("/ui/generate-nfos")
def ui_generate_nfos():
    threading.Thread(target=nfo_generator.generate_all, name="nfo-manual", daemon=True).start()
    flash("NFO generation started — Jellyfin will pick up metadata on next scan", "ok")
    return redirect(url_for("ui_dashboard") + "#repair")


# ── New JSON APIs ─────────────────────────────────────────────────────────────

@app.get("/ui/api/health")
def ui_api_health():
    return jsonify(services=health.check_all())


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
        streams = zilean.fetch_streams(imdb_id) if cfg.ZILEAN_ENABLED else []
        candidates = torrentio.rank_streams(streams)
        if not candidates:
            streams = torrentio.fetch_streams("movie", imdb_id)
            candidates = torrentio.rank_streams(streams)
    else:
        streams = zilean.fetch_streams(imdb_id, season=season, episode=episode) if cfg.ZILEAN_ENABLED else []
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
        flash("Magnet added to TorBox — rescan will create .strm shortly", "ok")
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
    import time as _t
    started = _t.monotonic()
    ua = request.headers.get("User-Agent", "?")[:80]
    rng = request.headers.get("Range", "-")
    url = catbox.materialize(token)
    elapsed = _t.monotonic() - started
    if not url:
        log.warning("stream: materialize FAILED token=%s ua=%r range=%s (%.1fs)",
                    token, ua, rng, elapsed)
        abort(404)
    log.info("stream: token=%s → CDN redirect (%.1fs) ua=%r range=%s",
             token, elapsed, ua, rng)
    # 302 is followed more reliably by some mobile players / ffmpeg than 307.
    return redirect(url, code=302)


@app.get("/ui/api/virtual-items")
def ui_api_virtual_items():
    items = db.get_all_virtual_items()
    return jsonify(items=[{
        "id": i["id"], "token": i["token"], "title": i["title"], "media_type": i["media_type"],
        "torbox_id": i["torbox_id"], "in_torbox": bool(i["torbox_id"]),
        "play_count": i["play_count"], "last_played": i["last_played"],
        "created_at": i["created_at"], "info_hash": i["info_hash"],
    } for i in items])


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


@app.post("/ui/library-import")
def ui_library_import():
    threading.Thread(target=_library_import_and_resolve,
                     name="lib-import", daemon=True).start()
    flash("Library import started — check Logs for progress", "ok")
    return redirect(url_for("ui_dashboard") + "#overview")


@app.post("/ui/recovery")
def ui_recovery():
    threading.Thread(target=recovery.run, name="recovery-wizard", daemon=True).start()
    flash("Recovery wizard started — runs integrity check + cleanup + import + strm scan", "ok")
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
    return jsonify(results=results)


@app.get("/ui/api/discover/trending")
def ui_api_discover_trending():
    media = request.args.get("type", "all")  # all | movie | tv
    window = request.args.get("window", "week")  # day | week
    return jsonify(results=tmdb.trending(media, window))


@app.get("/ui/api/discover/popular")
def ui_api_discover_popular():
    media = request.args.get("type", "movie")
    region = request.args.get("region") or cfg.AUTO_ADD_REGION
    return jsonify(results=tmdb.popular(media, region=region))


@app.get("/ui/api/discover/top-rated")
def ui_api_discover_top_rated():
    media = request.args.get("type", "movie")
    return jsonify(results=tmdb.top_rated(media))


@app.get("/ui/api/discover/now-playing")
def ui_api_discover_now_playing():
    region = request.args.get("region") or cfg.AUTO_ADD_REGION
    return jsonify(results=tmdb.now_playing(region=region))


@app.get("/ui/api/discover/upcoming")
def ui_api_discover_upcoming():
    region = request.args.get("region") or cfg.AUTO_ADD_REGION
    return jsonify(results=tmdb.upcoming(region=region))


@app.get("/ui/api/discover/on-the-air")
def ui_api_discover_on_the_air():
    return jsonify(results=tmdb.on_the_air())


@app.get("/ui/api/discover/providers")
def ui_api_discover_providers():
    media = request.args.get("type", "movie")
    region = request.args.get("region") or cfg.AUTO_ADD_REGION
    return jsonify(providers=tmdb.list_providers(media, region=region))


@app.get("/ui/api/discover/by-provider")
def ui_api_discover_by_provider():
    media = request.args.get("type", "movie")
    pid = int(request.args.get("provider_id") or "0")
    region = request.args.get("region") or cfg.AUTO_ADD_REGION
    if not pid:
        return jsonify(error="provider_id required"), 400
    return jsonify(results=tmdb.discover_by_provider(media, pid, region=region))


@app.get("/ui/api/discover/details")
def ui_api_discover_details():
    media = request.args.get("type", "movie")
    tmdb_id = int(request.args.get("id") or "0")
    region = request.args.get("region") or cfg.AUTO_ADD_REGION
    if not tmdb_id:
        return jsonify(error="id required"), 400
    detail = tmdb.details(media, tmdb_id, region=region)
    if not detail:
        return jsonify(error="not found"), 404
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
        # 'future' mode: don't eagerly fetch the back-catalog — let the monitor
        # pick up episodes as they air. Eagerly process only for all/selected.
        process_seasons = [] if monitor_mode == "future" else monitored
        req = MediaRequest(title=title, media_type="series",
                            imdb_id=imdb_id, seasons=process_seasons)
        if not process_seasons:
            # Nothing to fetch now; the series is monitored and the periodic
            # check will grab future episodes.
            return
    else:
        req = MediaRequest(title=title, media_type="movie", imdb_id=imdb_id, seasons=[])
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
    return jsonify(items=db.get_watchlist(rec["id"]))


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
    if rec.get("role") == "admin":
        status = request.args.get("status") or None
        return jsonify(items=db.get_user_requests(status=status))
    return jsonify(items=db.get_user_requests(user_id=rec["id"]))


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
def ui_api_wanted_recheck():
    threading.Thread(target=upgrader.recheck_wanted, name="wanted-recheck-manual",
                     daemon=True).start()
    return jsonify(ok=True, message="wanted recheck started")


@app.get("/ui/api/wanted-episodes")
def ui_api_wanted_episodes():
    return jsonify(items=db.get_all_wanted_episodes())


@app.get("/ui/api/torbox-quota")
def ui_api_torbox_quota():
    """createtorrent usage in the last hour, broken down by reason — explains
    why TorBox 429 rate limits are being hit."""
    return jsonify(torbox.createtorrent_usage())


@app.get("/ui/api/session")
def ui_api_session():
    """Current session info: who am I, what role, do I have auto_approve."""
    rec = auth.current_user_record()
    if not rec:
        return jsonify(authenticated=False, user=None)
    return jsonify(authenticated=True, user={
        "id": rec.get("id"),
        "username": rec.get("username"),
        "role": rec.get("role"),
        "auto_approve": bool(rec.get("auto_approve")),
    })


@app.get("/ui/api/users")
def ui_api_users():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    return jsonify(users=db.list_users())


@app.post("/ui/api/users/create")
def ui_api_users_create():
    # Bootstrap: if no users exist yet, allow creating the first one (forced to admin)
    if db.user_count() == 0:
        p = request.get_json(silent=True) or {}
        username = (p.get("username") or "").strip()
        password = p.get("password") or ""
        if not username or len(password) < 4:
            return jsonify(error="username + password (≥4 chars) required"), 400
        try:
            uid = auth.create_user_account(username, password, role="admin",
                                            auto_approve=True)
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
    if p.get("password"):
        if len(p["password"]) < 4:
            return jsonify(error="password too short"), 400
        fields["password_hash"] = auth.hash_password(p["password"])
    db.update_user(user_id, **fields)
    return jsonify(ok=True)


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
    url = p.get("url") or cfg.RADARR_URL
    key = p.get("api_key") or cfg.RADARR_API_KEY
    if not url or not key:
        return jsonify(ok=False, error="url + api_key required"), 400
    import radarr
    return jsonify(ok=radarr.ping(url, key))


@app.post("/ui/api/arr-import/test-sonarr")
def ui_api_arr_import_test_sonarr():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    p = request.get_json(silent=True) or {}
    url = p.get("url") or cfg.SONARR_URL
    key = p.get("api_key") or cfg.SONARR_API_KEY
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
            "<a href='/ui'>/ui</a>.</p>"
        ), 503
    with open(index_path, encoding="utf-8") as f:
        html = f.read()
    token = generate_csrf()
    html = html.replace(
        '<meta name="csrf-token" content="" />',
        f'<meta name="csrf-token" content="{token}" />',
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/app")
@app.get("/app/")
def app_root():
    return _spa_index()


@app.get("/app/assets/<path:filename>")
def app_assets(filename: str):
    return _send(_SPA_ASSET_DIR, filename)


@app.get("/app/<path:subpath>")
def app_catchall(subpath: str):
    # Serve static files from the build dir if they exist; otherwise serve the
    # SPA index so React Router can take over the route.
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
