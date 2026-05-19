import logging
import re
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

import catchup
import cleanup
import config as cfg
import db
import health
import jellyfin
import log_buffer
import monitor
import notify
import processor
import stats
import strm_generator
import tmdb
import torbox
import torrentio
import zilean
from config import (
    CATCHUP_ENABLED,
    CLEANUP_INTERVAL_HOURS,
    LISTEN_HOST,
    LISTEN_PORT,
    MERGE_VERSIONS_INTERVAL_HOURS,
    MONITOR_INTERVAL_HOURS,
    MOVIE_SYNC_INTERVAL_MINUTES,
    STRM_GENERATOR_INTERVAL_HOURS,
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

    scheduler.start()
    return scheduler


scheduler = _start_scheduler()

if CATCHUP_ENABLED:
    catchup.schedule()

# Kick off initial movie sync and strm scan shortly after startup
threading.Thread(target=monitor.sync_movies, name="movie-sync-init", daemon=True).start()
threading.Thread(target=strm_generator.run_and_refresh, name="strm-init", daemon=True).start()


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
        repair_items=db.get_repair_items(200),
        last_cleanup=db.get_last_cleanup_run(),
        activity=db.get_activity(50),
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
        torbox.add_magnet(magnet)
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


if __name__ == "__main__":
    log.info("Starting seerr-torbox webhook on %s:%d", LISTEN_HOST, LISTEN_PORT)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
