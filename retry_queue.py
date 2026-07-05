"""Smart retry queue with exponential backoff.

Failed requests are enqueued for retry at increasing intervals
(RETRY_BACKOFF_MINUTES). A scheduler picks up due items every
RETRY_QUEUE_INTERVAL_MINUTES and re-runs processor.process for them.
"""
import logging

import db
from config import RETRY_BACKOFF_MINUTES
from webhook_parser import MediaRequest

log = logging.getLogger(__name__)


def schedule(req: MediaRequest, attempt: int) -> None:
    """Enqueue a failed request for retry at the next backoff interval."""
    if attempt >= len(RETRY_BACKOFF_MINUTES):
        log.info("Retry: giving up on %s after %d attempts", req.title, attempt)
        return
    delay = RETRY_BACKOFF_MINUTES[attempt] * 60
    db.enqueue_retry(req.imdb_id, req.title, req.media_type, req.seasons, attempt + 1, delay)
    log.info("Retry: queued %s for attempt %d in %dmin",
             req.title, attempt + 1, RETRY_BACKOFF_MINUTES[attempt])


def run_due() -> int:
    """Process due retries SERIALLY (not as a thread stampede) and stop early
    once the TorBox createtorrent budget is exhausted, so a backlog of
    rate-limited items can't keep re-triggering 429s every cycle.
    Items not processed this round are left in the queue for the next run."""
    import processor  # local import to avoid cycle
    import torbox
    due = db.get_due_retries()
    if not due:
        return 0

    usage = torbox.createtorrent_usage()
    if usage["count"] >= torbox._CREATETORRENT_LIMIT_HOUR - 2:
        log.info("Retry: skipping this cycle  -  createtorrent budget %d/%d (resets ~%dm)",
                 usage["count"], torbox._CREATETORRENT_LIMIT_HOUR,
                 max(1, usage["resets_in_sec"] // 60))
        return 0

    log.info("Retry: processing up to %d due retries (budget %d/%d)",
             len(due), usage["count"], torbox._CREATETORRENT_LIMIT_HOUR)
    processed = 0
    for row in due:
        # Re-check budget before each item; bail out (leave the rest queued) when low.
        if torbox.createtorrent_usage()["count"] >= torbox._CREATETORRENT_LIMIT_HOUR - 2:
            log.info("Retry: budget reached after %d item(s)  -  leaving %d for next cycle",
                     processed, len(due) - processed)
            break
        seasons = [int(s) for s in (row.get("seasons") or "").split(",") if s.strip().isdigit()]
        req = MediaRequest(
            title=row["title"], media_type=row["media_type"],
            imdb_id=row["imdb_id"], seasons=seasons,
        )
        db.remove_retry(row["id"])
        try:
            import metrics_prom
            metrics_prom.retry_attempts_total.inc(1)
        except Exception:
            pass
        # Serial: process inline so we observe quota between items instead of
        # firing N parallel createtorrent calls at once. processor.process()
        # already catches its own failures and re-schedules via schedule()
        # below, but the row was just removed from the queue above - if
        # something outside that internal handling still raises (e.g. a DB
        # error in insert_request), the retry must not be lost, and one bad
        # item must not abort the rest of this batch.
        try:
            processor.process(req, _retry_attempt=row["attempt"])
        except Exception as exc:
            log.error("Retry: processing %s raised unexpectedly, re-queueing: %s", req.title, exc)
            schedule(req, row["attempt"])
        processed += 1
    return processed
