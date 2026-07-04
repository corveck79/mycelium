import os
import sys

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

sys.modules.pop("settings", None)

import lz_string
import zilean_index


def test_lz_string_roundtrip_known_vector():
    # Generated with the canonical JS lz-string lib:
    #   LZString.compressToEncodedURIComponent(JSON.stringify([{filename:..., hash:..., bytes:...}]))
    compressed = (
        "NobwRAZglgNgpgOwIYFs5gFxgCoAs4B0AskgC4BOUAHgQIwCcjdADABzMAOBA6gKIBCAWgAiAGTAAaMLiQBnXJjBJlK1WvUbNqyWABGAT1JxZmAEzMLlywF8AukA"
    )
    result = lz_string.decompress_from_encoded_uri_component(compressed)
    assert result == (
        '[{"filename":"The.Matrix.1999.1080p.WEB-DL",'
        '"hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","bytes":2000000000}]'
    )


def test_lz_string_empty_string_returns_none():
    assert lz_string.decompress_from_encoded_uri_component("") is None


def test_lz_string_none_returns_empty():
    assert lz_string.decompress_from_encoded_uri_component(None) == ""


def test_parse_size_to_bytes_formatted_string():
    assert zilean_index._parse_size_to_bytes("1.64 GB") == int(1.64 * 1024 ** 3)
    assert zilean_index._parse_size_to_bytes("500 MB") == int(500 * 1024 ** 2)
    assert zilean_index._parse_size_to_bytes(None) == 0
    assert zilean_index._parse_size_to_bytes(12345) == 12345


def test_normalize_strips_punctuation_and_lowercases():
    assert zilean_index._normalize("The.Matrix.1999.1080p.WEB-DL") == "the matrix 1999 1080p web dl"


def test_search_and_import_roundtrip(tmp_path, monkeypatch):
    db_path = str(tmp_path / "zilean_test.db")
    monkeypatch.setattr(zilean_index, "_DB_PATH", db_path)
    zilean_index._tls.conn = None

    conn = zilean_index._connect()
    conn.execute(
        "INSERT INTO hashes (info_hash, raw_title, title_norm, size_bytes) VALUES (?, ?, ?, ?)",
        ("a" * 40, "The Matrix 1999 1080p WEB-DL", zilean_index._normalize("The Matrix 1999 1080p WEB-DL"), 2_000_000_000),
    )
    conn.execute(
        "INSERT INTO hashes (info_hash, raw_title, title_norm, size_bytes) VALUES (?, ?, ?, ?)",
        ("b" * 40, "Some Other Movie 2020", zilean_index._normalize("Some Other Movie 2020"), 1_000_000_000),
    )
    conn.commit()

    results = zilean_index.search("The Matrix 1999")
    assert len(results) == 1
    assert results[0]["info_hash"] == "a" * 40

    no_match = zilean_index.search("Nonexistent Title Xyz")
    assert no_match == []


def test_search_season_pack_and_episode_filtering(tmp_path, monkeypatch):
    db_path = str(tmp_path / "zilean_test2.db")
    monkeypatch.setattr(zilean_index, "_DB_PATH", db_path)
    zilean_index._tls.conn = None

    conn = zilean_index._connect()
    rows = [
        ("a" * 40, "Show.Name.S01.Complete.1080p"),
        ("b" * 40, "Show.Name.S01E02.1080p"),
        ("c" * 40, "Show.Name.S02E01.1080p"),
    ]
    for info_hash, title in rows:
        conn.execute(
            "INSERT INTO hashes (info_hash, raw_title, title_norm, size_bytes) VALUES (?, ?, ?, 0)",
            (info_hash, title, zilean_index._normalize(title)),
        )
    conn.commit()

    results = zilean_index.search("Show Name", season=1, episode=2)
    found_hashes = {r["info_hash"] for r in results}
    assert "a" * 40 in found_hashes  # season pack matches any episode
    assert "b" * 40 in found_hashes  # exact episode match
    assert "c" * 40 not in found_hashes  # different season, not a match


def test_get_status_reports_total_and_mode(tmp_path, monkeypatch):
    db_path = str(tmp_path / "zilean_status.db")
    monkeypatch.setattr(zilean_index, "_DB_PATH", db_path)
    zilean_index._tls.conn = None

    status = zilean_index.get_status()
    assert status["total_hashes"] == 0
    assert status["last_status"] == "never"
