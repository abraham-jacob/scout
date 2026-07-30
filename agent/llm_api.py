"""
Everything that talks to the configured OpenAI-compatible endpoint (e.g. an
Ollama server, local or remote): the headless Pass 2/3 api-backend call
(_run_api_llm), plus its setup/connectivity checks and warm-up.
"""

import json
import sys
import time

import httpx

from agent.llm_common import SetupError, _add_usage, emit_log, log_model_call

# A non-streaming request gives Ollama nothing to write until generation is
# fully done, so a client-side timeout goes completely unnoticed server-side
# — the server keeps computing a response nobody will read, and the next
# request queues up behind it, cascading timeouts across the whole batch
# (observed in practice). Streaming means the server is writing bytes
# continuously, so a dead client is noticed on its next write instead of
# after the full (possibly abandoned) generation completes. It has a second
# benefit: httpx's read timeout applies per-chunk on a streamed response, not
# once for the whole reply, so a slow-but-progressing generation no longer
# trips a false-positive timeout the way one all-or-nothing deadline did —
# only a genuine stall (no new chunk within config.api_timeout) does.
# API_STREAM_RETRIES/API_STREAM_RETRY_DELAY_S retry a stalled/dropped
# stream a few times, pausing between attempts so an already-abandoned
# generation has a chance to actually finish draining server-side before the
# next attempt piles on top of it.
API_STREAM_RETRIES = 3
API_STREAM_RETRY_DELAY_S = 10


def _api_endpoint(config, path: str) -> tuple[str, dict]:
    """Build the request URL and auth headers for one OpenAI-compatible API call.

    ``path`` is appended to config.api_base_url (e.g. "/chat/completions",
    "/models"); an Authorization header is added only when [llm.api] api_key
    is set, matching how every OpenAI-compatible server (including
    unauthenticated local ones like Ollama) expects it.
    """
    url = config.api_base_url.rstrip("/") + path
    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return url, headers


def _build_api_payload(config, pass_name: str, model: str, system_prompt: str,
                       user_message: str) -> dict:
    """Build the chat-completion JSON payload for one clean/enrich api call.

    Temperature is NOT forced — the server/model default applies unless the
    per-pass param table sets one. That optional table ([llm.api.<pass_name>],
    e.g. temperature or GPT-OSS's reasoning_effort) is merged over the
    JSON-mode baseline — so a user can raise the effort for enrich and drop it
    for clean — but the model/messages/stream/stream_options fields the
    pipeline owns are re-asserted afterward so a stray config key can't
    clobber them.
    """
    pass_params = (config.api_clean_params if pass_name == "clean"
                   else config.api_enrich_params)
    return {
        "response_format": {"type": "json_object"},
        **pass_params,
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def _run_api_llm(config, pass_name: str, model: str, system_prompt: str,
                 user_message: str) -> str | None:
    """POST one streamed chat-completion to the configured OpenAI-compatible endpoint.

    Talks to config.api_base_url (e.g. an Ollama server's /v1 endpoint),
    asking for JSON output.

    Streams the response (see API_STREAM_RETRIES above for why) and
    reassembles the answer from each chunk's delta.content. Reasoning/thinking
    tokens (confirmed via a live test against Ollama) arrive as a separate
    delta.reasoning field and are never mixed into delta.content, so they're
    simply skipped rather than needing to be stripped out of the final text.
    stream_options.include_usage=true (also confirmed supported) makes the
    server send one final chunk with empty choices and a populated usage
    field just before [DONE]; that's mapped into the token tracker at zero
    cost (this backend isn't metered by the pipeline).

    Retries up to API_STREAM_RETRIES times, API_STREAM_RETRY_DELAY_S apart,
    on a connection error or a stream stall — this composes with
    _retry_failures' own single batch-level retry pass, so a call can
    exhaust its retries here and still get one more shot there. Returns the
    assistant message text, or None if every attempt fails so the caller
    falls back gracefully. _extract_json still tolerates stray prose if the
    server ignores the JSON-mode request.

    Every failure (connection error, stream stall, or a stream that ends
    with reasoning chunks but no content — the model reasoning itself out of
    budget without ever producing an answer) is also emit_log'd, not just
    printed to stderr — this subprocess's stderr is piped into an in-memory
    buffer by app/main.py and only ever surfaced (truncated) if the whole
    run fails, so a per-job failure that falls back gracefully would
    otherwise be invisible anywhere the user can see it. The event log's
    "log" scope isn't tied to a specific search's group (see emit_log), so
    no index needs threading through here.
    """
    url, headers = _api_endpoint(config, "/chat/completions")
    payload = _build_api_payload(config, pass_name, model, system_prompt, user_message)

    for attempt in range(1, API_STREAM_RETRIES + 1):
        attempt_t0 = time.monotonic()
        reasoning_chunks = 0
        try:
            content_parts: list[str] = []
            usage: dict = {}
            with httpx.stream("POST", url, json=payload, headers=headers,
                              timeout=config.api_timeout) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[len("data: "):]
                    if chunk == "[DONE]":
                        break
                    event = json.loads(chunk)
                    choices = event.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                        elif delta.get("reasoning"):
                            reasoning_chunks += 1
                    elif "usage" in event:
                        usage = event["usage"] or {}
            content = "".join(content_parts)
            if not content:
                attempt_elapsed = time.monotonic() - attempt_t0
                raise ValueError(
                    f"stream ended with no content after {attempt_elapsed:.0f}s "
                    f"({reasoning_chunks} reasoning chunk(s), 0 content chunks)"
                )
        except httpx.HTTPError as exc:
            attempt_elapsed = time.monotonic() - attempt_t0
            msg = (f"api LLM call failed (attempt {attempt}/{API_STREAM_RETRIES}, "
                   f"{attempt_elapsed:.0f}s, {reasoning_chunks} reasoning chunk(s) "
                   f"before failure): {exc}")
            print(f"  {msg}", file=sys.stderr)
            emit_log(msg, level="warn")
            if attempt < API_STREAM_RETRIES:
                time.sleep(API_STREAM_RETRY_DELAY_S)
                continue
            return None
        except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError) as exc:
            msg = f"api LLM returned an unexpected response (attempt {attempt}): {exc}"
            print(f"  {msg}", file=sys.stderr)
            emit_log(msg, level="warn")
            return None

        _add_usage(
            {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
            0.0,
        )
        return content
    return None


def _verify_api_llm(config) -> None:
    """Verify the configured API endpoint is reachable and serving the model.

    Probes the OpenAI-compatible /models endpoint with a short timeout and
    raises SetupError if the endpoint can't be reached (wrong host / down), if the
    response isn't an OpenAI-compatible model list, or if the list doesn't
    include [llm.api] model — so a misconfigured backend fails at startup,
    before Pass 1, instead of failing every clean/enrich call mid-run. Only
    called when the [llm] backend is "api".
    """
    url, headers = _api_endpoint(config, "/models")
    try:
        resp = httpx.get(url, headers=headers, timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise SetupError(
            f"Setup error: API endpoint at {config.api_base_url} is "
            f"unreachable ({exc}). Is it running and reachable from this "
            "machine? Check [llm.api] base_url in profiles/config.toml, or "
            'set [llm] backend = "claude" to use the Claude API instead.'
        )
    try:
        data = resp.json()
        available = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    except (ValueError, AttributeError, TypeError) as exc:
        raise SetupError(
            f"Setup error: API endpoint at {config.api_base_url} returned "
            f"an unexpected /models response ({exc}). Is base_url pointing at an "
            "OpenAI-compatible endpoint (usually one ending in /v1)?"
        )
    if config.api_model not in available:
        listed = ", ".join(sorted(available)) or "none"
        raise SetupError(
            f"Setup error: API endpoint at {config.api_base_url} does not "
            f"serve a model with the exact id {config.api_model!r} (it serves: "
            f"{listed}). [llm.api] model must match one of those ids exactly, "
            'including any tag — e.g. "scout-enrich:latest", not "scout-enrich". '
            "Copy the id from your server's model list (for Ollama, `ollama "
            f"list`), or pull it if it's missing (e.g. `ollama pull "
            f"{config.api_model}`)."
        )


# The run-start warm-up absorbs the one-time cold model load. It gets its own
# timeout and retry budget, independent of the (deliberately tight) per-call
# [llm.api] timeout: WARMUP_TIMEOUT_S per attempt, WARMUP_ATTEMPTS attempts.
# Loading a model into memory has been observed at ~1 min, so a ~1 min per-attempt
# cap plus a couple of retries recovers a server that crashed on the first
# request — without making a wedged server hang the run for many minutes (the
# earlier 5-min-per-attempt cap did exactly that).
WARMUP_TIMEOUT_S = 60
WARMUP_ATTEMPTS = 3


def _warm_api_llm(config) -> None:
    """Fire one tiny generation so the model loads before the timed passes.

    The setup check (_verify_api_llm) only lists /models — it runs no
    inference, so the first real clean call is otherwise where the model loads
    into VRAM and warms its compute graph. That one-time cost can be minutes and
    can even exceed the per-call timeout, making the first job time out and fall
    back to its raw description (a silent quality loss). Sending a throwaway
    max_tokens=1 completion here moves that cost to run start — before Pass 1 —
    and retries it (WARMUP_ATTEMPTS attempts, WARMUP_TIMEOUT_S each), so a
    first-request server hiccup (an observed failure mode: the first request
    stalls or crashes the server) is absorbed here instead of costing a real
    job. Failures are non-fatal: the real clean/enrich calls still retry and
    fall back, so a warm-up problem never aborts the run. Only called on the
    api backend.
    """
    url, headers = _api_endpoint(config, "/chat/completions")
    payload = {
        "model": config.api_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    emit_log("Warming model…", level="head")
    t0 = time.monotonic()
    for attempt in range(1, WARMUP_ATTEMPTS + 1):
        try:
            resp = httpx.post(url, json=payload, headers=headers,
                              timeout=WARMUP_TIMEOUT_S)
            resp.raise_for_status()
            resp.json()
            emit_log(f"Model ready ({time.monotonic() - t0:.0f}s)",
                     level="good")
            return
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  model warm-up attempt {attempt} failed: {exc}",
                  file=sys.stderr)
    emit_log("Model warm-up failed — continuing (calls will retry)",
             level="warn")
