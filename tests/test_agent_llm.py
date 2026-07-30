"""Tests for agent/llm.py — the Pass 2/3 backend dispatcher (run_headless)."""

import types

import pytest

import agent.llm as llm


def _fake_config(**overrides):
    """A minimal Config-like object for backend-dispatch tests."""
    base = dict(
        llm_backend="claude",
        max_workers=2,
        api_base_url=None,
        api_model=None,
        api_key=None,
        api_timeout=300.0,
        api_clean_params={},
        api_enrich_params={},
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class TestRunHeadlessDispatch:
    """Test that run_headless routes to the right backend with the right model."""

    def test_claude_backend_uses_subprocess_path(self, monkeypatch):
        """backend=claude routes to _run_claude_headless with the pass's model."""
        monkeypatch.setattr(llm, "load_config", lambda: _fake_config())
        seen = {}

        def _fake_claude(model, sp, um):
            seen["model"] = model
            return '{"ok": 1}'

        monkeypatch.setattr(llm, "_run_claude_headless", _fake_claude)
        monkeypatch.setattr(llm, "_run_api_llm",
                            lambda *a, **k: pytest.fail("api path used"))

        assert llm.run_headless("clean", "sys", "usr") == '{"ok": 1}'
        assert seen["model"] == llm._PASS_CLAUDE_MODEL["clean"]
        assert llm.run_headless("enrich", "sys", "usr") == '{"ok": 1}'
        assert seen["model"] == llm._PASS_CLAUDE_MODEL["enrich"]

    def test_api_backend_uses_api_path(self, monkeypatch):
        """backend=api routes to _run_api_llm with the configured api model."""
        monkeypatch.setattr(llm, "load_config", lambda: _fake_config(
            llm_backend="api", api_model="gpt-oss:20b"))
        seen = {}

        def _fake_api(config, pass_name, model, sp, um):
            seen["model"] = model
            return '{"ok": 1}'

        monkeypatch.setattr(llm, "_run_api_llm", _fake_api)
        monkeypatch.setattr(llm, "_run_claude_headless",
                            lambda *a, **k: pytest.fail("claude path used"))

        assert llm.run_headless("clean", "sys", "usr") == '{"ok": 1}'
        assert seen["model"] == "gpt-oss:20b"
