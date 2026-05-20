"""Multi-debrid abstraction.

When MULTI_DEBRID_ENABLED is true, check_cached_multi() queries all configured
providers (TorBox + RealDebrid). The candidate selector in processor.py uses
this to prefer cached releases across providers, falling back to a second
debrid if TorBox doesn't have a release cached.

Currently TorBox is primary and RealDebrid is fallback only — they don't share
.strm files, so a RealDebrid-only release would need RD-specific URL handling
that the .strm generator can't produce yet. The plumbing is in place for when
that's added.
"""
import logging

import settings as _settings
import torbox

log = logging.getLogger(__name__)


def check_cached_multi(hashes: list[str]) -> dict[str, set[str]]:
    """Return {provider: set_of_cached_hashes}. Empty providers omitted."""
    out: dict[str, set[str]] = {}
    if not hashes:
        return out
    out["torbox"] = torbox.check_cached(hashes)
    try:
        import realdebrid
        if realdebrid.is_configured():
            out["realdebrid"] = realdebrid.check_cached(hashes)
    except Exception as exc:
        log.warning("RealDebrid check failed: %s", exc)
    return out


def any_cached(hashes: list[str]) -> set[str]:
    """Hashes cached on any provider."""
    result: set[str] = set()
    for s in check_cached_multi(hashes).values():
        result |= s
    return result
