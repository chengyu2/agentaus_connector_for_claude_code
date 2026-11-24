# Behaviours worth knowing about

Things the bridge does on Agentaus turns that you would otherwise only find in the code or the log.

## Turns that stop without finishing

The failure the benchmark found most: the turn ends on an announcement. "We will first
locate questions.json." "We need to write answers.json with JSON object." Or the
deliverable arrives in the reply instead of being done - answers printed as JSON instead
of written to the file, a whole R script printed instead of written and run. The
baseline lost 8 of 15 MMLU-Pro batches this way.

The judge that used to ask only "is this a refusal?" now has three verdicts - REFUSAL,
STALLED, ANSWER - and sees the request the conversation started with and the ledger of
tools already run, so "answers.json written" after a Write reads as done. A stalled turn
is told to take the remaining step of the user's task and to write nothing else. At most
two re-asks per conversation: the first version of this judge had none, called finished
turns stalled 76 times in one run, and pushed one session into writing notes in the
user's Claude memory folder.

## Distillation

`AGENTAUS_DISTILL_RESULTS` condenses tool results over `AGENTAUS_DISTILL_THRESHOLD_TOKENS`
before they reach Agentaus, cached by content so the conversation prefix stays stable and
the compaction cache keeps hitting.

**It is off by default**, on evidence rather than principle. Condensing a 60,000-character
document added about a minute to one turn, and on work where the tool output *is* the
subject — a tender document being edited, a log being read line by line — condensing it
destroys the thing the user asked about.

Turn it on for long agentic sessions that would otherwise compact repeatedly, where
trading fidelity for window is the right trade, and measure the compaction rate before and
after. Its win shows up as *fewer compactions*, not as better answers.

## Where the output goes

**The bridge never writes files.** It translates requests and responses; it has no idea
what a deliverable is. Output reaches disk exactly one way: Agentaus calls `Write` or
`Edit`, and **Claude Code** executes it — through the same permission prompt as any other
write. Nothing is dumped automatically, and there is no output directory.

Two consequences worth knowing:

- **Ask for the file explicitly.** "Review 5.1.1" gets you an answer in the terminal.
  "Review 5.1.1 and write it to `<path>`" gets you a file.
- **Markdown, not `.docx`.** A `.docx` is a zip archive, so a text-writing tool cannot
  produce one — Agentaus can only write text. Have it write `.md`, then convert
  (`pandoc out.md -o out.docx`) if the buyer needs Word.

`agentaus_search` and `agentaus_zoom` are the exception in the other direction: the bridge
runs those itself, so they never reach a permission prompt. They are read-only and
confined by `AGENTAUS_SEARCH_ROOTS`.

## Truncated tool output is read back from disk

When a tool produces more than the client will carry, Claude Code saves the whole output
and passes on a preview:

```
Output too large (1.6MB). Full output saved to: …/tool-results/abc.txt
Preview (first 2KB):
```

Sensible for a client, terrible for a model. That is how one session came to survey a
repository from **2 KB of a 1.6 MB listing** and invent the rest — it was told where the
full output was and did not read it.

The bridge runs on the same machine, so it does: the preview is replaced with the real
content before anything else reads the turn, capped by `AGENTAUS_RESTORE_MAX_BYTES` with
the remainder announced rather than dropped. A preview naming a file that does not exist is
left alone — a preview is bad, and a preview replaced by nothing is worse.

### And `Bash` is restricted for searching

The same session reached for `find` rather than `agentaus_search`, which is what produced
the 1.6 MB in the first place. `Bash` now carries the treatment `Grep` got — the
restriction at the **front** of its description, because a caveat after a paragraph loses
to a strong prior:

> *RESTRICTED: do NOT use this to search or survey. `find`, `ls -R`, `grep -r` and `cat`
> over many files all produce more output than this conversation can carry… Use Bash for
> RUNNING things — tests, builds, git, one command with bounded output.*

Only the search-shaped uses. Bash is still how you run a test.

## MCP tools over the schema budget

Tool focus (`AGENTAUS_TOOL_FOCUS`) holds back Claude Code's non-coding harness tools, and
never your custom tools. MCP tools are kept too, until the tool schemas in one request pass
**40 KB**. Past that, an MCP server you have not named and the conversation has not used is
held back, largest first.

The budget exists because connectors are large. With the claude.ai Slack and Docs
connectors attached, MCP schemas were 85 KB of every call, about 28,000 tokens that
compaction cannot touch, and a long session overflowed the 131k window with only 78 KB of
conversation. If a server you need is being held back, name it in your request. The code
is `_TOOL_BUDGET_BYTES` in `agentaus_bridge/augment.py`.

## Typing "ultracode" runs the bridge's own agent graph

On an Agentaus turn, the multi-agent `Workflow` script the model writes is replaced by an
agent graph the bridge runs itself (`AGENTAUS_WORKFLOW_GRAPH`, on by default). Expect a
long, expensive turn: measured runs started 12-53 agents and took 10-80 minutes.
`AGENTAUS_GRAPH_KIND`, `AGENTAUS_GRAPH_WIDTH` and `AGENTAUS_GRAPH_FOR_CODE` tune it; see
the [configuration reference](configuration.md). Claude Code's own
`CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` caps how many workflow agents run at once, and
driving ultracode from `claude -p` needs `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` so the
headless run waits for the result. Why it works this way, and what it measured:
[design/ultracode.md](../design/ultracode.md).
