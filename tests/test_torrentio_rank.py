import os
import sys

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# test_strm_generator.py mocks sys.modules["settings"] at import time and never
# tears it down, which leaks into any test file collected after it and does a
# real `import settings` (as torrentio.rank_streams() does internally). Drop
# any stale mock so we get the real settings module here.
sys.modules.pop("settings", None)

import torrentio
from torrentio import TorrentioStream


def _stream(name, quality, size_gb, seeders=10):
    return TorrentioStream(
        name=name, title=name, info_hash="a" * 40, quality=quality,
        seeders=seeders, size_gb=size_gb, is_season_pack=False,
    )


def test_undersized_fake_quality_release_is_rejected():
    # A 90-minute movie claiming 2160p but only 500MB  -  physically impossible,
    # almost certainly a mislabeled cam or trailer.
    fake = _stream("Movie.2024.2160p.WEB-DL", "2160p", size_gb=0.5)
    real = _stream("Movie.2024.1080p.WEB-DL", "1080p", size_gb=2.0)
    ranked = torrentio.rank_streams([fake, real], override={"runtime_minutes": 90})
    hashes_kept = [s.name for s in ranked]
    assert "Movie.2024.2160p.WEB-DL" not in hashes_kept
    assert "Movie.2024.1080p.WEB-DL" in hashes_kept


def test_undersized_filter_scales_with_runtime():
    # A short (40-minute) title needs proportionally less data than a 90-minute
    # baseline  -  the same 1080p size that would fail for a 90-min film should
    # pass for something this short.
    short_ok = _stream("Special.2024.1080p.WEB-DL", "1080p", size_gb=0.6)
    ranked = torrentio.rank_streams([short_ok], override={"runtime_minutes": 40})
    assert len(ranked) == 1


def test_unknown_size_is_never_penalized():
    unknown = _stream("Movie.2024.2160p.WEB-DL", "2160p", size_gb=0.0)
    ranked = torrentio.rank_streams([unknown], override={"runtime_minutes": 90})
    assert len(ranked) == 1


def test_no_runtime_known_skips_the_filter():
    fake = _stream("Movie.2024.2160p.WEB-DL", "2160p", size_gb=0.1)
    ranked = torrentio.rank_streams([fake], override={})
    assert len(ranked) == 1


def test_all_candidates_undersized_falls_back_to_allowing_them():
    fake1 = _stream("Movie.2024.2160p.WEB-DL", "2160p", size_gb=0.1)
    fake2 = _stream("Movie.2024.1080p.WEB-DL", "1080p", size_gb=0.05)
    ranked = torrentio.rank_streams([fake1, fake2], override={"runtime_minutes": 90})
    assert len(ranked) == 2
