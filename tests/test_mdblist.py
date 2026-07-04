import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_MOCKED = ("db", "settings")
_had_prior = {m: sys.modules.get(m) for m in _MOCKED}
for _mod in _MOCKED:
    sys.modules[_mod] = MagicMock()

import mdblist  # noqa: E402

for _mod in _MOCKED:
    if _had_prior[_mod] is None:
        sys.modules.pop(_mod, None)
    else:
        sys.modules[_mod] = _had_prior[_mod]


def test_is_configured_requires_api_key():
    assert mdblist.is_configured({}) is False
    assert mdblist.is_configured({"mdblist_api_key": "abc"}) is True


def test_get_list_items_normalizes_movies_and_shows(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {
                "movies": [{"imdb_id": "tt1", "id": 100, "title": "A Movie"}],
                "shows": [{"imdb_id": "tt2", "id": 200, "title": "A Show"}],
            }
    monkeypatch.setattr(mdblist.requests, "get", lambda *a, **kw: FakeResp())
    items = mdblist.get_list_items(1, "key")
    assert {"imdb_id": "tt1", "tmdb_id": 100, "media_type": "movie", "title": "A Movie"} in items
    assert {"imdb_id": "tt2", "tmdb_id": 200, "media_type": "series", "title": "A Show"} in items


def test_get_list_items_skips_entries_without_imdb_id(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"movies": [{"id": 100, "title": "No IMDB"}], "shows": []}
    monkeypatch.setattr(mdblist.requests, "get", lambda *a, **kw: FakeResp())
    assert mdblist.get_list_items(1, "key") == []


def test_sync_auto_request_returns_zero_without_lists(monkeypatch):
    user = {"mdblist_api_key": "key", "mdblist_list_ids": ""}
    assert mdblist.sync_auto_request(user) == 0


def test_sync_auto_request_returns_zero_without_api_key():
    assert mdblist.sync_auto_request({}) == 0


def test_sync_auto_request_respects_cap_and_dedup(monkeypatch):
    user = {"mdblist_api_key": "key", "mdblist_list_ids": "1"}
    items = [
        {"imdb_id": "tt1", "tmdb_id": 1, "media_type": "movie", "title": "A"},
        {"imdb_id": "tt2", "tmdb_id": 2, "media_type": "movie", "title": "B"},
        {"imdb_id": "tt3", "tmdb_id": 3, "media_type": "movie", "title": "C"},
    ]
    monkeypatch.setattr(mdblist, "get_list_items", lambda list_id, api_key: items)
    monkeypatch.setattr(mdblist.db, "get_recent", lambda limit: [{"imdb_id": "tt1", "media_type": "movie"}])
    monkeypatch.setattr(mdblist.db, "get_all_monitored_series", lambda: [])
    monkeypatch.setattr(mdblist._settings, "get", lambda key, default=None: 1)  # cap=1

    started = []
    class FakeThread:
        def __init__(self, target, args, name, daemon):
            started.append(args[0])
        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", FakeThread)

    added = mdblist.sync_auto_request(user)
    assert added == 1
    assert started[0].imdb_id == "tt2"
