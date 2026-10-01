"""
Credentials must never reach the log buffer or stdout.

requests errors echo the full URL, including query-string credentials, and several call sites
log those exceptions. redact.py scrubs both the call sites and every LogRecord.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redact  # noqa: E402

SECRET = "s3cr3t-value-1234567890"


@pytest.mark.parametrize("text", [
    f"GET https://api.example.test/torrents/requestdl?token={SECRET}&torrent_id=1&file_id=2",
    f"https://mdblist.example.test/lists/user?apikey={SECRET}",
    f"https://x.example.test/a?api_key={SECRET}&b=1",
    f"https://x.example.test/a?access_token={SECRET}",
    f"Authorization: Bearer {SECRET}",
    f"headers={{'Authorization': 'Bearer {SECRET}'}}",
    f"password={SECRET}",
])
def test_redact_masks_secret(text):
    assert SECRET not in redact.redact(text)


def test_redact_keeps_other_params_and_shape():
    text = f"https://api.example.test/torrents/requestdl?token={SECRET}&torrent_id=1&file_id=2"
    out = redact.redact(text)
    assert out == "https://api.example.test/torrents/requestdl?token=***&torrent_id=1&file_id=2"


def test_redact_leaves_plain_text_alone():
    text = "requestdl failed torrent=12 file=3: 429 Too Many Requests"
    assert redact.redact(text) == text
    assert redact.redact("") == ""


def test_whole_word_names_only():
    # "monkey=" must not be treated as "key="
    assert redact.redact("monkey=banana") == "monkey=banana"


def test_safe_exc_masks_requests_style_error():
    exc = RuntimeError(f"429 Client Error: Too Many Requests for url: https://x.example.test/r?token={SECRET}&a=1")
    out = redact.safe_exc(exc)
    assert SECRET not in out
    assert "429 Client Error" in out


@pytest.fixture
def captured():
    redact.install_logging_redaction()
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = _H()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("redact-test")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def test_log_record_message_is_redacted(captured):
    logging.getLogger("redact-test").warning("call failed: %s", f"https://x.example.test/r?token={SECRET}")
    assert len(captured) == 1
    assert SECRET not in captured[0]
    assert "token=***" in captured[0]


def test_log_record_traceback_is_redacted(captured):
    try:
        raise RuntimeError(f"boom url=https://x.example.test/r?apikey={SECRET}")
    except RuntimeError:
        logging.getLogger("redact-test").exception("failed")
    assert len(captured) == 1
    assert SECRET not in captured[0]


def test_install_is_idempotent():
    redact.install_logging_redaction()
    factory = logging.getLogRecordFactory()
    redact.install_logging_redaction()
    assert logging.getLogRecordFactory() is factory


@pytest.fixture
def plain_record_factory():
    """Run with the stock LogRecord factory, so only code under test does the redacting."""
    saved_factory = logging.getLogRecordFactory()
    saved_flag = redact._installed
    logging.setLogRecordFactory(logging.LogRecord)
    redact._installed = False
    yield
    logging.setLogRecordFactory(saved_factory)
    redact._installed = saved_flag


def test_log_buffer_install_scrubs_buffered_lines(plain_record_factory):
    import log_buffer
    log_buffer.install()
    try:
        logging.getLogger("redact-test-buffer").warning("x token=%s", SECRET)
        lines = log_buffer.get_lines(50)
    finally:
        logging.getLogger().removeHandler(log_buffer._handler)
    assert any("token=***" in line for line in lines)
    assert not any(SECRET in line for line in lines)


def test_mdblist_call_site_does_not_log_the_api_key(plain_record_factory, caplog):
    from unittest.mock import MagicMock
    mocked = ("db", "settings")
    prior = {m: sys.modules.get(m) for m in mocked}
    for m in mocked:
        sys.modules[m] = MagicMock()
    try:
        import mdblist
    finally:
        for m in mocked:
            if prior[m] is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = prior[m]

    def boom(*a, **kw):
        raise mdblist.requests.exceptions.HTTPError(
            f"500 Server Error: Internal Server Error for url: https://x.example.test/lists/user?apikey={SECRET}"
        )

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(mdblist.requests, "get", boom)
    try:
        with caplog.at_level(logging.WARNING):
            mdblist.get_user_lists(SECRET)
    finally:
        mp.undo()
    assert "MDBList get_user_lists failed" in caplog.text
    assert SECRET not in caplog.text
