import logging

import db
from config import BLACKLIST_FAIL_THRESHOLD

log = logging.getLogger(__name__)


def is_blacklisted(info_hash: str) -> bool:
    rec = db.get_failed_hash(info_hash)
    return bool(rec and rec["fail_count"] >= BLACKLIST_FAIL_THRESHOLD)


def record_failure(info_hash: str, error: str | None = None) -> None:
    db.record_failed_hash(info_hash, error)
    rec = db.get_failed_hash(info_hash)
    if rec and rec["fail_count"] >= BLACKLIST_FAIL_THRESHOLD:
        log.warning("Hash %s now blacklisted (%d failures)", info_hash, rec["fail_count"])


def filter_candidates(candidates: list) -> list:
    """Remove blacklisted hashes from a candidate list."""
    if not candidates:
        return candidates
    blacklisted = db.get_blacklisted_hashes(BLACKLIST_FAIL_THRESHOLD)
    if not blacklisted:
        return candidates
    filtered = [c for c in candidates if c.info_hash not in blacklisted]
    if len(filtered) < len(candidates):
        log.info("Filtered %d blacklisted candidate(s)", len(candidates) - len(filtered))
    return filtered
