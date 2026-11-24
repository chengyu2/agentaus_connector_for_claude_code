# When a claim outruns the evidence

The self-review pass has to sit out tool-derived turns — a reviewer shown only the request
and the answer reads a well-grounded reply as an unverified claim, and rewrites a correct
answer into *"please provide the document"*. That fix was necessary and it left a hole:
**the one kind of turn where a claim can outrun the evidence had nothing watching it.**

What that looked like in practice. Asked to survey a repository, a real session ran one
`find`, and then wrote:

> *"the repo's `_notes.md` usually enforces a single, consistent fit label"* — never opened it
> *"No apparent breach of the 'prohibitions live once' rule"* — **a policy that does not exist**

Both stated as findings. Nothing checked them.

So there is a second pass for exactly those turns, and it is not blind: it is given the
**ledger of tools actually executed**, which turns *"you claim this about a file you never
opened"* from a guess into something checkable.

```
answer + <tools_it_actually_ran>  →  GROUNDED
                                 →  GAPS, then one line per unsupported claim
                                        ↓
                                 rewrite: each gap removed, or restated as the open
                                 question it actually is — everything else untouched
```

An unreadable verdict counts as grounded. Rewriting a good answer is the more expensive
mistake, and this pass exists because the last one made it.


## The ledger says a call happened, not what it returned

That design has a blind spot, and on analytical work it destroys exactly what it was
built to protect. The ledger renders a call as one line:

```
- Bash(cd /w && python3 fit.py) -> ok
```

Asked whether an answer could know the coefficient it just reported, a checker holding
that line can only say no. Verbatim, on a fitted regression:

> GAPS
> the specific numeric results (n = 12, coefficient = 0.8493, standard error = 0.9585,
> R-squared = 0.0728, p-value = 0.3964) - **would need the actual output from fit.py
> showing these values.**

The output *was* in the conversation. The ledger simply does not carry it. The rewrite
then turned every correct figure into *"the coefficient may be 0.849; I have not read the
source"* - an answer contradicting itself while the right numbers sat in the file the turn
had just written to disk. This is the bridge's own rule ([Working with Agentaus](working-with-agentaus.md)) biting the pass that was written
to enforce it: **a helper pass must never judge what it cannot see.**

So the grounding check is given `render_evidence` instead - the same ledger, plus a
bounded excerpt of what each call returned, newest first, head and tail of anything long.
A traceback says what broke on its last line and a table says what it holds on its first;
the middle of either is padding.

Same turn, after:

```
GROUNDED
```

And the failure it exists for still fails: given one `find` and an answer asserting the
contents of four files it never opened, all four were caught, plus a policy that does not
exist. The distinction it now draws is between *a number a command printed* - supported,
because the run is the evidence - and *a claim about a file nothing opened*.

It costs about 2,700 extra input tokens on a grounding call, bounded at roughly 6,000.
That is the price of not shredding correct arithmetic.

**It does not fire on short answers.** `worth_grounding_check` has a 400-character floor,
and a turn that replies with a JSON object clears neither it nor any need for it.

## The bridge's own tools are evidence too

The same rule was broken a second time, by a blind spot in where the evidence came from.
The ledger is built from the message list Claude Code sends, and **bridge tool calls never
enter that list** - `agentaus_search`, `agentaus_zoom` and the rest run inside the turn.
Two passes judged answers without seeing them:

- **Grounding stripped a correct fact.** An answer stating `CACHE_TTL_SECONDS = 600` in
  `src/config.py`, read by `agentaus_zoom` a minute earlier, was judged to *"need a tool read
  of src/config.py"* and rewritten to *"I have not read it"*. The live draft had been right.
- **Self-review invented an answer.** A turn that used only bridge tools looked like a
  prose turn, so it went to self-review, which sees only the request and the answer. An
  answer citing `src/retry.py` lines 7-9, just zoomed, was "revised" into a description of
  urllib3's `Retry.get_backoff_time()` - from memory, in a project with no urllib3 in it.

Both are fixed the same way. Every bridge tool result from the turn (already kept to catch
repeated calls) is rendered into the evidence alongside the ledger. A turn that used any
bridge tool goes to the grounding check, never to the evidence-blind self-review.
