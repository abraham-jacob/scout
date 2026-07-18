# FAQ / Troubleshooting

## Pass 1 hangs / never finishes

Check Chrome's download settings: *Settings → Downloads → "Ask where to save
each file before downloading"* must be **off**. Pass 1 hands scraped job data
off to the pipeline via a browser file download (see
[Architecture](architecture.md)); if Chrome pops
a save-location dialog, the agent can't dismiss it and the run stalls waiting
for a file that never lands.

## The pipeline refuses to start with a local-LLM error

Setup validation pings your `[llm.local] base_url` and checks that it's
serving the exact `[llm.local] model` id you configured, before any browser
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
(the Claude API, or a fully local model via Ollama, which never sends
anything over the network at all). The web UI has no authentication and
binds to localhost; don't expose it to a network.

## Does this violate LinkedIn's Terms of Service?

Scout automates *your own* browsing of *your own* job alerts, in *your own*
logged-in Chrome session — one page of results per alert email, no
crawling, no scale. That said, automated access may still conflict with
LinkedIn's Terms of Service; understand them and use your own judgment. This
project is not affiliated with LinkedIn.

## Where do I run the test suite / report a bug?

See [Contributing](contributing.md).
