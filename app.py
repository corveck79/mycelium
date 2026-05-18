import logging
import re
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

import catchup
import config as cfg
import db
import jellyfin
import log_buffer
import monitor
import processor
from config import (
    CATCHUP_ENABLED,
    LISTEN_HOST,
    LISTEN_PORT,
    MERGE_VERSIONS_INTERVAL_HOURS,
    MONITOR_INTERVAL_HOURS,
    MOVIE_SYNC_INTERVAL_MINUTES,
    WEBHOOK_SECRET,
    configure_logging,
)
from webhook_parser import IgnoreEvent, MediaRequest, WebhookError, parse

configure_logging()
log_buffer.install()
log = logging.getLogger("seerr-torbox")

app = Flask(__name__)
app.secret_key = "seerr-torbox-ui"

db.init()


def _start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)

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

    if MOVIE_SYNC_INTERVAL_MINUTES > 0:
        scheduler.add_job(
            monitor.sync_movies,
            trigger="interval", minutes=MOVIE_SYNC_INTERVAL_MINUTES,
            id="movie_sync", next_run_time=None,
        )
        log.info("Scheduled movie sync every %dm", MOVIE_SYNC_INTERVAL_MINUTES)

    scheduler.start()
    return scheduler


scheduler = _start_scheduler()

if CATCHUP_ENABLED:
    catchup.schedule()

# Kick off initial movie sync shortly after startup
threading.Thread(target=monitor.sync_movies, name="movie-sync-init", daemon=True).start()


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
def health():
    return jsonify(status="ok")


@app.post("/webhook")
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

    thread = threading.Thread(
        target=processor.process,
        args=(media_request,),
        name=f"process-{media_request.imdb_id}",
        daemon=True,
    )
    thread.start()
    return jsonify(status="accepted", imdb_id=media_request.imdb_id, title=media_request.title), 202


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/ui")
def ui_dashboard():
    return render_template(
        "ui.html",
        requests=db.get_recent(100),
        monitored=db.get_all_monitored_series(),
        wanted=db.get_all_wanted_episodes(),
        movies=db.get_media_items("movie"),
        config=cfg,
    )


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


if __name__ == "__main__":
    log.info("Starting seerr-torbox webhook on %s:%d", LISTEN_HOST, LISTEN_PORT)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
