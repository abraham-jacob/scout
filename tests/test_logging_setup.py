"""Tests for app/logging_setup.py and the runner's model-call logging."""

import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

import agent.runner as runner
import app.config as app_config
from app.logging_setup import (
    APP_LOG_NAME,
    MODEL_LOG_NAME,
    get_model_logger,
    setup_logging,
)
from tests.conftest import STANDARD_TEST_CONFIG


@pytest.fixture(autouse=True)
def clean_loggers():
    """Detach and close file handlers so each test configures from scratch.

    The loggers are process-global; without this, the first test's handler
    (pointing at its tmp_path) would leak into every later test.
    """
    yield
    for name in (APP_LOG_NAME, MODEL_LOG_NAME):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if isinstance(handler, TimedRotatingFileHandler):
                handler.close()
                logger.removeHandler(handler)


def _point_logs_at(tmp_path):
    """Rewrite the per-test config so [logging] dir is an absolute tmp path."""
    app_config.CONFIG_FILE.write_text(STANDARD_TEST_CONFIG.replace(
        'dir = "logs"', f'dir = "{tmp_path / "logs"}"'))
    return tmp_path / "logs"


class TestSetupLogging:
    """Test the daily application log."""

    def test_creates_dir_and_writes(self, tmp_path):
        """setup_logging creates the configured dir and log lines land in it."""
        log_dir = _point_logs_at(tmp_path)

        setup_logging().info("hello from the test")

        content = (log_dir / "scout.log").read_text()
        assert "hello from the test" in content
        assert "INFO" in content

    def test_tilde_expanded(self, tmp_path, monkeypatch):
        """A ~ in [logging] dir resolves under the user's home directory."""
        monkeypatch.setenv("HOME", str(tmp_path))
        app_config.CONFIG_FILE.write_text(STANDARD_TEST_CONFIG.replace(
            'dir = "logs"', 'dir = "~/state/logs"'))

        setup_logging().info("tilde test")

        assert "tilde test" in (tmp_path / "state" / "logs" / "scout.log").read_text()

    def test_idempotent(self, tmp_path):
        """A second call returns the same logger without stacking handlers."""
        _point_logs_at(tmp_path)

        first = setup_logging()
        second = setup_logging()

        assert first is second
        file_handlers = [h for h in first.handlers
                         if isinstance(h, TimedRotatingFileHandler)]
        assert len(file_handlers) == 1


class TestModelLogger:
    """Test the model-interaction log."""

    def test_writes_message_verbatim(self, tmp_path):
        """Messages land in model_calls.log with no formatter prefix."""
        log_dir = _point_logs_at(tmp_path)

        get_model_logger().info("a multi-line\nmessage block")

        content = (log_dir / "model_calls.log").read_text()
        assert content == "a multi-line\nmessage block\n"

    def test_separate_from_app_log(self, tmp_path):
        """Model-call lines don't leak into scout.log."""
        log_dir = _point_logs_at(tmp_path)

        setup_logging().info("app line")
        get_model_logger().info('{"call": "clean"}')

        assert "clean" not in (log_dir / "scout.log").read_text()
        assert "app line" not in (log_dir / "model_calls.log").read_text()


class TestRunnerLogModelCall:
    """Test agent.runner.log_model_call gating and payload."""

    def test_disabled_by_default(self, tmp_path, monkeypatch):
        """Without --log-model-calls nothing is written."""
        log_dir = _point_logs_at(tmp_path)
        monkeypatch.setattr(runner, "_log_model_calls", False)

        runner.log_model_call("enrich", "model-x", "system", "user")

        assert not (log_dir / "model_calls.log").exists()

    def test_enabled_writes_readable_block(self, tmp_path, monkeypatch):
        """When enabled, a labeled block with prompts verbatim lands."""
        log_dir = _point_logs_at(tmp_path)
        monkeypatch.setattr(runner, "_log_model_calls", True)

        runner.log_model_call("scrape", "model-x", "line one\nline two",
                              "the user message")

        content = (log_dir / "model_calls.log").read_text()
        assert "| scrape | model-x" in content
        assert "system prompt" in content
        assert "line one\nline two" in content  # newlines NOT escaped
        assert "user message" in content
        assert "the user message" in content

    def test_config_missing_logging_section_raises(self):
        """[logging] is a required config section."""
        app_config.CONFIG_FILE.write_text(STANDARD_TEST_CONFIG.replace(
            '[logging]\ndir = "logs"\n', ""))
        with pytest.raises(ValueError, match=r"\[logging\]"):
            app_config.load_config()

    def test_config_blank_logging_dir_raises(self):
        """A blank dir is as bad as a missing one."""
        app_config.CONFIG_FILE.write_text(STANDARD_TEST_CONFIG.replace(
            'dir = "logs"', 'dir = "  "'))
        with pytest.raises(ValueError, match=r"\[logging\]"):
            app_config.load_config()
