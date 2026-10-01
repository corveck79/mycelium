"""Mask API keys and tokens in log output.

Used by log_buffer to scrub every log record (message and traceback) before it reaches the
in-memory buffer or stdout, and by call sites that log exceptions from HTTP clients, whose text
often contains the full request URL including query-string credentials.
"""
import logging
import re

MASK = "***"

# name=value in a query string or form body. Names are matched as whole words.
_PARAM_RE = re.compile(
    r"(?i)\b(token|access_token|auth_token|apikey|api_key|api-key|key|secret|password|passwd)=([^&\s'\"<>]+)"
)
# Authorization headers and bare bearer tokens.
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization)\s*[:=]\s*(?!bearer\b)[^\s,'\"]+")


def redact(text: str) -> str:
    """Return text with credential values replaced by ***."""
    if not text:
        return text
    text = _PARAM_RE.sub(lambda m: f"{m.group(1)}={MASK}", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}: {MASK}", text)
    return text


def safe_exc(exc: BaseException) -> str:
    """Exception text that is safe to log (requests errors echo the URL with its token)."""
    return redact(str(exc))


_installed = False


def install_logging_redaction() -> None:
    """Scrub every LogRecord at creation, so all handlers (buffer, stdout, files) see clean text."""
    global _installed
    if _installed:
        return
    _installed = True
    old_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        try:
            message = record.getMessage()
            clean = redact(message)
            if clean != message:
                record.msg = clean
                record.args = ()
            if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        except Exception:
            pass
        return record

    logging.setLogRecordFactory(factory)
