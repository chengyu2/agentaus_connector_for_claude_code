---
name: math-reasoning
description: Get quantitative answers right - arithmetic, percentages, rates, unit conversions, probability, counting, algebra, growth and capacity estimates, and multiple-choice maths sets - by computing every result in Python, confirming it a second independent way, and writing the answers file before refining it. Use when asked to calculate, estimate, convert units, solve an equation, work out a probability or count, check someone's arithmetic, or answer a batch of quantitative or multiple-choice questions. Not for rigorous proofs, though its small-case checks still help there.
license: Apache-2.0
metadata:
  source: claude-plugins-official/plugins/math-olympiad/skills/math-olympiad
  adapted-for: agentaus
---

# Quantitative answers you can stand behind

Confident wrong numbers come from mental arithmetic and from checking an answer with the
same method that produced it. The fix is mechanical: every number comes out of a Python run,
every answer is confirmed by a second, different method, and the deliverable exists before
the checking starts.

## When to use

- A question needs a number, a count, a probability, a rate, a formula or a unit conversion.
- A multiple-choice set with quantitative options.
- "Is this calculation right?", "how long will X take at Y per second", "how many servers do we need", big-O or cost estimates with concrete numbers.
- A set of questions whose answers must be written to a file.
- Not for: analysing a whole dataset (that is data analysis; use this skill for the arithmetic inside it), or finding where something is in code.

## Two rules measured on this bridge

1. **Batch the work.** One script computes every question in one run. A measured run spent
   12 tool calls computing one question per call, hit Claude Code's turn cap, and never
   wrote its answers file. Every answer was lost, including the ones it had computed.
2. **Deliverable first.** When the answers go in a file, write the complete file with a best
   guess for every question before computing anything, then refine it. A file of best
   guesses beats no file.

Budget: whatever the number of questions, plan on about six tool calls - write the draft
file, write the script, run it, fix and re-run once, write the final file, read it back.
Spend extra calls on verification, never on one-question-per-call arithmetic.

## Steps

1. **List the questions.** Give each an ID (`Q1`, `Q2`, ... or the IDs the task uses). For each, note in one line: what is asked, the unit of the answer, the answer format (number, option letter, expression) and the rounding asked for.
2. **Check the reading.** If a question has two plausible readings, write both and pick one with a reason. Common splits: per item vs total; inclusive vs exclusive range; "at least" vs "exactly"; percent vs percentage points; with vs without replacement; order matters vs not; simple vs compound growth.
3. **Write the answers file now** if one is required: `Write` it with every ID and a best guess (a rough estimate is fine), in the exact format the task asked for.
4. **Check libraries once**, only if you want symbolic algebra: `python3 -c "import sympy" 2>&1 | tail -1`. The installed Python normally has **no** `sympy` or `mpmath`. Fall back to `math`, `fractions`, `itertools`, `statistics` and `numpy` (check `numpy` the same way). Do not install packages unless the user agrees.
5. **Write one script for all questions** with `Write` (template below). For each question compute:
   - `a` - the direct method (the formula).
   - `b` - an independent second method: substitute the answer back into the original condition, brute-force the actual numbers, enumerate a small case, simulate, or use a different formula. Typing the same formula twice is not a check.
   - For multiple choice - test **every** option against the question's own condition and record which survive.
6. **Run it once** with `Bash` (`python3 /path/to/check.py`) and read all of the output.
7. **Fix disagreements in one batch.** For every question where `a` and `b` differ, or where zero or several options survive, find the error (usually the reading, a unit, or an off-by-one), fix all of them in the script, and re-run. At most two fix rounds.
8. **Check units and edge cases** for every answer against the list below.
9. **Write the final answers** in one `Write` (or let the script write the file), then `Read` the file back and confirm every ID is present and formatted as asked.
10. **Report** each answer with the check that confirmed it, and name any answer you are not confident in, with both values and why.

## Script template

Copy this shape. Keep every question in the same file; add a block per question.

```python
# check.py - every question in ONE run. Run: python3 check.py
import json, math, itertools, random
from fractions import Fraction as F

def close(x, y, rel=1e-9, abs_tol=1e-12):
    return math.isclose(float(x), float(y), rel_tol=rel, abs_tol=abs_tol)

R = {}  # question id -> {"a": direct, "b": independent check, ...}

# Q1: how many integers 1..1000 are divisible by 3 or 5?  (answer: a count)
a = 1000 // 3 + 1000 // 5 - 1000 // 15                          # A: inclusion-exclusion
b = sum(1 for n in range(1, 1001) if n % 3 == 0 or n % 5 == 0)  # B: brute force
R["Q1"] = {"a": a, "b": b}

# Q2 (multiple choice): 120 km out at 60 km/h, back at 40 km/h. Average speed in km/h?
opts = {"A": 50, "B": 48, "C": 45, "D": 52}
dist_km = 2 * 120
time_h = 120 / 60 + 120 / 40
a = dist_km / time_h                     # A: total distance / total time
b = 2 / (1 / 60 + 1 / 40)                # B: harmonic mean of the two speeds
survivors = [k for k, v in opts.items() if close(dist_km / v, time_h)]  # every option vs the condition
R["Q2"] = {"a": a, "b": b, "survivors": survivors}

for qid, r in R.items():
    ok = close(r["a"], r["b"])
    extra = f" survivors={r['survivors']}" if "survivors" in r else ""
    print(f"{qid}: a={r['a']} b={r['b']} {'AGREE' if ok else 'CHECK'}{extra}")

ANSWERS_PATH = None   # e.g. "answers.json" - set it to write the final file from here
final = {"Q1": R["Q1"]["a"],
         "Q2": R["Q2"]["survivors"][0] if len(R["Q2"]["survivors"]) == 1 else "CHECK"}
if ANSWERS_PATH:
    with open(ANSWERS_PATH, "w") as fh:
        json.dump(final, fh, indent=2)
print(json.dumps(final))
```

Output of this template: `Q1: a=467 b=467 AGREE`, `Q2: a=48.0 b=47.99... AGREE survivors=['B']`.
Note Q2: the tempting answer 50 (the plain average of 60 and 40) is a distractor.

## Multiple choice: check every option, then eliminate

1. Compute your own value first (`a` and `b`), before looking for a matching option.
2. Normalise every option to a number: `1/sqrt(2)` and `sqrt(2)/2` are equal; convert fractions, percents and units to one form. Compare with `math.isclose`, never `==` on floats.
3. Substitute each option into the question's original condition. Keep only the survivors.
4. Exactly one survivor, and it matches your value → that is the answer.
5. Zero survivors → re-read the question: units, reading, rounding. Several survivors → apply the stricter condition the question states (integer, positive, smallest, "at least").
6. Distractors are built from standard mistakes: a skipped unit conversion, off by one, `n` instead of `n - 1`, percent vs percentage points, adding probabilities that should be multiplied. If your value matches an option only after a "fix", suspect the fix.
7. "All of the above" / "none of the above" → test each other option explicitly; do not infer.

## Independent second methods by question type

| Question | Method A | Independent check B |
| --- | --- | --- |
| Equation or root | algebra, formula | substitute into the ORIGINAL equation; the residual is about 0 |
| Counting | `math.comb`, `math.perm`, formula | brute force with `itertools` on the real n, or a small n you can count by hand |
| Probability | formula | exact enumeration with `Fraction`, or Monte Carlo (`random.seed(0)`, 10**6 trials, tolerance about 3/sqrt(trials)) |
| Percent, growth, interest | closed form | loop period by period |
| Rates, time, throughput | division | recompute with units in variable names, plus an order-of-magnitude estimate |
| Series and sums | closed form | direct `sum(...)` over the range |
| Statistics | `statistics` module | `numpy` (mind `ddof`) or the hand formula |
| Cost or big-O with concrete n | formula | count operations in a loop for small n and compare growth |
| Geometry | formula | coordinates: place points, compute lengths and areas numerically |

## Units and edge cases

- Convert every input to one base unit at the top of its block, and put the unit in the variable name (`size_bytes`, `rate_bytes_per_s`, `time_h`).
- Traps: KB (1000) vs KiB (1024); bits vs bytes (x8); ms vs s; minutes vs hours; percent vs fraction; degrees vs radians (`math.sin` takes radians); `math.log` is the natural log; `//` vs `/`; an inclusive count from a to b is `b - a + 1`; sample vs population standard deviation (`statistics.stdev` vs `pstdev`; `numpy.std` defaults to population); month lengths and leap years in date arithmetic; `numpy` integer arrays overflow silently (Python `int` never does).
- Edge values to try in `b`: 0, 1, empty input, negatives, both ends of every range, anything that divides by zero.
- Sanity: probabilities in [0, 1]; counts are non-negative integers; the magnitude matches a rough estimate made before computing.
- Exactness: use `Fraction` for exact rational answers and round only at the end, to what was asked.

## Know when to stop

- **Stop on a question** when `a` and `b` agree within tolerance, the units check out, and (multiple choice) exactly one option survives. Do not re-derive it again.
- **Stop fixing** after two fix rounds. Report both values, say which you trust and why, and mark the answer low confidence. A flagged uncertain answer beats a confident wrong one.
- **Kill** any computation running longer than about 60 seconds: it is probably unbounded. A recurrence like `b(n+1) = 2*b(n)**2 + 1` grows doubly exponentially; work modulo m, or with logarithms, instead of computing the exact value.
- **Never** spend one tool call, or one todo item, per question. The answers file is the tracker.

## If this → do that

- "What is 17.5% of 2,340 after a 12% discount?" → one `Bash` run computing both readings (discount first, then 17.5% of the result; and 17.5% of the original); state which reading you answered.
- 20 multiple-choice questions, answers to `answers.json` → `Write` `answers.json` with 20 best guesses; `Write` one script covering all 20 that tests every option; one `Bash` run; fix every disagreement in one re-run; `Write` the final file; `Read` it back.
- "Is 7 servers right for 3M requests/day at 200 ms each?" → compute rate = 3e6 / 86400 ≈ 34.7 req/s and concurrency = rate × 0.2 s ≈ 6.9; then name the assumption that decides it (peak vs average traffic, one request per server at a time) and give the number under each.
- `a` and `b` disagree → print the intermediate values of both methods and find the first step where they diverge; that step holds the error.
- `import sympy` fails and you need to solve something → `numpy.linalg.solve` for linear systems, `numpy.roots` for polynomials, bisection for one monotone equation; substitute the result back to verify.
- The answer matters (money, capacity, something someone will sign) → in the **same turn** as your script run, also launch one `Agent` with `subagent_type: "general-purpose"`, give it only the question texts (not your answers or working), and ask for all answers from one Python script, as a table. Compare. A checker that has seen your working tends to agree with it. Template in `references/verifier-checks.md`.
- Asked "is this calculation right?" → recompute it independently first, then attack the given working with the checks in `references/verifier-checks.md`.

## Done when

- Every question has an answer in the requested format, and the answers file (if one was asked for) has been read back and contains every ID.
- Every answer came from a Python run and has a second, independent method that agrees - or is flagged with both values and a reason.
- Units match what was asked, and edge values were tried.

## References

Open these with `agentaus_zoom` (absolute path under this skill's directory, `start_line` 1) or `Read`.

- `references/verifier-checks.md` - the ways calculations go wrong, and templates for a blind second checker and for attacking someone else's working. Open it when a result looks too clean, when methods disagree, or when asked to check another person's calculation.
- `references/solving-moves.md` - what to try when the direct method stalls: small cases, working backwards, invariants, symmetry, and a look-back checklist.
