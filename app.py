import logging
import re
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

import catchup
import db
import jellyfin
import log_buffer
import processor
from config import (
    CATCHUP_ENABLED,
    LISTEN_HOST,
    LISTEN_PORT,
    MERGE_VERSIONS_INTERVAL_HOURS,
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


def _start_scheduler() -> BackgroundScheduler | None:
    if MERGE_VERSIONS_INTERVAL_HOURS <= 0:
        log.info("MergeVersions scheduler disabled (interval=%d)", MERGE_VERSIONS_INTERVAL_HOURS)
        return None
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        jellyfin.merge_duplicate_versions,
        trigger="interval",
        hours=MERGE_VERSIONS_INTERVAL_HOURS,
        id="merge_versions",
        next_run_time=None,
    )
    scheduler.start()
    log.info("Scheduled Jellyfin MergeVersions every %d hours", MERGE_VERSIONS_INTERVAL_HOURS)
    return scheduler


scheduler = _start_scheduler()

if CATCHUP_ENABLED:
    catchup.schedule()


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
    rows = db.get_recent(100)
    return render_template("ui.html", rows=rows)


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
        title=imdb_id,
        media_type=media_type,
        imdb_id=imdb_id,
        seasons=seasons,
    )
    thread = threading.Thread(
        target=processor.process,
        args=(media_request,),
        name=f"manual-{imdb_id}",
        daemon=True,
    )
    thread.start()
    flash(f"Request queued: {imdb_id} ({media_type})", "ok")
    return redirect(url_for("ui_dashboard"))


@app.get("/ui/logs")
def ui_logs():
    return jsonify(lines=log_buffer.get_lines(100))


if __name__ == "__main__":
    log.info("Starting seerr-torbox webhook on %s:%d", LISTEN_HOST, LISTEN_PORT)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
