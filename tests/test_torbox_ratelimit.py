"""
Unit tests for the createtorrent rate-limit reservation in torbox.py.

Covers the TOCTOU fix: reserving a slot must count against the budget
immediately (before the HTTP call happens), and releasing a slot after a
failed call must give the budget back.
"""
import os
import sys

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Other test modules (test_strm_generator.py) replace sys.modules["torbox"]
# with a MagicMock at collection time and only restore it once their own
# tests run. Grab a real import for our own use, then put back whatever was
# there so those other files' torbox_mod references stay mocked as they
# expect - our own `torbox` name below stays bound to the real module either way.
_prior_torbox = sys.modules.get("torbox")
sys.modules.pop("torbox", None)
import torbox  # noqa: E402
if _prior_torbox is not None:
    sys.modules["torbox"] = _prior_torbox
else:
    sys.modules.pop("torbox", None)


@pytest.fixture(autouse=True)
def _isolated_log(monkeypatch):
    """Give each test its own in-memory log and skip the DB-backed preload."""
    monkeypatch.setattr(torbox, "_CREATETORRENT_LOG", __import__("collections").deque(maxlen=200))
    monkeypatch.setattr(torbox, "_CREATETORRENT_LOADED", True)
    monkeypatch.setattr(torbox, "_persist_createtorrent", lambda ts, reason: None)
    yield


def test_reservation_counts_immediately():
    entry = torbox._reserve_createtorrent_slot("test")
    assert len(torbox._CREATETORRENT_LOG) == 1
    assert entry in torbox._CREATETORRENT_LOG


def test_release_gives_the_slot_back():
    entry = torbox._reserve_createtorrent_slot("test")
    torbox._release_createtorrent_slot(entry)
    assert len(torbox._CREATETORRENT_LOG) == 0


def test_hourly_limit_blocks_reservation_once_reached(monkeypatch):
    monkeypatch.setattr(torbox, "_CREATETORRENT_LIMIT_MIN", 10_000)  # isolate the hourly check
    for _ in range(torbox._CREATETORRENT_LIMIT_HOUR - 2):
        torbox._reserve_createtorrent_slot("test")
    with pytest.raises(torbox.RateLimited):
        torbox._reserve_createtorrent_slot("test")


def test_released_slot_is_available_again(monkeypatch):
    monkeypatch.setattr(torbox, "_CREATETORRENT_LIMIT_MIN", 10_000)  # isolate the hourly check
    entries = [torbox._reserve_createtorrent_slot("test")
               for _ in range(torbox._CREATETORRENT_LIMIT_HOUR - 2)]
    with pytest.raises(torbox.RateLimited):
        torbox._reserve_createtorrent_slot("test")
    torbox._release_createtorrent_slot(entries[0])
    # Releasing one slot should free up room for exactly one more reservation.
    torbox._reserve_createtorrent_slot("test")
    with pytest.raises(torbox.RateLimited):
        torbox._reserve_createtorrent_slot("test")
