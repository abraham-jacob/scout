# Getting Started

## Requirements

Scout is a personal, single-user tool. It expects:

| Requirement | Why |
|---|---|
| **Python 3.12** + [pipenv](https://pipenv.pypa.io/) | Runtime & dependency management |
| **Google Chrome** with the [Claude in Chrome](https://claude.com/chrome) extension | Pass 1 drives your real, logged-in browser |
| **[Claude Code](https://claude.com/claude-code)** (the `claude` CLI) | Pass 1 always runs on Claude; Passes 2–3 do too unless you point them at a local model |
| **A Gmail account** receiving LinkedIn job-alert emails, plus a Google Cloud OAuth client (`credentials.json`) with the Gmail API enabled | Scout reads the alert emails to find the job URLs |
| **A LinkedIn account** logged into Chrome | The scrape runs inside your own session |
| *(Optional)* An OpenAI-compatible local server ([Ollama](https://ollama.com/) etc.) | Run Passes 2–3 on a local model: free and private |

## 1. Clone and install

```bash
git clone https://github.com/abraham-jacob/scout.git && cd scout
pipenv install
```

## 2. Configure

Create `profiles/config.toml`. See the [Configuration reference](configuration.md)
for the complete field-by-field breakdown; a minimal config looks like this:

```toml
[[roles]]
name = "Manager"
definition = "Leads people. Titles like Engineering Manager, Senior EM, Director."
profile = "manager.md"          # optional per-role scoring profile in profiles/

[[roles]]
name = "IC"
definition = "Senior individual contributor. Titles like Staff/Principal Engineer."

[gmail]
label = "Job Alerts"            # the Gmail label your LinkedIn alerts land under

[filters]
exclude_companies = []          # dropped before any LLM call

[scoring]
fit_weight = 0.85               # must sum to 1 with criteria_weight
criteria_weight = 0.15
dealbreaker_cap = 30.0           # max score when a dealbreaker is present

[logging]
dir = "logs"

[llm]
backend = "claude"              # or "local" — see the Local LLM Backend page
max_workers = 4                 # Pass 2/3 parallelism
```

Then add your resume as `profiles/resume.md` (plus optional per-role profiles
and a `criteria.md` with hard requirements — see [Configuration](configuration.md)).
Everything in `profiles/` except its own README is git-ignored; your personal
data stays local.

## 3. Wire up Gmail

In [Google Cloud Console](https://console.cloud.google.com/), enable the
Gmail API and create an OAuth *Desktop app* client. Save the JSON as
`credentials.json` in the repo root. In Gmail, create a filter that applies
your chosen label (e.g. `Job Alerts`) to LinkedIn job-alert emails. The first
run opens a browser window for the OAuth consent flow.

## 4. Prepare Chrome

Install the Claude in Chrome extension, be logged into LinkedIn, and turn
**off** *Settings → Downloads → "Ask where to save each file before
downloading"*. Pass 1 hands off scraped data through a browser download — a
save dialog would stall the agent. See the [FAQ](faq.md) if you hit this.

## 5. Run

```bash
pipenv run uvicorn app.main:app        # web UI at http://127.0.0.1:8000
```

Click **▶ Run Scout**. Or run the pipeline directly from the terminal:

```bash
pipenv run python -m agent.runner                   # process unread alert emails
pipenv run python -m agent.runner --url <linkedin_search_url>   # scrape one URL, skip Gmail
```

From here: read [Using the Web UI](web-ui.md) to see what a run produces, or
[Architecture](architecture.md) for how the pipeline works under the hood.
