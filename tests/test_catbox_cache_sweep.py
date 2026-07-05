"""
Unit tests for catbox._sweep_caches(): pruning expired entries from the
in-memory caches that are otherwise only cleaned up on read.
"""
import os
import sys
import time

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")
os.environ.setdefault("MEDIA_PATH", "/tmp/mycelium-test-media")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# test_strm_generator.py replaces sys.modules["db"/"settings"/"torbox"] with
# MagicMocks at collection time and only restores them once its own tests
# run. Grab real imports for our own use, then put back whatever was there.
_MOCKED = ("db", "settings", "torbox")
_prior = {m: sys.modules.get(m) for m in _MOCKED}
for m in _MOCKED:
    sys.modules.pop(m, None)
import catbox  # noqa: E402
for m in _MOCKED:
    if _prior[m] is not None:
        sys.modules[m] = _prior[m]
    else:
        sys.modules.pop(m, None)


@pytest.fixture(autouse=True)
def _clear_caches():
    catbox._url_cache.clear()
    catbox._fail_cache.clear()
    catbox._search_cache.clear()
    catbox._recent_tokens.clear()
    yield
    catbox._url_cache.clear()
    catbox._fail_cache.clear()
    catbox._search_cache.clear()
    catbox._recent_tokens.clear()


def test_sweep_removes_expired_entries_only():
    now = time.monotonic()
    catbox._url_cache["expired"] = ("http://x", now - 1)
    catbox._url_cache["fresh"] = ("http://y", now + 100)
    catbox._fail_cache["expired"] = now - 1
    catbox._fail_cache["fresh"] = now + 100
    catbox._search_cache[("tt1", None, None)] = (now - 1, "stale-result")
    catbox._search_cache[("tt2", None, None)] = (now + 100, "fresh-result")
    catbox._recent_tokens["old"] = now - catbox._SCAN_WINDOW_SEC - 1
    catbox._recent_tokens["recent"] = now

    catbox._sweep_caches()

    assert list(catbox._url_cache.keys()) == ["fresh"]
    assert list(catbox._fail_cache.keys()) == ["fresh"]
    assert list(catbox._search_cache.keys()) == [("tt2", None, None)]
    assert list(catbox._recent_tokens.keys()) == ["recent"]


def test_sweep_is_a_no_op_on_empty_caches():
    catbox._sweep_caches()  # must not raise
    assert catbox._url_cache == {}
    assert catbox._fail_cache == {}
    assert catbox._search_cache == {}
    assert catbox._recent_tokens == {}
