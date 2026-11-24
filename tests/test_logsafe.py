import logging

import pytest

from hey_claude_relay.config import Secrets
from hey_claude_relay.logsafe import QUIET_LOGGERS, RedactSecrets, configure_logging, secret_values


@pytest.fixture
def log():
    logger = logging.getLogger("hey_claude_relay.test")
    yield logger
    logger.filters.clear()


def test_a_secret_in_a_log_line_is_masked(caplog, log):
    log.addFilter(RedactSecrets(["sk-test-123456", "abc"]))
    log.warning("key=%s short=%s", "sk-test-123456", "abc")
    assert "sk-test-123456" not in caplog.text and "[redacted]" in caplog.text and "short=abc" in caplog.text


def test_a_secret_in_a_traceback_is_masked(caplog, log):
    log.addFilter(RedactSecrets(["hunter2-secret"]))
    try:
        raise ValueError("token hunter2-secret was rejected")
    except ValueError:
        log.exception("call failed")
    assert "hunter2-secret" not in caplog.text and "[redacted]" in caplog.text


def test_a_malformed_log_call_elsewhere_does_not_raise_through_the_filter(caplog, log):
    # One argument too few: logging would normally print "--- Logging error ---" and carry on.
    log.addFilter(RedactSecrets(["sk-test-123456"]))
    log.warning("%s %s token sk-test-123456", "only-one")
    assert "sk-test-123456" not in caplog.text and "[redacted]" in caplog.text


def test_a_message_that_is_not_a_string_is_still_masked(caplog, log):
    log.addFilter(RedactSecrets(["sk-test-123456"]))
    log.warning(ValueError("bad key sk-test-123456"))
    assert "sk-test-123456" not in caplog.text and "bad key [redacted]" in caplog.text


def test_secret_values_lists_everything_set():
    secrets = Secrets(anthropic_api_key="sk-ant-1", cf_client_secret="cf-secret-1", bearer={"X_TOKEN": "bearer-tok-1"})
    assert sorted(secret_values(secrets)) == ["bearer-tok-1", "cf-secret-1", "sk-ant-1"]


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    names = ("hey_claude_relay", *QUIET_LOGGERS)
    saved = (root.handlers[:], root.level, {n: logging.getLogger(n).level for n in names})
    yield
    root.handlers[:], root.level = saved[0], saved[1]
    for name, level in saved[2].items():
        logging.getLogger(name).setLevel(level)


def test_debug_applies_to_the_relay_only(restore_logging):
    configure_logging("debug", Secrets(anthropic_api_key="sk-ant-123456"))
    assert logging.getLogger("hey_claude_relay").level == logging.DEBUG
    for quiet in ("anthropic", "mcp", "httpx2", "httpcore2"):
        assert logging.getLogger(quiet).level == logging.WARNING
    root = logging.getLogger()
    assert root.level == logging.INFO
    assert all(any(isinstance(f, RedactSecrets) for f in h.filters) for h in root.handlers) and root.handlers


def test_an_unknown_level_falls_back_to_info(restore_logging):
    configure_logging("chatty", Secrets())
    assert logging.getLogger("hey_claude_relay").level == logging.INFO
