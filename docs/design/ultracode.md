# Ultracode: multi-agent workflows on Agentaus

Typing **ultracode** in Claude Code opts the turn into multi-agent orchestration: the main
loop calls the `Workflow` tool with a JavaScript script, and Claude Code runs it in the
background, starting one subagent per `agent()` call - often several at once - and
delivering the script's result later as a `<task-notification>` message. Each workflow
subagent is offered a `StructuredOutput` tool and told that one call to it *is* its return
value.

Driving that path at Agentaus through the real Claude Code binary (2.1.300, headless,
with the reminder the VS Code extension adds for the keyword) found a fault at every step.
Each is now handled, and each has a test in `tests/test_ultracode.py`:

| Step | What went wrong on Agentaus | What the bridge does now |
| --- | --- | --- |
| Offering the tool | Tool focus held `Workflow` back unless the user typed its name, and headless Claude Code sends no ultracode reminder - so the model was never offered the tool it was told to use | The opt-in - the `ultracode` keyword, an invoked `workflow-authoring` skill, or asking for a multi-agent workflow - keeps `Workflow` on the wire for the session, decided before compaction so a summary cannot withdraw it. CI wording ("write a workflow that runs pytest") is not an opt-in |
| Knowing how | Nothing told the model what a script may contain, that it runs in the background, or what to do with the result | A Markdown-structured `Multi-agent workflows` section: look at the inputs first, the five functions a script has, self-contained subagent prompts, independent cross-checks, a bracket checklist, and a worked example |
| Writing the script | Every script was one line, so `export const meta = {...} const X = ...` is a parse error; four of the first five rejections were exactly that, one three times running | A lexical repair inserts `;` between `}` and a keyword that can only start a statement - never inside a string, template, comment or regex, and never changing a valid script. Replayed on the scripts already rejected, it fixed 5 of 8 and left every valid one unchanged |
| After the launch | "It is running, I will report back" - the right reply - was judged a stall, the re-ask said take the step, and the model launched the same workflow a second time; the duplicate's notification ended the session on a dump of the workflow reference | The stall judge sits out a reply to a successful launch. A `Write` or `Edit` into the working directory while the workflow is still running is returned to the model to wait for the result |
| Subagent turns | Each subagent was planned, reviewed, grounding-checked and syntax-fixed like a main loop: up to four extra calls per subagent, and rewrites of the value its parent reads. Its prompt carried the 2,000-character reporting contract | Subagent turns (marked `cc_is_subagent=true`) skip the planner and every rewrite pass, and get a return contract instead: work with tools, then exactly one `StructuredOutput` call |
| Thin subagent prompts | Agentaus wrote prompts like "compute the required statistics from the provided files" and "repeat the calculations as before" - with no rules or questions in them. The subagents answered confidently and wrong | Every request carries Claude Code's session id, so a subagent is shown its session's request as context: definitions, rules, file names. Its own prompt still sets its task |
| Schemas | A workflow's schema is whatever its author wrote. `"type": ["string", "null"]` crashed the argument check and ended the turn; a `null` in a nullable required field was "corrected" until the rounds ran out | Type lists and nullable fields are understood; a schema the checker cannot read is forwarded unchecked rather than failing the turn |
| The result arrives | The `<task-notification>` sits inside a system reminder, which the bridge strips from what its helpers read. The planner re-planned the task from scratch, and the grounding check - seeing numbers no tool call produced - rewrote eight correct figures as "the repo may contain 12 956 ...; I have not read it" | A notification turn is not re-planned, and its result counts as evidence for the grounding check. A rewrite pass shown only the first 12,000 characters of an answer no longer replaces all of it |
| Paths | Two long sandbox paths side by side got spliced: the deliverable was written to the session scratchpad's prefix joined to the working directory's tail | The guidance names the working directory and says deliverables go there. A path whose directory does not exist, and whose prefix is a garbled copy of the working directory, is moved into it |
| Documents | Claude Code 2.1.300's `Read` now refuses a `.docx` outright - "This tool cannot read binary files" - where it used to return zip noise, and the bridge's document repair skipped error results, so it never ran. Separately, across four tender runs Agentaus never opened the `.docx` that held most of the evidence | The refusal is replaced with the document's text where LibreOffice is installed. An opted-in main loop is given the working directory's files as a checklist, each document marked with what actually opens it here: `Read`, or a named Python reader in `Bash` |
| Window | MCP tools are never hidden, and the claude.ai Slack and Docs connectors added 85 KB of schema to every call - ~28,000 tokens that compaction cannot touch. A long session overflowed the 131k window with 78 KB of conversation, and every retry failed the same way | Over a 40 KB schema budget, an MCP server the user has not named and the conversation has not used is held back, largest first. Two new harness tools (`PublishPlugin`, `WaitForMcpServers`) join the hold-back list |
| Stopping early | The stall judge had two re-asks per conversation for its lifetime. A long session spent both early, and its last reply - "We will apply multiple Edit calls." - ended the run with the fixes unwritten | The budget refills after three tool calls of progress, so a model that keeps stopping without acting still runs out |

## Measuring it

`abbench/ultra/ultra.py` runs the comparison: the same three tasks, each the kind of
work ultracode is for, given to Agentaus and to Opus 5.5 through the same bridge, graded
afterwards with no model in the loop.

```bash
cd abbench/ultra
../../.venv/bin/python ultra.py setup        # fixtures from the local sources below
../../.venv/bin/python ultra.py run --arms agentaus,opus --tasks rdti,tender,c1913,c1913h,he10 --reps 2 --label u1 --port 8787
../../.venv/bin/python ultra.py report --label u1
```

| Task | What it is | Graded on |
| --- | --- | --- |
| `rdti` | Data analysis across four spreadsheets of the public ATO R&D Tax Incentive report: ABNs written three ways, an amendment file that overrides claims (one amendment is $0), two entities with no ABN | Eight figures computed independently by `build_rdti.py` |
| `c1913` | Five bugs injected into the 1913_collective analysis pipeline, one of them not among the reported symptoms, plus a `--json` feature | The fixed module's behaviour against the untouched original |
| `tender` | Answer a nine-question RFQ from a past proposal (`.docx`) and a README, citing sources; two questions the evidence cannot answer | Sixteen required facts, citations, and refusing to invent insurance or client records |
| `c1913h` | The same pipeline with nine injected bugs, five of them not among the reported symptoms | As `c1913`, with a check per extra bug |
| `he10` | Ten HumanEval functions in one file - the ten that a frontier model or Agentaus got wrong in the earlier A/B | Each function on its original tests plus edge inputs checked against the canonical solution, HumanEval+-style |

Fixtures are built under `~/agentaus_ultra_runs` from the ATO file in `~/Downloads` and a
local `1913_collective` checkout (`ULTRA_ATO`, `ULTRA_1913` override the paths). The
1913 sources contain personal information, so nothing from them is committed here. Runs
that end on a spend limit or a refused connection are excluded, not scored zero.

Headless Claude Code stops waiting for background work after a ceiling and kills it: a
workflow ended ten minutes in with "[Request interrupted by user]" and its run scored
zero. An open VS Code session never does that, so the harness sets
`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0`. Set it yourself to drive ultracode from
`claude -p`. It also sets `CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000` and
`CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES=1`: measured against a mock, a request whose
non-streaming fallback times out is otherwise sent 10 times, which is how five runs died on
their first request after 62 minutes during an outage.

Rounds are run with `loop.py`, not `ultra.py run`:

```bash
../../.venv/bin/python loop.py run --label d15 --valid 3 --manage-bridge   # one bridge on 8800, width 6
../../.venv/bin/python loop.py report --labels d15
../../.venv/bin/python loop.py regrade --out regrade.jsonl                  # copies; run folders untouched
```

It launches an Agentaus run only when the bridge's `/healthz/upstream` and a realistic
streamed probe say Agentaus is serving, aborts a run whose last 15 minutes were an outage
(at least half the calls failing, or under 2 good calls a minute, at a throttle limit of 2
or less), and voids runs whose window degraded, rerunning them until each task has N valid
runs. Runs that finish through a moderate slump stay valid and are counted as "survived
degraded". Every attempt goes to `~/agentaus_ultra_runs/ledger.jsonl` with the bridge's
loaded-source hash, the upstream's health over the run, the score and each check. The
report shows valid runs only: the mean, a bootstrap interval and the range against the
rolling Opus mean (an Opus arm runs every round), with the old bars second. Replayed
against the d1-d13 bridge logs, the rules abort and void all 10 outage zeros that have a
record, and no run from a healthy window. They also abort d8 tender: it had written a
1.0 answer before the outage, then sat in it until the 7200 s cap. The ledger keeps an
aborted run's score, but the run does not count.

## Results

Measured October 2026, Claude Code 2.1.300, the VS Code ultracode reminder on every run.
Scores are the mean of each run's graded fraction; spend-limit runs excluded.

| Task | Agentaus, bridge before (n=2) | Agentaus, this bridge (n=8) | Opus 5.5 (n=2) | Median time, Agentaus / Opus |
| --- | --- | --- | --- | --- |
| `rdti` - spreadsheets | 0.38 | **0.61** (0.38-0.88) | 1.00 | 630s / 271s |
| `tender` - RFQ from a `.docx` | 0.70 | **0.71** (0.65-0.80) | 0.98 | 276s / 792s |
| `c1913` - five bugs and a feature | 0.69 | **0.53** (0.00-0.88) | 1.00 | 749s / 259s |

What these numbers say, and what they do not:

- **The mechanics now work.** On this bridge Agentaus launched a workflow on all 16
  analysis and tender runs and on 4 of the 8 coding runs (it chose to fix the code
  alone on the rest). 29 of its 36 `Workflow` calls were accepted; the 7 rejected were
  errors the `;` repair cannot fix, such as unbalanced brackets, and it corrected them on
  the next call. Its subagents return through `StructuredOutput`, and it waits for the
  notification and finishes from the result. Every fault in the table above was seen
  live, and each has a test.
- **The results are not yet Claude's.** Opus 5.5 scored at or near full marks on every
  valid run; Agentaus averages 0.53-0.71 with wide spread. What remains is the work
  itself: the $0-amendment trap (a falsy zero) caught it on most `rdti` runs, it rarely
  found the bug that was not among the reported symptoms, and on `tender` its workflows
  extracted the `.docx` but did not pass that text on to the subagent that wrote the
  answer. Two `c1913` runs still ended on an announced edit after the re-ask budget ran
  out, which is why stall re-asks now also set `tool_choice: "required"` - honoured
  about one time in three when measured.
- **The before/after comparison is small.** n=2 against n=8, with scores that moved by
  0.5 between rounds of the same bridge. The before arm was offered `Workflow` too,
  because the VS Code reminder names the tool - the headless opt-in fault only bites
  outside the extension. Read the first column as "no worse and sometimes much better",
  not as a measured effect size. The last round of the "this bridge" column predates
  the MCP schema budget, which is the one change written after it.
- **Agentaus capacity is a factor.** Twelve concurrent sessions saw 27-34 Agentaus 5xx
  and Cloudflare 524 responses per bridge and a 25-second median call, which tripled
  wall times for both arms. `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` caps a
  workflow's fan-out if the upstream is the bottleneck.

## An experiment that did not pay: a fixed agent graph

**Superseded by the [dynamic graph](#the-dynamic-graph-planners-decide-the-shape-in-markdown).**
`AGENTAUS_GRAPH_KIND=fixed` selects it. It is kept because the measurements below are what
led to the dynamic design.

The fixes above made Agentaus's own workflows *run*. They did not make them *good*: its
scripts carried prompts like "compute the required statistics from the provided files",
told a verifier to "repeat the calculations as before", and never passed one agent's
result to the next. Every fix aimed at the script it wrote was one more rule it then had
to follow.

So the experiment: when an opted-in Agentaus main loop calls `Workflow`, the bridge runs its own graph,
[`agentaus_bridge/graphs/solve.js`](../../agentaus_bridge/graphs/solve.js), in place of the
model's script, passing the user's request and the working directory. Agentaus only
decides to run it, waits, and checks the result. Four principles shape it, each from how
this model measurably behaves:

1. **More agents.** A run starts 100-200 of them.
2. **One simple prompt per agent.** Each has one job, a few Markdown lines, and only the
   tools that job needs - `Bash`, `Read`, `Write`, `Edit`, `StructuredOutput`. Agents that
   only reason over text the graph hands them (extractors, expanders, planners) get
   `StructuredOutput` alone, and answer in one call instead of a tool loop.
3. **More steps.** Ten stages, each a fan-out of small agents:

   | Stage | Agents |
   | --- | --- |
   | Survey | two listers |
   | Read | one reader per file; documents saved as plain text for everyone after |
   | Draft | one agent does the whole task quickly in a copy - the backup deliverable |
   | Requirements | three extractors and a merger, beside two trap hunters (a data lens and a code-versus-docstring lens); a deterministic backstop adds back any request line no requirement covers, and each unreported bug a hunter finds becomes a requirement |
   | Expand | two per requirement, splitting it into separate outputs - one per question, function, bug or section; the finer split wins |
   | Plan | one per item: inputs, method, traps, check |
   | Solve | three per item, each told to use a different method, read-only, code changes made in a scratch copy and saved as a patch file |
   | Reconcile | a judge per item who re-tests the traps with its own code even when the solvers agree; a fourth solver when there is no majority; a second and third judge before any judge overrides the solvers |
   | Assemble | one agent applies the chosen patches with `patch`; one writes each deliverable |
   | Verify | three checkers per requirement and per rule |
   | Fix | one fixer per real failure, starting from a backup and undone if the checkers still find the problem |

4. **Redundancy.** Every agent has a backup that runs when it dies, returns nothing, or
   returns a plan instead of the work ("We will perform the edits."). A value is reached
   three ways before it is trusted. A failure needs two of three checkers to show it,
   because one weak checker's mistaken "fail" once sent fixers to overwrite eight correct
   answers.

Two things the live runs forced, both enforced by the bridge rather than asked of the
model: at most `AGENTAUS_GRAPH_WIDTH` (default 8) agents run at once, because Agentaus
answers 524s past about eight concurrent sessions; and any tool result over 16,000
characters in a graph subagent is cut to head and tail, because agents printed whole
spreadsheets into their own context - 108 requests overflowed the window in one round,
none after the cut.

Agentaus throughput sets the price: about 30 calls a minute across all sessions, so a
graph run takes tens of minutes where Opus's own workflow takes a few. The graph has a time
budget (50 minutes): past it, unsettled items fall back to the draft and checking is
skipped, so a slow upstream costs quality, never the deliverable. Each agent also has a
ten-minute deadline, after which its backup runs.

### What it measured

Same harness, same five tasks, the VS Code ultracode reminder on every run. Opus 5.5 is the
reference; "own workflow" is Agentaus writing its own scripts, with every fix in the table
further up. Scores are means over runs, the range in brackets.

| Task | Agentaus, own workflow | Agentaus, agent graph | Opus 5.5 |
| --- | --- | --- | --- |
| `rdti` - spreadsheets | 0.61 (0.38-0.88, n=8) | 0.53 (0.25-1.00, n=4) | 1.00 (n=2) |
| `tender` - RFQ from a `.docx` | 0.71 (0.65-0.80, n=8) | 0.59 (0.30-0.80, n=4) | 0.98 (n=2) |
| `he10` - ten HumanEval+ functions | **0.80** (n=3) | 0.40 (0.30-0.50, n=4) | 0.90 (n=1) |
| `c1913` - five bugs and a feature | 0.57 (0.00-0.88, n=9) | 0.54 (0.00-0.88, n=3) | 1.00 (n=2) |
| `c1913h` - nine bugs, five unreported | 0.14 (0.00-0.42, n=3) | 0.19 (0.00-0.42, n=4) | 1.00 (n=1) |
| Median time | 5-15 minutes | 35-70 minutes | 2-8 minutes |

Averaged over runs, the graph scored below Agentaus's own workflow on four of the five tasks -
and on the fifth, `c1913h`, both are near zero - at five to ten times the time. No
configuration came close to Opus. What the runs showed:

- **Its middle worked; its edges did not.** On `rdti` three solvers and a judge repeatedly
  settled seven of eight answers correctly - the $0 amendment included - and one run scored
  1.00. Most runs lost those answers at the boundaries: requirements split into steps instead
  of outputs, duplicate items eating the budget, a writer rewriting a code file, fixers
  "correcting" right answers, verifiers and judges reporting input files missing without
  looking. Each was found in a live run and fixed (an adversarial review then confirmed 21
  more defects, also fixed), and each fix moved the score less than the run-to-run noise.
- **More agents amplify a weak judge as much as they correct one.** Three solvers agreeing on
  a wrong total, overturned by one right judge and restored by a second wrong one, is the
  graph working exactly as designed.
- **Throughput sets the ceiling.** Agentaus served 17-30 calls a minute across all sessions,
  with Cloudflare 524s past about eight concurrent ones. A graph of 100-200 agents needs
  1,000-3,000 calls, so it spends most of its budget queueing; the time budget and draft
  fallback keep it from ending empty, not from ending short.
- **Code is where it loses most.** Ten functions in one file: 0.80 by Agentaus's own script
  in ten minutes, 0.40 through the graph in an hour. Even with code routed away from the
  graph (`AGENTAUS_GRAPH_FOR_CODE=false`, the default when the graph is on), it did not beat
  the own-workflow numbers on data or documents.

The tasks Opus leaves headroom on are where this should be retried: `he10` is the closest
(0.80 against 0.90), with Agentaus's own workflow.

## The dynamic graph: planners decide the shape, in Markdown

**On by default for ultracode** (`AGENTAUS_WORKFLOW_GRAPH=true`, `AGENTAUS_GRAPH_KIND=dynamic`).

The fixed graph gave every task the same ten stages and every item three solvers. Here the
shape is decided per task by agents, and the script
([`graphs/dynamic.js`](../../agentaus_bridge/graphs/dynamic.js)) is only the runtime that carries
it out:

1. **Scout** - one agent surveys the project and writes a Markdown brief.
2. **Plan, twice** - a lean planner and a careful planner each write a plan in Markdown: one
   `## Subtask` per output, each with its difficulty, how many solvers it gets (1 easy, 2-3
   medium, 3-5 hard), whether it is checked, how its answers are merged, and what it depends on.
3. **Review** - a reviewer compares the two and writes the plan the work follows.
4. **Run the plan** - each subtask once its dependencies are done: its solvers, a **merger**
   that combines their answers by the plan's strategy (`vote`, `best-evidence`, `combine`,
   `union`, `pick-change`), and a checker if the plan asked for one; a failed check gets one
   more solver and a second merge.
5. **Assemble, then check** - an assembler writes the deliverables and applies patches; two
   checkers check the finished work; a fixer runs if either finds a problem.

Every role works from a skill file, in [`graphs/skills/`](../../agentaus_bridge/graphs/skills) -
`scout`, `plan`, `review-plan`, `solve`, `merge`, `check`, `assemble`, `fix` - and every agent
hands the next one Markdown. The bridge passes the skills in with the script, so no agent
spends a call reading its own instructions. Planners, the reviewer and mergers reason over
text they are handed and get no tools at all: one call each. The runtime keeps only what an
agent should not decide: at most `AGENTAUS_GRAPH_WIDTH` agents at once, a cap of 36 solvers,
a backup for every agent, deadlines, and a one-subtask fallback if no plan can be read.

### What it measured

Same harness and tasks as above, two runs each.

| Task | Agentaus, own workflow | Fixed graph | **Dynamic graph** | Opus 5.5 |
| --- | --- | --- | --- | --- |
| `c1913` - five bugs and a feature | 0.57 | 0.54 | **0.88** (0.88, 0.88) | 1.00 |
| `c1913h` - nine bugs, five unreported | 0.14 | 0.19 | **0.50** (0.50, 0.50) | 1.00 |
| `he10` - ten HumanEval+ functions | **0.80** | 0.40 | 0.60 (0.60, 0.60) | 0.90 |
| `rdti` - spreadsheets | **0.61** | 0.53 | 0.56 (0.50, 0.62) | 1.00 |
| `tender` - RFQ from a `.docx` | **0.71** | 0.59 | 0.65 (0.80, 0.50) | 0.98 |
| **Mean** | 0.57 | 0.45 | **0.64** | 0.98 |
| Agents per run | - | 100-200 | 12-53 | - |
| Typical time | 5-15 min | 35-70 min | 10-80 min | 2-8 min |

The first approach here to beat Agentaus's own workflow on average, and the gains are where
Agentaus was weakest: the two bug-fix tasks, identical across both runs. It uses a quarter
of the fixed graph's agents because easy work gets one solver. It loses on `he10`, is level
on data and documents within the noise, and is still well short of Opus everywhere. Two runs
per task is a small sample: read the bug-fix gains as solid and the rest as not yet decided.

### Rounds d16-d17: measuring instead of trusting

Same tasks, the benchmark loop running unattended on the dynamic graph, every valid run
counted (a run is void only when Agentaus itself was down or out of quota, never because it
scored badly). Mean (lowest-highest, runs):

| Task | Agentaus, own workflow | Dynamic graph, first | **Dynamic graph, d16-d17** | Typical time |
| --- | --- | --- | --- | --- |
| `c1913` - five bugs and a feature | 0.57 | 0.88 | **0.84** (0.25-1.00, n=8) | 79 min |
| `c1913h` - nine bugs, five unreported | 0.14 | 0.50 | **0.37** (0.08-0.58, n=7) | 82 min |
| `he10` - ten HumanEval+ functions | 0.80 | 0.60 | **0.90** (0.80-1.00, n=6) | 72 min |
| `rdti` - spreadsheets | 0.61 | 0.56 | **1.00** (1.00-1.00, n=8) | 21 min |
| `tender` - RFQ from a `.docx` | 0.71 | 0.65 | **0.99** (0.90-1.00, n=9) | 25 min |
| **Mean** | 0.57 | 0.64 | **0.82** | |

The data and document tasks now match Opus run after run; `c1913` and `he10` reach 1.00
and usually land one check short; `c1913h` beats Agentaus's own workflow on every run but
swings widely and is the open problem. Most of the gain came from replacing an agent's word
with a measurement:

- **Picks are measured, and fairly.** A solver's pytest-style tests are called, a test that
  imports the module by its bare name finds it, a test that loads the solver's own file
  tests the candidate instead, and a candidate's import of its own module or exact copy of
  a module table is removed before it is measured.
- **The docstring's traps are tested.** Spec-test writers aim at inputs where readings
  disagree, judges settle lone cases even in a late graph, and the agent proposing new
  readings has its saved reading run and is asked again with the failing output.
- **An audited function keeps its original** unless a measurement - or, failing one, every
  judge with an input that shows the bug - says the change is a fix.
- **Checkers see what the program prints** on the project's own data before and after a
  change, and a feature that changes the output of an existing command is failed without
  asking anyone.
- **The bridge is robust to Agentaus itself:** a spent quota (HTTP 402) is waited out like an
  outage, the graph is launched by the bridge when a turn opts in, and a graph agent cannot
  wander into another run's scratch folder.
