"""
Thin router for the Pass 2/3 headless backend dispatch: picks the Claude CLI
path (agent.claude) or the OpenAI-compatible api path (agent.llm_api)
according to [llm] backend in the config.
"""

from app.config import load_config
from agent.claude import _PASS_CLAUDE_MODEL, _run_claude_headless
from agent.llm_api import _run_api_llm
from agent.llm_common import log_model_call


def run_headless(pass_name: str, system_prompt: str, user_message: str) -> str | None:
    """Run one headless structured call for Pass 2/3 on the configured backend.

    Dispatches to Claude (a `claude --print` subprocess) or an OpenAI-compatible
    endpoint (e.g. Ollama, local or remote) according to [llm] backend in the
    config. pass_name is "clean" or "enrich". Handles model-call logging and
    token/cost accounting internally and returns the raw model result text (the
    JSON blob the caller parses with _extract_json), or None on any failure so
    the caller can fall back gracefully. Pass 1 (the browser scrape) does not go
    through here — it always runs on Claude via agent.claude.run_claude.
    """
    config = load_config()
    if config.llm_backend == "api":
        model = config.api_model
        log_model_call(pass_name, model, system_prompt, user_message)
        return _run_api_llm(config, pass_name, model, system_prompt,
                            user_message)
    model = _PASS_CLAUDE_MODEL[pass_name]
    log_model_call(pass_name, model, system_prompt, user_message)
    return _run_claude_headless(model, system_prompt, user_message)
