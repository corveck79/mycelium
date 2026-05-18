import logging
import threading

from flask import Flask, abort, jsonify, request

import processor
from config import LISTEN_HOST, LISTEN_PORT, WEBHOOK_SECRET, configure_logging
from webhook_parser import IgnoreEvent, WebhookError, parse

configure_logging()
log = logging.getLogger("seerr-torbox")

app = Flask(__name__)


def _check_auth() -> None:
    if not WEBHOOK_SECRET:
        return
    provided = request.headers.get("X-Webhook-Secret") or request.args.get("secret")
    if provided != WEBHOOK_SECRET:
        log.warning("Rejected webhook with bad/missing secret from %s", request.remote_addr)
        abort(401)


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

    # Run the long-running fulfillment off the request thread so Seerr gets a fast 200.
    thread = threading.Thread(
        target=processor.process,
        args=(media_request,),
        name=f"process-{media_request.imdb_id}",
        daemon=True,
    )
    thread.start()
    return jsonify(status="accepted", imdb_id=media_request.imdb_id, title=media_request.title), 202


if __name__ == "__main__":
    log.info("Starting seerr-torbox webhook on %s:%d", LISTEN_HOST, LISTEN_PORT)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
