# FAQ / Troubleshooting

## The extension popup doesn't show my saved searches

The popup reads `profiles/config.toml` through the running web server, and
that config is cached for the server's lifetime. If you edited
`[[linkedin_searches]]` after starting the server, click **Reload** next to
Saved Searches in the popup — it re-reads the file without a restart. A
parse/validation error keeps the popup showing its last-known list rather
than clearing it; check the server's terminal output for the actual error.

## A run seems stuck partway through

Use the **Abort** button next to the Run Log in the extension popup — it
stops the current run regardless of which pass it's in. If a run halted
because LinkedIn returned a block/auth-loss signal, whatever was already
fetched is still ingested and the popup offers a **Resume** for the rest;
give LinkedIn some time before retrying the same search.

## "Scrape Current Page" fails with "Couldn't reach this tab: Could not establish connection. Receiving end does not exist."

The content script that talks to LinkedIn only injects into tabs
opened or navigated **after** the extension was loaded or reloaded — a
LinkedIn jobs tab that was already open from before doesn't have it, so the
popup can't reach it (the "Couldn't reach this tab" part is the popup's own
wrapper; the rest is Chrome's underlying error, which is the same message
you'd get for several unrelated causes). Refresh the LinkedIn tab (the
popup's enable-check only looks at the URL, not whether the content script
actually registered, so it can't catch this case) and try again. If it still
fails, reload the extension itself from `chrome://extensions` and refresh
the tab once more.

## Do I still need Claude Code?

Only for Passes 2–3 (cleaning and enrichment/scoring), and only if
`[llm] backend = "claude"` — the default. Pass 1 (the browser extension) is
plain JavaScript and never calls an LLM. Set `backend = "api"` to route
Passes 2–3 to an OpenAI-compatible server instead and skip Claude Code
entirely; see [OpenAI-compatible Backend](openai-compatible-backend.md).

## The pipeline refuses to start with an API-backend error

Setup validation pings your `[llm.api] base_url` and checks that it's
serving the exact `[llm.api] model` id you configured, before any browser
work starts — so this fails fast rather than mid-run. The error message
prints the model ids the server actually reports; copy one of those verbatim
into `model`, including the tag (e.g. `scout-enrich:latest`, not
`scout-enrich`). See [Configuration](getting-started.md) for the full field
reference.

## A job I expected got classified `Other` and dropped

Jobs that don't match any of your configured `[[roles]]` definitions are
classified `Other` and dropped rather than saved with a meaningless role.
Tighten or broaden the `definition` field for the role you expected the job
to match — it's the classification guidance handed to the enrichment model,
so specificity there (example titles, explicit exclusions) directly controls
what gets kept. See [`[[roles]]`](getting-started.md) in the config reference.

## Is my data private?

Yes, by design. Scout is a single-user, local-only app: your resume,
criteria, and scraped job data live in a local DuckDB file and never leave
your machine — except as prompts to whichever LLM backend you've configured
(the :simple-claude:{ .claude } Claude API, or a fully local model via :simple-ollama: Ollama, which never sends
anything over the network at all). The web UI has no authentication and
binds to localhost; don't expose it to a network.

## :fontawesome-brands-linkedin:{ .linkedin } Does this violate LinkedIn's Terms of Service?

Scout automates *your own* browsing of *your own* saved searches, in *your
own* logged-in Chrome session — one page of results per configured search,
no crawling, no scale. That said, automated access may still conflict with
LinkedIn's Terms of Service; understand them and use your own judgment. This
project is not affiliated with LinkedIn.

## Where do I run the test suite / report a bug?

See [Contributing](contributing.md).
