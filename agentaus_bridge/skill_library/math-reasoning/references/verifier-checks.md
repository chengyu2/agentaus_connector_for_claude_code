# Verifier checks for everyday calculations

A short version of the math-olympiad verifier patterns, reworded for quantitative answers in
coding sessions and question sets. Each check is something to run on a candidate answer or
on someone else's working. Self-checking misses these because it reuses the reasoning that
produced the answer.

## Contents

1. Seven reasons an answer is wrong
2. The checks, one by one
3. Template: blind second checker
4. Template: attack a given calculation
5. How to use the checkers' verdicts

## 1. Seven reasons an answer is wrong

Look for any one of these. If none fires after a genuine attempt, accept the answer - but not
because it was written confidently.

1. **A step does not follow.** Includes direction errors: A > B and C > D does not give A - C > B - D; dividing an inequality by a negative number flips it.
2. **A precondition is not met.** A formula is used outside its assumptions (see check 2.3).
3. **It fails on a small case.** The claimed formula gives the wrong value at n = 1, 2, 3 or at the first non-trivial case.
4. **It is circular.** The check reuses what it is checking - for example substituting into the derived equation instead of the original one.
5. **It proves too much.** The same method, applied elsewhere, would give a probability above 1, a negative count, or a result known to be false.
6. **It answers the wrong reading.** An easier version of the question was answered.
7. **It hand-waves at the crux.** "Clearly", "obviously", "it follows that" sit exactly on the step that carries the answer.

## 2. The checks

### 2.1 Wrong reading (the most common failure)

- Write 2-3 readings of the question. Flag any reading that makes it trivial.
- Check the usual splits: per item vs total; inclusive vs exclusive; "at least" vs "exactly"; percent vs percentage points; with vs without replacement; order matters or not; "find all" vs "find one".
- If the question comes from a set where others are harder, a one-line answer is a red flag for the easy misreading.

### 2.2 Test past the first non-trivial case

- A pattern seen at n = 1, 2, 3 may be forced by degeneracy (for example, every 1x2 block has rank 1).
- Ask "what makes the small cases easy?", find the first value where that stops, and confirm the claim there too.

### 2.3 Re-check preconditions from scratch

- Multiplying probabilities needs independence.
- A normal approximation needs a large enough sample.
- The arithmetic-series formula needs a constant step; the geometric one a constant ratio.
- The compound-interest formula assumes a fixed compounding period.
- Write the formula's assumptions out, then check each against this problem, not against the label.

### 2.4 Formula scope

- Trace each formula to where it was derived. A formula proved for a special case (two groups, equal sizes, integer n) is often applied silently to the general case.

### 2.5 Same words, different quantity

- Average speed over equal distances is the harmonic mean, not the average of the speeds.
- A mean of ratios is not the ratio of totals.
- Median is not mean; APR is not the effective annual rate; percent change is not percentage-point change.
- State what the question needs (pointwise, total, per unit, per period), then state what the formula gives.

### 2.6 Generalise the one-line step, then try a tiny counterexample

- Write the step as a general rule. Try it on the smallest case with two items.
- Example: "the average of group averages is the overall average" fails for groups of size 1 and 3.
- If the general rule fails but your specific answer still checks out numerically, something special about this problem makes it true. Find it; otherwise the answer is luck.

### 2.7 Check on the original, not the proxy

- After taking logs, normalising, changing units or simplifying, verify the final claim on the original values. Errors hide in the transformation.

### 2.8 Quantifier direction

- "For all x in S" over a smaller S is weaker (fewer obligations). "There exists x in S" over a smaller S is stronger (fewer candidates). Reversed strength claims are a common slip.

### 2.9 Boundaries

- Evaluate at both ends of every range: division by zero, `log(0)`, a series that diverges at the edge, an empty set.

### 2.10 Circular reduction

- If the working ends with "so it remains to show X", substitute the working's own earlier equations into X. If you get the original question back, nothing was reduced.

## 3. Template: blind second checker

Give this to one `Agent` call (`subagent_type: "general-purpose"`), launched in the same turn
as your own script run. Paste only the question texts - never your answers or working.

```
Solve each of these questions yourself. You have not seen anyone else's answers or working.
Compute every question in ONE Python script (python3; sympy is probably not installed - use
math, fractions, itertools, statistics, numpy), run it once, and confirm each answer with a
second, different method (substitution, brute force, enumeration or simulation).
For multiple choice, test every option against the question's condition.
Return one line per question:  ID | answer | method | second-method result | confident? (yes/no, why)

QUESTIONS:
[paste the question texts only]
```

## 4. Template: attack a given calculation

Use when asked "is this right?", or to stress-test your own final working. Give the checker
the question and the cleaned calculation only - strip false starts and "let me try" prose,
because a long chain of reasoning reads as supporting evidence even when it is wrong.

```
Below is a question and a proposed calculation. You are not grading it; you are trying to
break it. Assume it contains one subtle error. Check: the reading of the question, the
direction of every inequality, every formula's preconditions, the units, and the smallest
cases. Recompute the result in Python with a different method.
Return:
VERDICT: CORRECT | INCORRECT | UNCLEAR
HARDEST STEP: the step you attacked hardest and why it held (or broke)
IF INCORRECT: the step, why it fails, and the corrected value

QUESTION: [...]
CALCULATION: [...]
```

## 5. How to use the checkers' verdicts

- Do not tell a checker what anyone else concluded. Each should believe it is the only reviewer.
- A checker agrees with you → accept.
- One checker disagrees → recompute that question yourself with a third method before changing anything.
- Two independent checkers disagree with you → treat your answer as wrong, or report it as unresolved with all values.
- The solver never counts as its own checker.
