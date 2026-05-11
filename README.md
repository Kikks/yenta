# PR Review Agent

A LangGraph-powered agent that reviews a GitHub pull request end-to-end: reads the diff, reasons about it with Claude, and either **auto-approves** it or **escalates** to specific human reviewers with file/line-cited comments explaining what to look at.

This was built for the [Numeo AI Product Engineering Challenge](https://github.com/numeo-ai/numeo-ai-product-engineering-challenge) inside the 6-hour cap.

---

## Quick start

```bash
git clone <this repo>
cd numeoai-test
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in your tokens
python main.py https://github.com/<org>/<repo>/pull/<n> --mode conservative
```

That's it. The agent talks to GitHub for real — no stdout simulation.

### CLI

```bash
python main.py <PR_URL> --mode {conservative|aggressive} [--dry-run]
```

- `PR_URL` — full URL, e.g. `https://github.com/octocat/Hello-World/pull/42`
- `--mode conservative` — escalates eagerly (threshold 25), `REQUEST_CHANGES` event
- `--mode aggressive` — auto-approves more readily (threshold 60), softer `COMMENT` event
- `--dry-run` — runs the full pipeline (fetch + LLM + decision + reviewer pick) **but does not post to GitHub**. Prints the exact payloads it would have posted. Use this the first time you point it at a new PR.

### Required env (see `.env.example`)

| Var | Why |
|---|---|
| `GITHUB_TOKEN` | PAT with `repo` + `read:org`. The agent posts as this account. |
| `ANTHROPIC_API_KEY` | Claude API key |
| `ANTHROPIC_MODEL` | Default `claude-sonnet-4-5`; override per env |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | Optional but recommended. If absent, the agent runs without traces. |
| `MAX_TOKENS_PER_FILE_CHUNK` | Default `6000`. Files larger than this are hunk-split. |
| `MAX_LLM_CALLS_PER_RUN` | Default `80`. Hard cap to bound cost on monorepo PRs. |

---

## Architecture

```mermaid
flowchart TD
    A[CLI<br/>main.py] --> B[fetch<br/>PR meta, files,<br/>CODEOWNERS, blame]
    B --> C[chunk<br/>per-file -> per-hunk if<br/>over token budget]
    C --> D[analyze<br/>one LLM call / chunk<br/>structured JSON findings]
    D --> E[aggregate<br/>deterministic risk score<br/>0-100+]
    E --> F{decide<br/>risk &lt; threshold[mode]?}
    F -->|yes| G[approve<br/>summary + APPROVE]
    F -->|no| H[escalate<br/>pick reviewers,<br/>line comments,<br/>per-reviewer @-mention]
    G --> I[GitHub]
    H --> I

    style D fill:#fffbe6
    style E fill:#fffbe6
    style F fill:#fdf2f8
```

Every LLM call (analyze, summary) is wrapped with Langfuse `@observe(as_type="generation")` and emits **prompt, model, output, input/output tokens, latency, and per-call metadata** (file_path, hunk_index, pr_url, mode). The whole run is one parent trace named `pr-review/<owner>/<repo>#<n>`.

### Why LangGraph (and why not "just an agent loop")

The whole flow is a fixed DAG with one branch point. A LangGraph `StateGraph` is the right level of abstraction:

- **Auditable** — each node has one job; the graph diagram = the runtime behaviour
- **Testable** — node functions take a `GraphState` and return a state delta. Unit-testable in isolation.
- **Defensible** — in interview I can point at any node and explain why it's there

A free-form ReAct loop would have been cute but harder to defend, harder to test, and worse for the observability story.

### Why LLM does perception, not decision

The model produces structured findings (severity, category, file, line, rationale). A **deterministic function** (`aggregate.py`) turns those into a risk score. A **two-line decision** (`decide.py`) compares the score to a mode-specific threshold.

This split means:

- The agent's *behaviour* is reproducible across runs given the same findings
- Mode tuning is one number per mode — interview-defensible at a glance
- The model is never asked to be a judge — which is the failure mode for most LLM agents

---

## Design decisions (the spec is deliberately ambiguous — these are mine)

| Question the spec leaves open | My decision | Why |
|---|---|---|
| What defines "risk"? | Weighted sum: severity-weighted findings + sensitive-path hits (`auth/`, `migrations/`, `.env`, workflows, etc.) + PR-size curve + fork bonus + truncation bonus. See `aggregate.py`. | Auditable. The LLM perceives; the code decides. |
| Mode difference, *concretely*? | Two knobs: **escalate threshold** (conservative 25, aggressive 60) and **review event** (REQUEST_CHANGES vs COMMENT). Aggressive also drops `low`-severity findings from comments. | Two-knob design keeps the modes meaningfully different without sprawl. |
| How are reviewers picked? | CODEOWNERS last-match-wins per file → blame fallback (recency-weighted vote across changed paths) → drop PR author → cap 3. | Mirrors what real teams do. Degrades gracefully. |
| File-level vs line-level comments? | Both. Findings with a line number become GitHub line comments; the summary lives as the review body; per-reviewer @-mentions go as issue comments. | What a thoughtful human reviewer does. |
| Huge PRs (5K/5K)? | Per-file fan-out → hunk-split (via `unidiff`) when one file overruns `MAX_TOKENS_PER_FILE_CHUNK` → hard cap on total LLM calls. If we hit the cap, `state.truncated = True` and the final review honestly says what wasn't reviewed. | Structurally scales; never silently drops. |
| Fork PRs? | Add fork score bonus (+10) and an explicit safety floor: any fork PR with findings escalates regardless of mode. | Untrusted contributor — Numeo's culture cares about this. |
| Can the agent approve its own PR? | No. We detect when the agent's token equals the PR author and downgrade APPROVE → COMMENT. GitHub returns 422 otherwise. | Real-world failure mode; cheap to handle correctly. |
| Re-runs against the same PR? | Each run posts a new review. No dedupe in v1. | Documented limitation. Dedupe lives in "future work". |
| Hard escalations? | Any `critical` severity finding OR fork PR with any findings → always escalate, regardless of score. | Safety floor that overrides mode tuning. |

---

## Observability — Langfuse

The spec calls this out twice ("This matters"). For every LLM call you can see, in Langfuse:

- **input** — the full system + user prompt
- **output** — the raw text the model returned
- **model** — the exact model name used
- **usage** — input tokens, output tokens, total
- **latency** — captured per call
- **metadata** — `node`, `file_path`, `hunk_index`, `pr_url`, `mode`
- **trace tags** — `mode:conservative`, `repo:<owner>/<repo>` for filtering

Trace hierarchy:

```
pr-review/<owner>/<repo>#<n>   ← root trace, tagged by mode
├─ node.fetch                  ← span
├─ node.chunk                  ← span
├─ node.analyze                ← span
│  ├─ anthropic.messages.create  ← generation (file_a.py)
│  ├─ anthropic.messages.create  ← generation (file_b.py)
│  └─ ...
├─ node.aggregate              ← span (input: finding count; output: score)
├─ node.decide                 ← span
└─ node.escalate (or .approve) ← span
   └─ anthropic.messages.create  ← generation (summary)
```

If `LANGFUSE_*` env is absent, the decorators are a no-op — the agent still runs.

---

## What breaks at 1000x scale (and how I'd fix it)

The spec's job post quotes *"what breaks first at 1000x scale?"* — so here it is for this agent:

1. **Cost.** ~1 LLM call per file × ~80 files cap = ~80 calls/PR. At Numeo's scale (say 10k PRs/mo) you're at 800k LLM calls/mo. **Fix:** prompt-cache the repo-level system prompt + PR metadata in Anthropic, share across per-file calls — the diffs are the only thing that changes. Easily 30-50% cost cut.
2. **Token sprawl on monorepo PRs.** A 5K-LOC PR across 200 files would hit our budget cap *and* leave large parts unreviewed. **Fix:** semantic chunking (group related files via path + import graph) and triage chunking — cheap classifier picks the top-N most-interesting files first, then spends LLM budget there.
3. **GitHub secondary rate limits.** PyGithub doesn't surface these well, and we post N+1 comments per escalation. **Fix:** batch into a single `create_review` payload (already mostly done), backoff on 403, and dedupe comments by file:line hash on re-runs.
4. **Prompt drift without evals.** With no eval harness, prompt changes risk regressions in comment quality. The spec explicitly calls out "Eval frameworks for non-deterministic systems" as one of the role's tech areas. **Fix:** ship a small golden-set of (PR diff → expected finding categories) pairs; run as CI on every prompt change.
5. **Reviewer signal decays.** In a fast-moving team, CODEOWNERS goes stale and `git blame` points at people who left. **Fix:** decay-weight the blame signal toward last-90-days commits, and cross-check against active org members via the GitHub API.
6. **No memory across PRs in a series.** Stacked PRs would each be reviewed in isolation. **Fix:** small vector-store of recent reviews keyed by author+repo so we can flag "you keep introducing X".
7. **Provider single-point-of-failure.** Today we're Claude-only. **Fix:** thin provider abstraction (LiteLLM or hand-rolled) with Claude primary, OpenAI fallback on 5xx — explicitly deferred for the 6-hour budget.

---

## Repo layout

```
.
├── main.py                       # CLI; argparse + LangGraph invocation
├── pr_agent/
│   ├── config.py                 # RuntimeConfig + MODE_PROFILES + weights
│   ├── state.py                  # Pydantic GraphState (single source of truth)
│   ├── graph.py                  # LangGraph wiring
│   ├── llm.py                    # Anthropic + Langfuse + budget cap
│   ├── github_client.py          # PyGithub wrapper (one place for I/O)
│   ├── reviewers.py              # CODEOWNERS parser
│   └── nodes/
│       ├── fetch.py              # All GH reads, in one place
│       ├── chunk.py              # File -> hunk split with token budget
│       ├── analyze.py            # One LLM call per chunk -> findings
│       ├── aggregate.py          # Deterministic risk score
│       ├── decide.py             # 2-line decision
│       ├── approve.py            # APPROVE + LLM summary
│       └── escalate.py           # Pick reviewers + line comments + per-reviewer @-mention
├── prompts/
│   ├── analyze_file.md           # The prompt that drives review quality
│   └── summary.md                # PR-level summary prompt
├── tests/test_chunk.py           # Deterministic-logic smoke tests
├── requirements.txt
├── .env.example
└── README.md
```

---

## Running the tests

```bash
pip install -r requirements.txt
pytest -q
```

The tests cover the deterministic logic (chunking, risk scoring, CODEOWNERS parsing). LLM-touching nodes are integration-tested by running against a real PR.

---

## Future work (deliberately deferred for the 6-hour cap)

- Provider fallback (Claude → OpenAI) via LiteLLM or hand-rolled wrapper
- Bounded concurrency in `analyze` (currently sequential; `asyncio.Semaphore(4-8)`)
- Comment dedupe across re-runs against the same PR (idempotent reviews)
- Eval harness — golden-set regression tests for prompt changes
- Team mentions in CODEOWNERS (separate GitHub API param)
- Prompt caching for repo-level context
- Webhook entry-point — currently one-shot CLI per spec

---

## AI tools used while building

Built this with **Claude Code** in plan mode + edit mode. The planning step (writing out architecture + ambiguity resolutions before any code) was where most of the value came from — Phases 1-5 then executed cleanly because the design decisions were already nailed down. The commit history mirrors the phases so you can see the build progression.

---

## How to verify end-to-end

1. Create a fresh repo + a meaty PR (~300-500 line diff covering: a refactor, an auth-sensitive change, a test file change, a migration).
2. Run both modes:

   ```bash
   python main.py https://github.com/<you>/<repo>/pull/<n> --mode conservative
   python main.py https://github.com/<you>/<repo>/pull/<n> --mode aggressive
   ```

3. Click into the PR on GitHub. You should see:
   - A review posted by your agent's account
   - Line-level comments citing specific findings
   - Reviewers assigned (if escalated)
   - One issue comment per reviewer with @-mention + targeted focus

4. Open the Langfuse trace and confirm every LLM call shows prompt / model / output / tokens / latency.

5. For the large-PR sanity check: a PR with ~2-3K LOC should run without crashing; `state.truncated` should be `False` (or `True` with an honest note in the review body).
