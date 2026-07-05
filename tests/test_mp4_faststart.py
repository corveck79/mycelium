"""
Unit tests for the stco/co64 offset rewriting in mp4_faststart.py.

Covers the dual-mdat bug fix: for CDN files laid out as
[ftyp][mdat1][moov][mdat2], only chunk offsets pointing into mdat1 (before
moov) may be shifted by +moov_size. Offsets pointing into mdat2 (after moov)
must stay untouched, and a 32-bit stco offset that would overflow must raise
instead of silently wrapping/corrupting the file.
"""
import os
import struct
import sys

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# test_strm_generator.py replaces sys.modules["mp4_faststart"] with a MagicMock
# at collection time and only restores it once its own tests run. Grab a real
# import for our own use, then put back whatever was there so we don't
# accidentally bind that file's torbox_mod-style references to the real
# module for the rest of the session, regardless of file collection order.
_prior_mp4_faststart = sys.modules.get("mp4_faststart")
sys.modules.pop("mp4_faststart", None)
import mp4_faststart as fs  # noqa: E402
if _prior_mp4_faststart is not None:
    sys.modules["mp4_faststart"] = _prior_mp4_faststart
else:
    sys.modules.pop("mp4_faststart", None)


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _stco(offsets: list[int]) -> bytes:
    payload = struct.pack(">II", 0, len(offsets))  # version/flags, entry_count
    for off in offsets:
        payload += struct.pack(">I", off)
    return _box(b"stco", payload)


def _co64(offsets: list[int]) -> bytes:
    payload = struct.pack(">II", 0, len(offsets))
    for off in offsets:
        payload += struct.pack(">Q", off)
    return _box(b"co64", payload)


def _wrap_to_moov(inner: bytes) -> bytes:
    stbl = _box(b"stbl", inner)
    minf = _box(b"minf", stbl)
    mdia = _box(b"mdia", minf)
    trak = _box(b"trak", mdia)
    return _box(b"moov", trak)


def _read_stco_offsets(moov: bytes) -> list[int]:
    i = moov.find(b"stco")
    n = struct.unpack_from(">I", moov, i + 8)[0]
    return [struct.unpack_from(">I", moov, i + 12 + k * 4)[0] for k in range(n)]


def _read_co64_offsets(moov: bytes) -> list[int]:
    i = moov.find(b"co64")
    n = struct.unpack_from(">I", moov, i + 8)[0]
    return [struct.unpack_from(">Q", moov, i + 12 + k * 8)[0] for k in range(n)]


def test_stco_offsets_before_moov_are_shifted():
    moov_offset = 1000
    moov_size = 500
    moov = bytearray(_wrap_to_moov(_stco([100, 900])))  # both point into mdat1
    fs._rewrite_offsets(moov, moov_size, moov_offset)
    assert _read_stco_offsets(bytes(moov)) == [100 + moov_size, 900 + moov_size]


def test_stco_offsets_after_moov_are_untouched():
    moov_offset = 1000
    moov_size = 500
    mdat2_offset = moov_offset + moov_size + 42  # points into mdat2
    moov = bytearray(_wrap_to_moov(_stco([100, mdat2_offset])))
    fs._rewrite_offsets(moov, moov_size, moov_offset)
    offsets = _read_stco_offsets(bytes(moov))
    assert offsets[0] == 100 + moov_size          # mdat1: shifted
    assert offsets[1] == mdat2_offset             # mdat2: unchanged


def test_co64_offsets_split_correctly():
    moov_offset = 5_000_000_000
    moov_size = 1000
    moov = bytearray(_wrap_to_moov(_co64([10, moov_offset + moov_size + 10])))
    fs._rewrite_offsets(moov, moov_size, moov_offset)
    offsets = _read_co64_offsets(bytes(moov))
    assert offsets[0] == 10 + moov_size
    assert offsets[1] == moov_offset + moov_size + 10  # mdat2: unchanged


def test_stco_overflow_raises_instead_of_wrapping():
    moov_offset = 1000
    delta = 0xFFFFFFF0  # pushes the offset just past the 32-bit boundary
    moov = bytearray(_wrap_to_moov(_stco([100])))
    with pytest.raises(ValueError):
        fs._rewrite_offsets(moov, delta, moov_offset)
