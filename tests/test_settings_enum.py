import os
import sys

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

sys.modules.pop("settings", None)

import pytest

import db
import settings


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    yield


def test_zilean_mode_accepts_valid_values():
    settings.set("ZILEAN_MODE", "native")
    assert settings.get("ZILEAN_MODE") == "native"
    settings.set("ZILEAN_MODE", "external")
    assert settings.get("ZILEAN_MODE") == "external"


def test_zilean_mode_rejects_invalid_value():
    with pytest.raises(ValueError):
        settings.set("ZILEAN_MODE", "bogus")


def test_zilean_mode_get_falls_back_on_corrupt_stored_value():
    # Simulate a bad value having ended up in the DB some other way (e.g. a
    # stale row from before this enum existed) - get() should not surface it.
    db.set_setting("ZILEAN_MODE", "bogus")
    assert settings.get("ZILEAN_MODE") == settings._config.ZILEAN_MODE


def test_all_for_ui_reports_enum_kind_and_options():
    groups = settings.all_for_ui()
    group = next(g for g in groups if g["id"] == "zilean_native")
    item = next(i for i in group["items"] if i["key"] == "ZILEAN_MODE")
    assert item["kind"] == "enum"
    assert item["options"] == ["external", "native"]
