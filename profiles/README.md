# Profiles & user config

This directory holds your personal configuration and the artifacts the
enrichment pass scores each job against. Everything here except this README
is **git-ignored** — these files contain personal data and never leave your
machine.

## `config.toml` (**required**)

The user config: five required sections, plus one optional `[scrape]`:

```toml
[gmail]
label = "Daily LinkedIn Search"   # the label your job-alert emails live under

[filters]
exclude_companies = ["Capital One"]   # dropped before any LLM call; [] is fine

[scoring]
fit_weight = 0.85        # weights must sum to 1
criteria_weight = 0.15
dealbreaker_cap = 30.0   # score ceiling (0-100) when a dealbreaker is hit

[logging]
dir = "~/.local/state/scout/logs"   # daily app log + opt-in model-call log;
                                    # ~ expands, relative paths = project root

[scrape]                            # OPTIONAL — omit unless you've changed
download_dir = "~/Downloads"        # Chrome's download folder. Defaults to
                                    # ~/Downloads (works on Win/Mac/Linux).

[llm]                               # REQUIRED
backend = "claude"                  # "claude" or "local" (no default)
max_workers = 2                     # Pass 2/3 parallelism; tune to the backend
```

### `[llm]` — pick the backend and its parallelism (required)

`backend` says which model backend runs the two headless passes — description
cleaning (Pass 2) and enrichment/scoring (Pass 3). There's no default: you must
state `"claude"` or `"local"` explicitly, so the config always says which one is
in use. Pass 1 (the browser scrape) always runs on Claude — it drives the
browser and can't move to a local text model.

`max_workers` is the width of the Pass 2/3 worker pool (a positive integer).
Tune it to the active backend: a Claude run can go wide (it's bounded mainly by
prompt-cache-write dedup, default 2), while a local server is bounded by its own
VRAM/throughput — a 16GB box running a 20B model may only manage `max_workers = 1`.

Set `backend = "local"` to route both headless passes to a **local
OpenAI-compatible server** such as [Ollama](https://ollama.com), cutting API cost
to zero for them. It's all-or-nothing: both passes move together. Add a
`[llm.local]` subsection:

```toml
[llm]
backend = "local"
max_workers = 1                             # local box; keep it low

[llm.local]
base_url = "http://192.168.1.50:11434/v1"   # your server's OpenAI-compatible endpoint
model    = "scout-enrich:latest"             # EXACT id from the server's model list
# api_key = "ollama"    # optional; Ollama ignores it, other servers may need it
# timeout = 300         # optional, seconds (default 300) — local inference can be slow
```

`base_url` and `model` are required in this mode. `model` must be the **exact
id the server reports**, including any tag — Ollama lists models as
`name:tag` (e.g. `scout-enrich:latest`, from `ollama list`), so `scout-enrich`
alone won't match; other OpenAI-compatible servers (vLLM, LM Studio) report ids
with no tag at all. Whatever the server's model list shows, copy it verbatim.

At startup the pipeline probes the server and refuses to run if it's unreachable
**or** isn't serving that exact `model` id, so a wrong host, a stopped server, or
a mistyped/un-pulled model fails fast instead of mid-run — and the error prints
the ids the server currently serves so you can copy the right one.

#### Per-pass request parameters (optional)

Two optional sub-tables let you pass request parameters to the server per pass —
`[llm.local.clean]` for description cleaning (Pass 2) and `[llm.local.enrich]`
for enrichment/scoring (Pass 3). Each key/value is merged **verbatim** into that
pass's chat-completion JSON, so you can set anything the server accepts. The
motivating case is a reasoning model like GPT-OSS: give the mechanical cleaning
pass low effort and the scoring pass high effort.

```toml
[llm.local.clean]
temperature = 0
reasoning_effort = "low"      # cleaning is mechanical — don't burn thinking on it

[llm.local.enrich]
temperature = 0
reasoning_effort = "high"     # scoring is judgment — let it think
```

Both tables are optional, as is every key inside them. Omit them and the
pipeline sends only JSON-output mode — temperature and any reasoning knob fall
back to the **server/model default** (Scout no longer forces `temperature = 0`;
set it explicitly here if you want it). Values must be scalars
(string/number/boolean). The pipeline owns `model`, `messages`, and `stream`, so
those keys are rejected here. Parameter *values* aren't validated — an
unsupported one (a `reasoning_effort` a non-reasoning model doesn't understand,
say) is left for the server to reject.

### `[[roles]]`

Defines the role types Scout keeps. Each `[[roles]]` entry has a `name` (the
label stored in the DB and shown in the UI), a `definition` (classification
guidance for the enrichment model — what counts, example titles, explicit
exclusions), and an optional `profile` (a markdown file in this directory the
role is scored against). Jobs matching no configured role are classified
`Other` and dropped. Chip and filter colors are assigned automatically in the
order roles are listed.

The file must exist and define **at least one** role — with zero roles
there is nothing for Scout to keep, so the pipeline (and the web UI) refuse
to run. There are no built-in default roles.

```toml
[[roles]]
name = "Product Manager"
definition = """the core of the job is owning product strategy and \
execution... Examples: Product Manager, Senior/Group PM, Director of \
Product. Project/program management does not count."""
profile = "profile_pm.md"   # optional — omit to score on the resume alone
```

## Scoring files

`resume.md` is **required** — the pipeline refuses to start without it, and
every kept job is scored against it. Per-role profiles are optional refinements
on top: a role with one is scored against resume + profile, a role without one
against the resume alone. A profile that is referenced in `config.toml` but
missing on disk is a setup error and also stops the run.

| File | Contents |
|---|---|
| `resume.md` | **Required.** Your latest resume, converted to markdown / plain text. |
| `profile_<role>.md` | Optional, one per role (referenced from `config.toml`): what you are looking for in that kind of role — level, kind of work, technologies, scope. Jobs of a role with no profile are scored against the resume alone. |
| `criteria.md` | Optional. Preferences outside the resume: workplace, compensation, domains to seek/avoid, company stage. Drives the `criteria_weight` share of the final score (the rest is resume+profile fit). Without this file the score is 100% fit. |

Mark any criteria line as a hard veto by prefixing it with `**DEALBREAKER**:` —
jobs violating one are capped at a low score no matter how well they fit, and
the violated item is shown on the job card. Example:

```markdown
## Workplace
- Remote strongly preferred; Hybrid up to 2 days is fine
- **DEALBREAKER**: On-site 4+ days a week

## Domains to avoid
- Ad Tech (soft penalty)
- **DEALBREAKER**: Crypto / Web3
```

## After adding or editing files

- **New scrape runs** score automatically.
- **Existing jobs**: run `pipenv run python -m scripts.backfill_scores`
  (one Sonnet call per unscored job; updates only the scoring columns).
- **Changed your mind about weights or the cap?**
  `pipenv run python -m scripts.backfill_scores --recompute` rebuilds every
  final score from the stored subscores with zero LLM calls.
