# Scout

**An AI agent that reads your LinkedIn job alerts, scrapes every posting behind them, and tells you which ones are actually worth your time.**

LinkedIn job search results are a firehose: dozens of postings a day, half of them reposts, mismatches, or roles you already applied to. Scout drinks from the firehose for you. It drives a real Chrome session to scrape **every** job behind each of your saved LinkedIn searches (including the ones LinkedIn never renders), cleans the boilerplate out of each description, classifies and scores every role against *your* resume and criteria, and files the survivors into a local database with a clean web UI — each job tagged, summarized, and scored out of 100.

Everything runs on your machine. Your resume, your criteria, and your job-search data never leave it — except as prompts to the LLM you choose (Claude API, or a fully local model via Ollama).

![Scout job list UI](images/scout_light.png)

## Why I built this

I built Scout during my own job search. Every morning started with a stack of LinkedIn alert emails, and every posting meant the same ritual: open it, scroll past three paragraphs of EEO boilerplate, figure out if it's a real match, check whether I'd already seen it last week under a different posting ID. After a few weeks I realized I was doing the same mechanical classification task hundreds of times — which is exactly the kind of task you should hand to an agent. So I did.

## Where to go next

- **[Configuration](getting-started.md)** — requirements, setup walkthrough, and the full `config.toml`/scoring-file reference
- **[Using the Web UI](web-ui.md)** — filtering, job cards, match scores, the application pipeline
- **[Local LLM Backend](local-llm.md)** — run Passes 2–3 on a free, fully local model
- **[Architecture](architecture.md)** — how the three-pass pipeline actually works
- **[Contributing](contributing.md)** — conventions for working on Scout itself
- **[FAQ / Troubleshooting](faq.md)**

## Project links

- [Source on GitHub](https://github.com/abraham-jacob/scout)
- [MIT License](https://github.com/abraham-jacob/scout/blob/main/LICENSE) © 2026 Jacob Abraham
