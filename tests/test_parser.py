import os
import sys

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from webhook_parser import IgnoreEvent, WebhookError, parse


def test_parse_movie_with_imdb():
    payload = {
        "notification_type": "MEDIA_AUTO_APPROVED",
        "subject": "Dune (2024)",
        "media": {"media_type": "movie", "imdbId": "tt15239678"},
    }
    req = parse(payload)
    assert req.is_movie
    assert req.imdb_id == "tt15239678"
    assert req.title == "Dune (2024)"


def test_parse_series_seasons():
    payload = {
        "notification_type": "MEDIA_APPROVED",
        "subject": "Foundation",
        "media": {"media_type": "tv", "imdbId": "tt0804484"},
        "extra": [{"name": "Requested Seasons", "value": "1, 2"}],
    }
    req = parse(payload)
    assert req.media_type == "series"
    assert req.seasons == [1, 2]


def test_imdb_in_extras():
    payload = {
        "notification_type": "MEDIA_AUTO_APPROVED",
        "media": {"media_type": "movie"},
        "extra": [{"name": "IMDb ID", "value": "tt0111161"}],
    }
    assert parse(payload).imdb_id == "tt0111161"


def test_test_notification_ignored():
    with pytest.raises(IgnoreEvent):
        parse({"notification_type": "TEST_NOTIFICATION"})


def test_missing_imdb():
    with pytest.raises(WebhookError):
        parse({"notification_type": "MEDIA_APPROVED", "media": {"media_type": "movie"}})


def test_series_defaults_to_season_1():
    payload = {
        "notification_type": "MEDIA_APPROVED",
        "media": {"media_type": "tv", "imdbId": "tt0903747"},
    }
    req = parse(payload)
    assert req.seasons == [1]
