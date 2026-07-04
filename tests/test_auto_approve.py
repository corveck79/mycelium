import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock heavy imports so auto_approve.py's module-level `import db`/`import settings`
# don't need a real DB connection, then immediately drop them from sys.modules so
# this doesn't leak into other test files collected afterward (they'd otherwise get
# these mocks instead of the real modules via their own lazy imports).
_MOCKED = ("db", "settings", "processor")
_had_prior = {m: sys.modules.get(m) for m in _MOCKED}
for _mod in _MOCKED:
    sys.modules[_mod] = MagicMock()

import auto_approve  # noqa: E402

for _mod in _MOCKED:
    if _had_prior[_mod] is None:
        sys.modules.pop(_mod, None)
    else:
        sys.modules[_mod] = _had_prior[_mod]


def test_genre_rules_round_trips_through_json(monkeypatch):
    stored = {}
    monkeypatch.setattr(auto_approve._settings, "get", lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(auto_approve._settings, "set", lambda key, value: stored.__setitem__(key, value))

    rules = [{"media_type": "movie", "genre_id": 28, "genre_name": "Action",
              "year_from": 2015, "year_to": 2024, "enabled": True}]
    auto_approve.set_genre_rules(rules)
    assert auto_approve._genre_rules() == rules


def test_genre_rules_invalid_json_returns_empty(monkeypatch):
    monkeypatch.setattr(auto_approve._settings, "get", lambda key, default=None: "not json")
    assert auto_approve._genre_rules() == []


def test_run_genre_fill_skips_disabled_rules(monkeypatch):
    monkeypatch.setattr(auto_approve, "_genre_rules", lambda: [
        {"media_type": "movie", "genre_id": 28, "enabled": False},
    ])
    assert auto_approve.run_genre_fill() == 0


def test_run_genre_fill_respects_daily_cap(monkeypatch):
    monkeypatch.setattr(auto_approve, "_genre_rules", lambda: [
        {"media_type": "movie", "genre_id": 28, "genre_name": "Action", "enabled": True},
    ])
    monkeypatch.setattr(auto_approve._settings, "get", lambda key, default=None: 2)
    monkeypatch.setattr(auto_approve, "_seen_imdb_ids", lambda: set())
    items = [
        {"tmdb_id": 1, "title": "A", "media_type": "movie"},
        {"tmdb_id": 2, "title": "B", "media_type": "movie"},
        {"tmdb_id": 3, "title": "C", "media_type": "movie"},
    ]
    monkeypatch.setattr(auto_approve.tmdb, "discover_by_genre", lambda *a, **kw: items)
    monkeypatch.setattr(auto_approve.tmdb, "tmdb_to_imdb", lambda tmdb_id, media_type: f"tt{tmdb_id}")
    monkeypatch.setattr(auto_approve.processor, "process", lambda req: True)

    added = auto_approve.run_genre_fill()
    assert added == 2  # cap=2, even though 3 candidates were available


def test_run_favorite_actor_fill_excludes_talk_shows(monkeypatch):
    monkeypatch.setattr(auto_approve.db, "get_all_favorite_actors",
                         lambda: [{"person_id": 1, "name": "Actor"}])
    monkeypatch.setattr(auto_approve, "_seen_imdb_ids", lambda: set())
    monkeypatch.setattr(auto_approve._settings, "get", lambda key, default=None: 10)
    monkeypatch.setattr(auto_approve.tmdb, "person_details", lambda person_id: {
        "name": "Actor",
        "filmography": [
            {"tmdb_id": 1, "title": "Movie", "media_type": "movie", "genre_ids": [28]},
            {"tmdb_id": 2, "title": "Talk Show", "media_type": "tv", "genre_ids": [10767]},
        ],
    })
    monkeypatch.setattr(auto_approve.tmdb, "tmdb_to_imdb", lambda tmdb_id, media_type: f"tt{tmdb_id}")
    queued = []
    monkeypatch.setattr(auto_approve.processor, "process", lambda req: queued.append(req.title) or True)

    added = auto_approve.run_favorite_actor_fill()
    assert added == 1
    assert queued == ["Movie"]


def test_queue_skips_already_seen_imdb_id(monkeypatch):
    monkeypatch.setattr(auto_approve.tmdb, "tmdb_to_imdb", lambda tmdb_id, media_type: "tt1")
    called = []
    monkeypatch.setattr(auto_approve.processor, "process", lambda req: called.append(req) or True)
    result = auto_approve._queue({"tmdb_id": 1, "title": "X", "media_type": "movie"}, "test", {"tt1"})
    assert result is False
    assert called == []
