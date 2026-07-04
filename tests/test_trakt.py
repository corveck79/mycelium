import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock heavy imports so trakt.py's module-level `import db` / `import settings`
# don't need a real DB connection.
for _mod in ("db", "settings"):
    sys.modules.setdefault(_mod, MagicMock())

import trakt  # noqa: E402


def test_is_configured_requires_both_client_id_and_secret(monkeypatch):
    monkeypatch.setattr(trakt, "_client_id", lambda: "")
    monkeypatch.setattr(trakt, "_client_secret", lambda: "")
    assert trakt.is_configured() is False
    monkeypatch.setattr(trakt, "_client_id", lambda: "abc")
    assert trakt.is_configured() is False
    monkeypatch.setattr(trakt, "_client_secret", lambda: "xyz")
    assert trakt.is_configured() is True


def test_get_watchlist_normalizes_movies_and_shows(monkeypatch):
    def fake_get(path, token):
        if "movies" in path:
            return [{"movie": {"title": "Dune", "ids": {"imdb": "tt1160419", "tmdb": 438631}}}]
        return [{"show": {"title": "Severance", "ids": {"imdb": "tt11280740", "tmdb": 95396}}}]
    monkeypatch.setattr(trakt, "_get", fake_get)
    result = trakt.get_watchlist("token")
    assert {"imdb_id": "tt1160419", "tmdb_id": 438631, "media_type": "movie", "title": "Dune"} in result
    assert {"imdb_id": "tt11280740", "tmdb_id": 95396, "media_type": "series", "title": "Severance"} in result


def test_get_watchlist_skips_entries_without_imdb_id(monkeypatch):
    monkeypatch.setattr(trakt, "_get", lambda path, token: [{"movie": {"title": "No IMDB", "ids": {}}}])
    assert trakt.get_watchlist("token") == []


def test_valid_access_token_returns_cached_token_when_not_expired():
    user = {"id": 1, "trakt_access_token": "tok", "trakt_token_expires": 9999999999.0}
    assert trakt._valid_access_token(user) == "tok"


def test_valid_access_token_returns_none_without_connection():
    assert trakt._valid_access_token({"id": 1}) is None


def test_sync_watchlist_auto_request_respects_cap_and_dedup(monkeypatch):
    user = {"id": 1, "trakt_access_token": "tok", "trakt_token_expires": 9999999999.0}
    watchlist = [
        {"imdb_id": "tt1", "tmdb_id": 1, "media_type": "movie", "title": "A"},
        {"imdb_id": "tt2", "tmdb_id": 2, "media_type": "movie", "title": "B"},
        {"imdb_id": "tt3", "tmdb_id": 3, "media_type": "movie", "title": "C"},
    ]
    monkeypatch.setattr(trakt, "get_watchlist", lambda token: watchlist)
    monkeypatch.setattr(trakt.db, "get_recent", lambda limit: [{"imdb_id": "tt1", "media_type": "movie"}])
    monkeypatch.setattr(trakt.db, "get_all_monitored_series", lambda: [])
    monkeypatch.setattr(trakt._settings, "get", lambda key, default=None: 1)  # cap=1

    started = []
    class FakeThread:
        def __init__(self, target, args, name, daemon):
            started.append(args[0])
        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", FakeThread)

    added = trakt.sync_watchlist_auto_request(user)
    # tt1 already seen (skipped), cap=1 means only 1 new item queued
    assert added == 1
    assert len(started) == 1
    assert started[0].imdb_id == "tt2"
