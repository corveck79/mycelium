import os
import sys

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tmdb


def test_person_details_normalizes_filmography(monkeypatch):
    def fake_get(path, params=None, timeout=10):
        assert path == "/person/123"
        return {
            "id": 123,
            "name": "Timothee Chalamet",
            "biography": "An actor.",
            "profile_path": "/profile.jpg",
            "birthday": "1995-12-27",
            "place_of_birth": "New York",
            "combined_credits": {
                "cast": [
                    {"media_type": "movie", "id": 1, "title": "Dune", "character": "Paul Atreides",
                     "release_date": "2021-10-21", "vote_average": 8.0, "vote_count": 1000,
                     "popularity": 99.0, "overview": "...", "poster_path": "/p1.jpg", "backdrop_path": None},
                    {"media_type": "tv", "id": 2, "name": "Homeland", "character": "Guest",
                     "first_air_date": "2013-01-01", "vote_average": 7.0, "vote_count": 500,
                     "popularity": 10.0, "overview": "...", "poster_path": None, "backdrop_path": None},
                    {"media_type": "person", "id": 3},  # non-movie/tv credit types are skipped
                ],
            },
        }
    monkeypatch.setattr(tmdb, "_get", fake_get)

    person = tmdb.person_details(123)
    assert person["name"] == "Timothee Chalamet"
    assert len(person["filmography"]) == 2
    titles = {item["title"] for item in person["filmography"]}
    assert titles == {"Dune", "Homeland"}
    dune = next(item for item in person["filmography"] if item["title"] == "Dune")
    assert dune["character"] == "Paul Atreides"
    assert dune["media_type"] == "movie"


def test_person_details_returns_none_when_tmdb_unavailable(monkeypatch):
    monkeypatch.setattr(tmdb, "_get", lambda *a, **kw: None)
    assert tmdb.person_details(123) is None


def test_person_details_dedupes_repeated_credits(monkeypatch):
    def fake_get(path, params=None, timeout=10):
        return {
            "id": 1, "name": "Actor", "biography": "", "profile_path": None,
            "birthday": None, "place_of_birth": None,
            "combined_credits": {"cast": [
                {"media_type": "movie", "id": 5, "title": "A", "release_date": "2020-01-01"},
                {"media_type": "movie", "id": 5, "title": "A", "release_date": "2020-01-01"},
            ]},
        }
    monkeypatch.setattr(tmdb, "_get", fake_get)
    person = tmdb.person_details(1)
    assert len(person["filmography"]) == 1


def test_cast_includes_person_id(monkeypatch):
    def fake_get(path, params=None, timeout=10):
        return {
            "id": 1, "title": "Movie", "release_date": "2020-01-01",
            "credits": {"cast": [{"id": 42, "name": "Actor", "character": "Role", "profile_path": None}]},
        }
    monkeypatch.setattr(tmdb, "_get", fake_get)
    result = tmdb.details("movie", 1)
    assert result["cast"][0]["id"] == 42
