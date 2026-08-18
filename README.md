# App Integration Research Agent

Give it **one app name**. It researches that app's integration surface and returns
an evidence-backed, structured report:

| Dimension | What it answers |
|---|---|
| **Category + one-liner** | What the product is |
| **Auth method(s)** | OAuth2, API key, bearer/PAT, basic, JWT, HMAC, mTLS, none |
| **Self-serve vs gated** | Can a developer get working credentials alone, free or on trial — or does it need a paid plan, an admin, a partner program, or a sales call |
| **API surface** | REST / GraphQL / SOAP / gRPC / webhooks-only / SDK-only / none, how broad, versioned, publicly documented |
| **MCP** | Official, community, none — with the URL |
| **Buildability verdict** | `buildable_today`, `buildable_with_friction`, or `blocked` + the single main blocker |
| **Evidence** | Every claim carries a source URL and a verbatim quote |

Built with **LangGraph + LangChain**, **Composio** for all web reach, **Claude**
(tiered Opus 5 / Haiku 4.5), **FastAPI**, **Postgres**, and a **React** frontend.

---

## Quick start

```bash
cp .env.example .env    # then fill in ANTHROPIC_API_KEY and COMPOSIO_API_KEY
docker compose up -d
```

```bash
cd frontend && npm install && npm run dev    # http://localhost:5173
```

Postgres and the API come up under Compose; the frontend runs on Vite with `/api`
proxied, so there is no CORS setup in development.

### Running the backend without Docker

```bash
docker compose up -d postgres
cd backend
python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/uvicorn app.main:app --reload --port 8000
```

### CLI

```bash
cd backend
./.venv/bin/python -m app.cli "Zendesk"
./.venv/bin/python -m app.cli "Ramp" --json
./.venv/bin/python -m app.cli "Linear" --no-db --force-refresh
```

Prints the report, the verification result, and what the run cost in tokens.

---

## Prerequisites

**`ANTHROPIC_API_KEY`** — the agent runs on Claude.

**`COMPOSIO_API_KEY`** plus at least one connected search/scrape toolkit. The agent
does not hard-code tool slugs; on first use it asks Composio for the tools in each
toolkit named by `COMPOSIO_TOOLKITS` (default `COMPOSIO_SEARCH,EXA,FIRECRAWL,TAVILY`)
and keeps the ones that search, scrape, or extract. Toolkits that aren't connected
are skipped with a warning. If none resolve, the run fails with a message naming
the toolkits it tried — connect one in the Composio dashboard, or adjust the list.

---

## Architecture

![Architecture: app name enters lookup_cache; a cache hit skips to persist. A miss goes to resolve, then fans out to three parallel Haiku lanes — identity, auth & access, API & MCP. The lanes return claims and quotes only; Opus 5 synthesizes and verifies, re-running a single lane on gaps, before persisting to Postgres.](Architecture-Diagram.png)

### Why this shape

**Planning is deterministic Python, not an LLM call.** The six dimensions are fixed
by the problem, so decomposing into lanes needs no model: it costs zero tokens, is
identical every run, and cannot hallucinate a lane. An LLM planner is right when
the shape of the question is unknown; here it isn't.

**Lanes are the token control.** Each lane owns one narrow question, runs its own
bounded search loop, and returns a handful of structured claims. Raw page text
never leaves the lane. The orchestrator's context is therefore a function of how
many *claims* were made, not of how much the workers had to read to make them —
which is what makes it affordable to put the expensive model on synthesis.

**Search and extraction are one conversation.** The lane's output schema
(`LaneFindings`) is bound as just another tool alongside the Composio tools. The
lane ends when the model calls it. There is no second pass over the transcript, so
it is paid for once instead of twice.

**Verification repairs rather than re-runs.** When the critic finds an unsupported
claim, its complaint is mapped to the lane that could fix it (`access_tier` →
auth lane, `mcp_status` → API lane) and only that lane re-runs. The lane reducer is
keyed by lane name, so a re-run *replaces* its earlier result while untouched lanes
keep theirs.

### Token economics

Every lever is an env var in `.env.example`, so the cost profile is inspectable
without reading the graph.

| Lever | Effect |
|---|---|
| **Model tiering** | Haiku 4.5 does the gathering (high volume, shallow judgment); Opus 5 only synthesises and verifies (small inputs, hard judgment). Worker calls dominate volume, so this is where the cost is actually decided. |
| **Context isolation** | Lanes compress to structured claims; scraped text never reaches the orchestrator. |
| **Prompt caching** | The shared rubric is byte-identical on every call and sent as a cached system block, so it bills at ~0.1× after the first request. Everything run-specific goes *after* the breakpoint — putting it in the system prompt would invalidate the prefix every time. |
| **Report cache** | A repeat request inside `REPORT_CACHE_TTL_HOURS` costs zero model calls and zero network. |
| **Run-scoped URL dedupe** | Three lanes researching the same app hit the same docs page; it is fetched and paid for once. |
| **Source truncation** | Any single source is capped at `MAX_SOURCE_CHARS`. |
| **Search budget** | `MAX_TOOL_CALLS_PER_LANE`, with the schema force-called when it runs out, so a lane always returns something. |
| **Structured output everywhere** | No retry-on-parse loops and no "respond ONLY with JSON" prompt padding. |

Actual usage — calls, uncached input, cached reads, output, and dollar cost — is
recorded per run and shown in the UI and CLI.

### Accuracy

Accuracy was treated as the primary requirement, so it is enforced in three places
rather than trusted to the prompt:

1. **Evidence is mandatory.** Every lane claim carries a URL and a verbatim quote.
   The report can only cite URLs that appeared in the findings.
2. **Fabricated citations are caught in Python, not argued about.** The evidence
   pool records every URL that genuinely appeared in a tool result. Any cited URL
   outside that set is flagged deterministically (case- and trailing-punctuation
   insensitive), forces `human_review_needed`, and fails verification regardless
   of what the critic model thought.
3. **Unknowns are first-class.** Every enum has an `unknown` member, the report has
   an `unknowns` list, and confidence is recorded per dimension. A gated app
   reported as gated with evidence is a correct answer, not a failure.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/research` | `{app_name, force_refresh?, wait?}` → run id, or the full result when `wait: true` |
| `GET` | `/api/research/{run_id}/events` | SSE progress: node transitions, lane starts, tool calls, verification |
| `GET` | `/api/runs/{run_id}` | Stored run with report, verification, cost, and trace |
| `GET` | `/api/runs` | Recent runs with cost and verdict |
| `GET` | `/api/apps/{app_name}` | Latest stored report for an app, without triggering a run |
| `GET` | `/api/config` | Model tiering and budget settings (the UI reads this) |
| `GET` | `/api/health` | DB and checkpointer readiness |

Runs are checkpointed to Postgres via LangGraph's `AsyncPostgresSaver`, so a run
is resumable and its intermediate state is inspectable.

---

## Data model

- **`research_runs`** — one row per run: status, token usage, cost, tool stats,
  the resolved Composio toolkits, the verification result, and the node trace.
- **`app_reports`** — append-only report history, with `category` / `access_tier` /
  `verdict` / `mcp_status` denormalised for clustering across many apps.
- **`report_evidence`** — citations broken out so sources are queryable across apps
  rather than buried in a JSON blob.

A cache-hit run records a pointer to the report it served rather than copying it —
duplicating the row would reset its `created_at` and the TTL would never expire.

---

## Tests

```bash
cd backend && ./.venv/bin/python -m pytest
```

41 tests, no API keys and no network required. The graph depends on two seams — the
tool provider and the two functions that talk to Anthropic — so the whole pipeline
runs against fakes: fan-out, the verification repair loop and its budget, citation
integrity, source dedupe and truncation, cost accounting, and graceful degradation
when no toolkit is connected.

Three further tests exercise a real Postgres and **skip automatically** when none is
reachable:

```bash
docker compose up -d postgres && cd backend && ./.venv/bin/python -m pytest
```

---

## Batch runs

```bash
cd backend
./.venv/bin/python -m app.batch --apps apps.txt --concurrency 3 --out results.json
./.venv/bin/python -m app.batch Zendesk Stripe Linear --concurrency 2
```

One app name per line in the file, `#` for comments. The graph is per-app and
stateless between apps, so batch is a semaphore over `ResearchService.execute`
with the Composio provider resolved once for the whole run. A failing app is
recorded and the batch continues — one dead vendor site must not cost the other
ninety-nine. The summary reports apps succeeded, verification pass rate, total
cost, and wall clock.

---

## The case study

`case-study.html` at the repo root is the assignment's deliverable page. It is
generated from real runs and reports **6 apps, not 100** — the Anthropic account
ran out of credits mid-batch, and the page says so rather than filling the gap.

It records a first-pass accuracy of **18/36 field-level judgements (50%)** against
hand-verified ground truth, the four harness defects that caused it, and the fact
that only one app (Stripe, 5/6 → 6/6) completed the fixed pipeline before billing
stopped the run.

### What those defects were

The first pass was poor for reasons that had nothing to do with the model:

1. **Unreachable toolkits were bound as if live.** `client.tools.get()` returns
   schemas whether or not the toolkit is connected, so EXA/Firecrawl/Tavily tools
   were offered to every lane and 404'd on invocation. The provider now checks each
   toolkit's auth mode against the live connected accounts and skips what it cannot
   actually call, recording the reason in `describe()`.
2. **Failed tool calls were charged to the lane's search budget**, so a lane could
   spend all three attempts on 404s and return nothing. Only successful calls count
   now.
3. **The tool filter admitted every `COMPOSIO_SEARCH_*` vertical** — flights,
   hotels, shopping — because the fragment `SEARCH` matches them all. 36 tools
   became 7.
4. **One over-long prose field discarded the whole report.** `integration_notes`
   caps at 800 characters; `with_structured_output` turns the breach into a
   returned `parsing_error` that was silently dropped, losing every lane's work.
   Four of five runs in the second pass died this way. `structured_call` now logs
   the error and salvages deterministically: only `string_too_long` fields are
   trimmed and re-validated. `_coerce_findings` salvages lane findings one at a
   time for the same reason — a single 900-character quote used to discard all
   seven of Stripe's claims, including the one that found `mcp.stripe.com`.

### Still open

- **95 of the 100 apps.** Blocked on API credits, not on code.
- **A real scraping toolkit.** Only `COMPOSIO_SEARCH` was ever connected, and it
  needs no credentials. Connecting Firecrawl or Exa is the highest-value next step.
- **MCP detection**, the weakest dimension at 1/6 before the fixes. MCP servers
  live in changelogs and separate docs trees that a docs-focused search misses
  unless told to look.
