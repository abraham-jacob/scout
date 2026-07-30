"""Tests for agent/llm_api.py — the OpenAI-compatible api-backend calling layer."""

import json
import types

import httpx
import pytest

import agent.llm_api as llm_api
from agent.llm_common import _tokens, _tokens_lock


def _fake_config(**overrides):
    """A minimal Config-like object for api-backend tests."""
    base = dict(
        llm_backend="api",
        max_workers=2,
        api_base_url="http://box:11434/v1",
        api_model="m",
        api_key=None,
        api_timeout=5.0,
        api_clean_params={},
        api_enrich_params={},
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakeStreamResponse:
    """Minimal stand-in for the context-managed Response httpx.stream() yields."""

    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _sse_lines(content_pieces=(), usage=None, reasoning_pieces=()):
    """Build fake `data: ...` SSE lines matching Ollama's streaming shape.

    Reasoning pieces are emitted first (as delta.reasoning, empty delta.content
    — confirmed via a live curl test against Ollama not to be mixed into the
    real content), then content pieces (delta.content), then one final chunk
    with empty choices and the usage object (when stream_options.include_usage
    is set), then the [DONE] sentinel.
    """
    lines = []
    for r in reasoning_pieces:
        lines.append("data: " + json.dumps(
            {"choices": [{"delta": {"content": "", "reasoning": r}}]}))
    for c in content_pieces:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
    if usage is not None:
        lines.append("data: " + json.dumps({"choices": [], "usage": usage}))
    lines.append("data: [DONE]")
    return lines


class TestRunApiLlm:
    """Test _run_api_llm's request construction, streaming, and retries."""

    def test_posts_and_maps_usage(self, monkeypatch):
        """Streams from the server and maps OpenAI usage at zero cost."""
        config = _fake_config(api_base_url="http://box:11434/v1",
                              api_model="gpt-oss:20b", api_key="k", api_timeout=42.0)
        captured = {}

        def _fake_stream(method, url, json=None, headers=None, timeout=None):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _FakeStreamResponse(_sse_lines(
                content_pieces=['{"description_clean"', ': "x"}'],
                usage={"prompt_tokens": 11, "completion_tokens": 4},
                reasoning_pieces=["thinking…"],
            ))

        monkeypatch.setattr(llm_api.httpx, "stream", _fake_stream)

        with _tokens_lock:
            in0, out0, cost0 = _tokens["input"], _tokens["output"], _tokens["cost_usd"]

        result = llm_api._run_api_llm(config, "clean", "gpt-oss:20b", "sys", "usr")

        assert result == '{"description_clean": "x"}'
        assert captured["method"] == "POST"
        assert captured["url"] == "http://box:11434/v1/chat/completions"
        assert captured["json"]["model"] == "gpt-oss:20b"
        assert captured["json"]["stream"] is True
        assert captured["json"]["stream_options"] == {"include_usage": True}
        assert captured["json"]["messages"][0]["role"] == "system"
        assert captured["headers"]["Authorization"] == "Bearer k"
        assert captured["timeout"] == 42.0
        with _tokens_lock:
            assert _tokens["input"] == in0 + 11
            assert _tokens["output"] == out0 + 4
            assert _tokens["cost_usd"] == cost0  # this backend isn't metered by the pipeline

    def test_no_api_key_omits_auth_header(self, monkeypatch):
        """Without api_key, no Authorization header is sent."""
        config = _fake_config(api_key=None)
        captured = {}

        def _fake_stream(method, url, json=None, headers=None, timeout=None):
            captured["headers"] = headers
            return _FakeStreamResponse(_sse_lines(content_pieces=["{}"], usage={}))

        monkeypatch.setattr(llm_api.httpx, "stream", _fake_stream)
        llm_api._run_api_llm(config, "enrich", "m", "sys", "usr")
        assert "Authorization" not in captured["headers"]

    def test_http_error_returns_none(self, monkeypatch):
        """A network/HTTP failure exhausts all retries and returns None."""
        config = _fake_config()
        slept = []
        monkeypatch.setattr(llm_api.time, "sleep", lambda s: slept.append(s))

        def _boom(*a, **k):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(llm_api.httpx, "stream", _boom)
        assert llm_api._run_api_llm(config, "clean", "m", "sys", "usr") is None
        # Retried API_STREAM_RETRIES times, sleeping between attempts (not after the last).
        assert slept == [llm_api.API_STREAM_RETRY_DELAY_S] * (llm_api.API_STREAM_RETRIES - 1)

    def test_http_error_recovers_on_retry(self, monkeypatch):
        """A stream that fails once then succeeds is not treated as a failure."""
        config = _fake_config()
        monkeypatch.setattr(llm_api.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def _fake_stream(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("stalled")
            return _FakeStreamResponse(_sse_lines(content_pieces=["{}"], usage={}))

        monkeypatch.setattr(llm_api.httpx, "stream", _fake_stream)
        assert llm_api._run_api_llm(config, "clean", "m", "sys", "usr") == "{}"
        assert calls["n"] == 2

    def test_malformed_response_returns_none(self, monkeypatch):
        """A stream with no content chunks at all returns None, not a crash."""
        config = _fake_config()

        def _fake_stream(*a, **k):
            return _FakeStreamResponse(["data: [DONE]"])

        monkeypatch.setattr(llm_api.httpx, "stream", _fake_stream)
        assert llm_api._run_api_llm(config, "enrich", "m", "sys", "usr") is None

    def _capture_payload(self, monkeypatch, config, pass_name):
        """Run one api call under `config` and return the streamed JSON payload."""
        captured = {}

        def _fake_stream(method, url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _FakeStreamResponse(_sse_lines(content_pieces=["{}"], usage={}))

        monkeypatch.setattr(llm_api.httpx, "stream", _fake_stream)
        llm_api._run_api_llm(config, pass_name, config.api_model, "sys", "usr")
        return captured

    def test_per_pass_params_merged(self, monkeypatch):
        """[llm.api.<pass>] params are merged into the payload for that pass."""
        config = _fake_config(
            api_base_url="http://box/v1",
            api_clean_params={"temperature": 0.2, "reasoning_effort": "low"},
            api_enrich_params={"reasoning_effort": "high"})

        captured = self._capture_payload(monkeypatch, config, "clean")
        assert captured["json"]["temperature"] == 0.2
        assert captured["json"]["reasoning_effort"] == "low"

        captured = self._capture_payload(monkeypatch, config, "enrich")
        assert captured["json"]["reasoning_effort"] == "high"
        # enrich set no temperature, so none is sent — server default applies
        assert "temperature" not in captured["json"]

    def test_params_cannot_clobber_owned_fields(self, monkeypatch):
        """model/messages/stream/stream_options are re-asserted even if a param
        table sets them.

        config validation rejects those keys, but the merge order guards against
        them defensively too.
        """
        config = _fake_config(
            api_base_url="http://box/v1",
            api_clean_params={"model": "evil", "stream": False})

        captured = self._capture_payload(monkeypatch, config, "clean")
        assert captured["json"]["model"] == "m"
        assert captured["json"]["stream"] is True
