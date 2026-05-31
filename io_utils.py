"""Small filesystem helpers shared across the writer modules.

The plain `Path.write_text` / `Path.write_bytes` pattern truncates the
destination before writing the new contents, so a crash or kill mid-write
leaves an empty or partial file on disk. For .strm and .nfo files Jellyfin
reads continuously, that's a long-lived broken entry. The helpers below
write to a uniquely-named sibling tempfile and then atomically rename it
over the destination via os.replace, which is the POSIX rename guarantee
on the same filesystem.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _write(path: Path, mode: str, payload, encoding: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = "".join(path.suffixes) or ""
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=suffix + ".tmp",
        dir=str(path.parent),
    )
    try:
        if encoding is None:
            with os.fdopen(fd, mode) as fh:
                fh.write(payload)
        else:
            with os.fdopen(fd, mode, encoding=encoding) as fh:
                fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write `content` to `path` via a tempfile + os.replace."""
    _write(path, "w", content, encoding)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write `content` to `path` via a tempfile + os.replace."""
    _write(path, "wb", content, None)
