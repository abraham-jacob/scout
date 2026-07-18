# Local LLM Backend

Passes 2 and 3 — the headless text-in/JSON-out passes — can run on any
OpenAI-compatible server instead of the Claude API. Pass 1 always runs on
Claude, because it's an agentic browser task a local text model can't do.

```toml
[llm]
backend = "local"
max_workers = 1                 # tune to your GPU; a 16GB box may want 1

[llm.local]
base_url = "http://localhost:11434/v1"
model    = "gpt-oss:20b"        # must match the server's model id exactly
timeout  = 45                   # per-call seconds; stalls fail fast and retry

[llm.local.clean]               # optional per-pass request params,
reasoning_effort = "low"        # merged verbatim into the API call

[llm.local.enrich]
reasoning_effort = "medium"
```

See the [`[llm.local]` reference](getting-started.md) for the full field
reference, including the required vs. optional keys and what happens if
`model` doesn't match what the server reports.

## Built for imperfect hardware

The local path assumes you're running on a box that isn't a dedicated
inference server:

- **Warm-up.** Scout fires a warm-up request at run start so the model loads
  *before* the timed passes begin, with its own generous timeout and
  retries — so a cold-start model load doesn't eat into (or fail) the first
  real call.
- **Tight per-call timeouts.** Each Pass 2/3 call has a short timeout
  (`[llm.local] timeout`), so a stalled generation fails fast instead of
  hanging the run.
- **One retry pass.** Every failed call gets one parallel retry pass before
  falling back gracefully — a job that still fails cleaning proceeds with
  its raw description rather than being dropped from the run.
- **Fail-fast setup validation.** Before any browser work starts, setup
  validation pings the server and verifies the model id, so a wrong host, a
  stopped server, or a mistyped/un-pulled model surfaces immediately with
  the list of model ids the server actually serves.

## Why route only Passes 2–3

Pass 1 drives a real, logged-in Chrome session through the Claude in Chrome
extension — that's an agentic capability, not a text-completion task, so it
stays on Claude regardless of backend. Passes 2 and 3 are plain
text-in/JSON-out calls (clean a description, classify and score a job),
which is exactly the shape a local OpenAI-compatible server handles well.
Routing is all-or-nothing across those two passes — see
[Architecture](architecture.md) for why the pipeline is structured this way.

## Cost

With the Claude backend, a run costs what the models cost: Haiku for the
scrape and clean passes, Sonnet only for enrichment, prompt caching on, and
the exact token usage and dollar cost printed at the end of every run. With
the local backend, Passes 2–3 are free — Pass 1's Haiku scrape is the only
API spend either way.
