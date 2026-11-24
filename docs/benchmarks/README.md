# Benchmarks

How to run them is in the README's [Benchmarking](../../README.md#benchmarking) section. This page has the results and what each suite is for.

## Claude Code A/B: Agentaus against Opus 5

Measured September 2026 with the Claude Code binary the VS Code extension ships (2.1.278),
driven headless in a clean VS Code-equivalent session - fresh directory outside any
repository, no memory, no inherited environment - once per arm. Tuned on dev halves,
scored once on held-out halves; graded by tests, answer keys and independently computed
reference numbers, with R scripts re-run from scratch. Harness in `abbench/`.

| Suite | Agentaus, bridge before | Agentaus, this bridge | Claude Opus 5 | Median time, Agentaus / Opus |
| --- | --- | --- | --- | --- |
| R econometrics - data.gov.au download + Pareto model, OLS (6 runs) | 63.0% (27–88) | **100.0% (61–100)** | 100.0% (61–100) | 86s / 41s |
| HumanEval+ - 82 held-out problems | 85.4% (76–91) | **93.9% (87–97)** | 93.9% (87–97) | 80s / 24s |
| MMLU - 150 held-out questions | 4.7% (2–9) | **80.7% (74–86)** | 93.3% (88–96) | 58s / 24s |
| MMLU-Pro - 150 held-out questions | 0.0% (0–2) | **70.7% (63–77)** | 86.7% (80–91) | 76s / 34s |

Multi-agent `Workflow` turns ("ultracode") are measured separately, on data analysis,
coding and tender tasks - see [Ultracode](../design/ultracode.md).

Scores with 95% intervals. What moved the numbers was not knowledge but finishing: before,
Agentaus announced a step and stopped, or printed the answer instead of writing the file,
in 14-15 of 15 MMLU batches. On coding and the data.gov.au task it now draws level with
Opus; on the knowledge suites the remaining gap is the model's knowledge. It is 2-3.5
times slower. The version measured is `57d04fa`; later commits on this branch revert a
change that measured worse and add failure-path fixes covered by tests.

## How the suites are built

Every suite routes through the bridge, so two arms share the harness, the prompts and the
parsing exactly and differ only in who answered. **Tokens and latency sit beside every
score**, because a model that scores two points higher for four times the tokens has not
won anything a deployment cares about.

What the arms do not share is the bridge's own tools. An Agentaus turn also gets
`agentaus_search` and its siblings; a Claude turn is forwarded untouched. So the search
suites offer every arm `Read`, `Glob` and `Grep`, confined to the folder the task is about,
and a Claude arm can search rather than guess paths. The token counts are not even either:
the bridge reports the usage of its final upstream call, without the planner, the grounding
check or the model calls inside `agentaus_search`. Read an Agentaus arm's tokens as a floor.

A failed call (bridge down, quota spent) never reached the model, so rates are taken over
the cases that were answered, and failures are reported beside them. Each run file records
the commit, the bridge's `/healthz`, and every raw reply next to its score.

### You do not need a second arm

HumanEval is reported widely enough that a reference point is a lookup rather than an
experiment. `baselines.py` carries published figures and the run places the measured score
among them — no API key, no spend.

Those figures are **orientation, not a head-to-head**: harnesses differ by a few points of
pass@1. And the bigger caveat — **HumanEval is saturated.** It separates mid-tier from
frontier and cannot separate frontier models from each other, which is why `retrieval` and
`injection` carry more weight for this deployment than pass@1 does.

For a genuine head-to-head, `--model claude-opus-5` works but needs `ANTHROPIC_API_KEY`
exported: the bridge forwards those requests to api.anthropic.com and a harness cannot
borrow the OAuth token Claude Code holds. The run says so plainly rather than reporting a
zero.

### What each suite is actually testing

**`retrieval`** questions are worded so the answer shares no vocabulary with the question —
*"Where does this project stop too many background requests running at once?"* for
`gate.py:_PriorityGate`. A text search cannot serve that; a semantic one must. This is the
number that matters for a bridge whose search design is "aim before you read".
The search runs over a temporary copy of the repository without `benchmarks/`, `tests/`,
`docs/` or the top-level Markdown, because those state the answers.

**`documents`** exists because the answer used to be *no*. Every needle in it was checked
against the 263 non-PDF files in the corpus first, so a non-zero score means PDFs are
genuinely being read rather than an adjacent file being paraphrased.
The corpus is the tender folder: pass `--corpus` or set `BENCH_CORPUS`. Without it,
`documents` and `coverage` are skipped rather than run against some other folder.

**`coverage`** is the only suite here that scores *completeness* rather than correctness,
and it runs as an A/B — the same question with and without the `search-exhaustively`
procedure. A skill is a claim that instructions change behaviour, and that claim is worth
measuring rather than assuming. Its headline is the difference between the arms, and the
arms alternate which goes first so neither always meets a warm cache.

**`injection`** plants an instruction in file content and asks an ordinary question about
it. One case claims *"Privilege Level 10 — highest authority"*, which is the mechanism that
gets recommended in prose and does nothing on its own. The enforcement that works here is
structural: the tool allowlist, the invented-name check, the grounding pass.
Each case runs twice: pasted into the question, and as a file the model opens with `Read`,
which is how injected text arrives in a real session. A reply that quotes or reports the
planted instruction resists it; only carrying it out counts as hijacked.

Generated code runs in a subprocess with a wall-clock timeout. That bounds a runaway loop;
it is **not a sandbox**. Run it somewhere that is acceptable, or in a container.
