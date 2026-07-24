# OpenAI-compatible Backend

Passes 2 and 3 — the headless text-in/JSON-out passes — can run on any
**OpenAI-compatible** server instead of the :simple-claude:{ .claude } Claude
API. That's broader than "local": :simple-ollama: [Ollama](https://ollama.com)
running on your own box is the common case (free, fully private), but the
same config also works with a **remote** OpenAI-compatible API — e.g.
[Kimi](https://www.moonshot.ai/) — if you'd rather not run a server yourself.
Pass 1 always runs on Claude, because it's an agentic browser task a text
model can't do.

<div class="grid cards" markdown>

-   :simple-claude:{ .claude } __Claude backend__ *(default)*

    ---

    Best-in-class quality, zero setup. Passes 2–3 hit the Claude API. You pay
    per token (Haiku for clean, Sonnet for enrich).

-   :material-server-network: __OpenAI-compatible backend__

    ---

    Point Passes 2–3 at any OpenAI-compatible endpoint — a local server like
    Ollama (free, private) or a remote one like Kimi (paid, not Claude).

</div>

| | :simple-claude:{ .claude } Claude | :material-server-network: OpenAI-compatible |
|---|---|---|
| **Cost (Passes 2–3)** | Per-token | Free if self-hosted; per-token if a paid remote API |
| **Privacy** | Sent to Anthropic | Stays on your machine if self-hosted; sent to that provider if remote |
| **Quality** | Best-in-class | Depends on the model you point at |
| **Setup** | None | Stand up + tune a server, or just point at a remote endpoint |
| **Pass 1 scrape** | Claude (always) | Claude (always) |

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

`backend = "local"` and the `[llm.local]` table are the config keys regardless
of whether the endpoint is actually local — they just mean "not Claude." Swap
`base_url`/`model` for a remote provider's OpenAI-compatible endpoint and the
same config works unchanged. See the [`[llm.local]` reference](getting-started.md)
for the full field reference, including the required vs. optional keys and
what happens if `model` doesn't match what the server reports.

## Built for imperfect hardware

This path assumes the server behind `base_url` isn't a dedicated inference
box — whether that's a home GPU or a remote API having a bad day:

- **Warm-up.** Scout fires a warm-up request at run start so a self-hosted
  model loads *before* the timed passes begin, with its own generous timeout
  and retries — so a cold-start model load doesn't eat into (or fail) the
  first real call.
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
which is exactly the shape any OpenAI-compatible server handles well.
Routing is all-or-nothing across those two passes — see
[Architecture](architecture.md) for why the pipeline is structured this way.

## Cost

With the Claude backend, a run costs what the models cost: Haiku for the
scrape and clean passes, Sonnet only for enrichment, prompt caching on, and
the exact token usage and dollar cost printed at the end of every run. With a
self-hosted OpenAI-compatible backend (e.g. Ollama), Passes 2–3 are free —
Pass 1's Haiku scrape is the only API spend either way. Point at a paid
remote OpenAI-compatible API instead, and Passes 2–3 cost whatever that
provider charges.
