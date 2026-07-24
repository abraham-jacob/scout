---
hide:
  - toc
---

<div class="st-hero" markdown>
![Scout](images/banner_light.svg#only-light){ .st-logo }
![Scout](images/banner_dark.svg#only-dark){ .st-logo }

# Know which jobs are worth your time. { .st-title }

<p class="st-sub">
Scout scrapes every posting behind your LinkedIn searches, then classifies,
summarizes, and scores each one against your résumé — locally.
</p>

<div class="st-cta" markdown>
[Get started :material-arrow-right:](getting-started.md){ .md-button .md-button--primary }
[:fontawesome-brands-github: View on GitHub](https://github.com/abraham-jacob/scout){ .md-button }
</div>
</div>

![Scout job list UI](images/scout_light.png#only-light){ .st-shot }
![Scout job list UI](images/scout_dark.png#only-dark){ .st-shot }

## How it works

<div class="st-flow" markdown>
<div class="st-flow-step" markdown>
<span class="st-flow-num">1</span>
**Scrape**
Drives real Chrome to pull every job behind each saved search.
</div>
<div class="st-flow-step" markdown>
<span class="st-flow-num">2</span>
**Clean & classify**
Strips boilerplate, sorts each role into your categories.
</div>
<div class="st-flow-step" markdown>
<span class="st-flow-num">3</span>
**Score**
Rates every job out of 100 against your résumé & criteria.
</div>
</div>

## What you get

<div class="grid cards" markdown>

-   :material-target: __A match score, 0–100__

    ---

    Every job scored against your résumé, profile, and dealbreakers.

    ![Match score](images/feature_job_match_score.png)

-   :material-tag-multiple: __Auto-tagging & summaries__

    ---

    A 2–4 sentence summary and tags on every card — no more scrolling boilerplate.

    ![Tagging](images/feature_tagging.png)

-   :material-filter-variant: __Sort & filter__

    ---

    By role, score, company, status. Find the good ones fast.

    ![Sort and filter](images/feature_sort_filter.png)

-   :material-shield-lock: __Runs on your machine__

    ---

    Your data stays local. Bring :simple-claude:{ .claude } Claude, or a
    fully local model via :simple-ollama: Ollama.

</div>

## Why I built this

I built Scout during my own job search. Every morning started with a stack of
LinkedIn alert emails, and every posting meant the same ritual: open it,
scroll past three paragraphs of EEO boilerplate, figure out if it's a real
match, check whether I'd already seen it last week under a different posting
ID. After a few weeks I realized I was doing the same mechanical
classification task hundreds of times — which is exactly the kind of task
you should hand to an agent. So I did.

## Explore the docs

- **[Configuration](getting-started.md)** — requirements, setup walkthrough, and the full `config.toml`/scoring-file reference
- **[Using the Web UI](web-ui.md)** — filtering, job cards, match scores, the application pipeline
- **[OpenAI-compatible Backend](openai-compatible-backend.md)** — run Passes 2–3 on Ollama or any other OpenAI-compatible server, local or remote
- **[Architecture](architecture.md)** — how the three-pass pipeline actually works
- **[Contributing](contributing.md)** — conventions for working on Scout itself
- **[FAQ / Troubleshooting](faq.md)**

---

Made with ❤️ by Jacob Abraham. If Scout helped you land your next role,
consider supporting the project ☕.

<script type="text/javascript" src="https://cdnjs.buymeacoffee.com/1.0.0/button.prod.min.js" data-name="bmc-button" data-slug="jacob.abraham" data-color="#FFDD00" data-emoji=""  data-font="Cookie" data-text="Buy me a coffee" data-outline-color="#000000" data-font-color="#000000" data-coffee-color="#ffffff" ></script>
<a href="https://ko-fi.com/jacobabraham" target="_blank"><img src="https://storage.ko-fi.com/cdn/kofi3.png?v=3" alt="Support me on Ko-fi" style="height: 36px;border: 0px;vertical-align: middle;" ></a>

[Source on GitHub](https://github.com/abraham-jacob/scout) ·
[MIT License](https://github.com/abraham-jacob/scout/blob/main/LICENSE) © 2026 Jacob Abraham
