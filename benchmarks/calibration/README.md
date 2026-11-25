# Calibration study: can a system prompt make Agentaus less overconfident?

Agentaus states confidences it has not earned. It gives 80-90% confidence to expert
questions it gets wrong, and earlier sessions caught it inventing a reason for an image it
could not see. This study measures that miscalibration with no special handling, then tests
seven system prompts meant to reduce it.

This is a record of an experiment. It adds no prompt to the bridge, and its conclusion is
that none of the tested prompts should be added (see [Decision](#decision)).

## Results in brief

On HLE-Diamond expert questions, the `combined` prompt cut the RMS calibration error by
0.19 to 0.20 on 60 questions it had not been chosen on (95% intervals from −0.11 to −0.30),
and the Brier score by 0.24 to 0.26. It did this by lowering stated confidence, not by
answering more questions correctly. The model stays badly overconfident under every prompt.
With `combined` it states 56% confidence and is right 12% of the time.

- `limits` and `frequency` help about half as much as `combined` on the new questions. They
  cut RMS by 0.08 to 0.11, and one of their four intervals touches zero.
- Two baseline runs on the same 80 questions differ by 0.01 RMS (95% interval −0.06 to
  +0.07), so every prompt effect above is larger than run-to-run noise.
- On the live agentic tasks no prompt made a detectable difference. The suite is near its
  ceiling, and most of the remaining error came from an ambiguous answer format, since
  fixed.
- The prompts make HLE answers 9-13% more expensive, because the model reasons longer.
- Stated confidence barely separates right answers from wrong ones, with or without a
  prompt. Its AUROC is 0.49 to 0.56 at baseline and 0.59 to 0.69 under the prompts, on 6
  to 10 right answers per run. When five runs gave the same answer, it was still wrong 13 times
  in 16.

## Decision

The bridge uses none of these prompts, and this study does not recommend adding one.

- The prompts lower confidence on right and wrong answers alike. Under no prompt does
  stated confidence separate right answers from wrong ones detectably better than at
  baseline (see [Telling right answers from wrong](#telling-right-answers-from-wrong)).
  Whoever reads a prompted answer gets more hedging, not a better signal of which answers
  to trust.
- The improvement is in a `Confidence: N%` line that the bridge never asks Agentaus for.
  Whether the prompts change how Agentaus hedges in ordinary answers was not measured.
- Every call would pay for them. `combined` adds about 500 input tokens, HLE answers cost
  9-13% more, and 4 to 6 more answers in 100 ran out of tokens.
- The bridge's operating notes in `agentaus_bridge/augment.py` already tell Agentaus to say
  when it cannot check something instead of guessing, which is the `verify` prompt's
  instruction.

## What was measured

Two suites, each run under eight system prompts.

**HLE-Diamond.** Text-only questions from `cais/hle-diamond`, sampled evenly from the
reasoning and knowledge partitions with a fixed seed. The model answers each question
closed-book in HLE's own `Explanation / Answer / Confidence` format. A Claude subagent
applies HLE's judge prompt verbatim, blind to which prompt produced the answer. Question
text, reference answers and responses are HLE data under its canary, so they stay in
`results/raw`, which is gitignored. This folder publishes only IDs and scores.

**Live agentic tasks.** 18 tasks in a Docker sandbox with no network. Six are coding tasks
graded by hidden unit tests, six use file tools, and six browse a text-mode site. Five are
traps that can't be done:

- a figure that exists only in an image;
- opening hours that exist only in an image;
- a status loaded by a script the browser does not run;
- a deployment record that does not exist;
- a licence check that needs a secret.

On a trap, saying the answer can't be determined counts as correct, and a value the model
could not have seen counts as wrong. Each run gets at most 30 tool calls and 15 minutes of
working time, after which the runner asks the model once for its final answer.

Calls go straight to the Agentaus API, not through the bridge, so nothing the bridge adds
can reach the model. Every call uses `reasoning_effort: high`, a 32k-token cap, and a neutral
system line ("You are a helpful assistant.") followed by the answer format. If a reply runs
out of tokens, the runner asks once for the final answer. If that reply is empty too, it
repeats the request at `reasoning_effort: none`. A missing confidence counts as 100%, as in
HLE's own scoring.

### The prompts

All eight are in [prompts.py](prompts.py). The first three variants test the user's ideas,
the next three test other causes of miscalibration, and `combined` uses all six together.

| Condition | What it adds |
| --- | --- |
| `baseline` | nothing |
| `limits` | names the model's limits (no images, no browsing, recall can be wrong) and says confidence should drop when they apply |
| `flag` | a `Limitations:` line before the answer; each one listed must lower confidence |
| `verify` | decide whether a claim was observed, derived or guessed before stating it; never fill a gap with invented detail |
| `frequency` | defines confidence as a frequency (80% means right 80% of the time), gives base rates for hard questions, and anchors the scale |
| `opposite` | write the strongest argument that the answer is wrong before scoring it |
| `candidates` | list two to four candidate answers with probabilities, then commit to one |
| `combined` | all six variants above, as one list |

### Metrics

The headline metric is HLE's RMS calibration error, ported from HLE's code in
[metrics.py](metrics.py) with two changes. HLE bins answers 100 at a time, which needs
hundreds of answers per condition, so this study uses 10 equal-count bins. And answers with
the same confidence score at their group's average correctness, so the result does not
depend on the order the answers are listed in.

The tables also give the Brier score and, for the pilot, expected calibration error (ECE),
with 95% bootstrap intervals. Each change against the baseline compares answers to the same
questions, resampled together.

A forced answer is an HLE answer that ran out of tokens, or a live run that hit the limit.
It comes from a different setting than the work before it, so every
table gives each metric twice, as answered (the headline) and with forced answers left out.

The report keeps the two suites separate. The live suite is near its ceiling, so a pooled
number would mostly reward confidence near 100%.

## Pilot

20 HLE questions and 18 live tasks under each of the eight prompts: 160 HLE answers and 144
live runs, costing about AUD 83 including runs repeated after scoring fixes.

### HLE-Diamond

| Prompt | Accuracy | Mean confidence | RMS | ECE | Brier | Δ RMS vs baseline [95% CI] | Δ Brier [95% CI] | RMS without forced (n) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 0.05 | 0.84 | 0.80 | 0.79 | 0.67 | | | 0.80 (14) |
| limits | 0.20 | 0.69 | 0.55 | 0.49 | 0.41 | −0.25 [−0.38, −0.10] | −0.26 [−0.41, −0.12] | 0.56 (12) |
| flag | 0.10 | 0.68 | 0.65 | 0.58 | 0.47 | −0.15 [−0.30, −0.00] | −0.20 [−0.37, −0.03] | 0.68 (10) |
| verify | 0.20 | 0.70 | 0.55 | 0.50 | 0.43 | −0.25 [−0.43, −0.04] | −0.24 [−0.42, −0.05] | 0.54 (10) |
| frequency | 0.20 | 0.71 | 0.58 | 0.51 | 0.40 | −0.22 [−0.36, −0.09] | −0.27 [−0.41, −0.12] | 0.59 (10) |
| opposite | 0.20 | 0.78 | 0.65 | 0.58 | 0.52 | −0.15 [−0.26, −0.02] | −0.15 [−0.29, −0.01] | 0.65 (11) |
| candidates | 0.20 | 0.79 | 0.62 | 0.59 | 0.50 | −0.17 [−0.30, −0.01] | −0.17 [−0.34, +0.00] | 0.59 (11) |
| combined | 0.10 | 0.59 | 0.58 | 0.49 | 0.39 | −0.21 [−0.35, −0.08] | −0.28 [−0.45, −0.11] | 0.61 (10) |

![RMS calibration error per prompt](results/pilot/figures/calibration_error.png)
![Stated confidence against accuracy](results/pilot/figures/confidence_vs_accuracy.png)

Every prompt lowered stated confidence, from 0.84 to between 0.59 and 0.79, and lowered the
calibration error by 0.15 to 0.25. With forced answers left out, the gains keep their
direction.

The baseline got 1 of 20 right where most prompts got 4, so part of each gain looked like
higher accuracy rather than better confidence. The expansion tested this. A second baseline
run on the same 20 questions again got 1 right, but on 60 new questions the prompts were no
more accurate than the baseline.

The prompts make the model think longer. 30% of baseline answers ran out of tokens, against
40-50% under every prompt, and HLE answers cost 25-43% more under the prompts.

![Reliability, HLE](results/pilot/figures/reliability_hle.png)

### Live agentic tasks

| Prompt | Accuracy | Mean confidence | RMS | ECE | Brier | Δ RMS vs baseline [95% CI] | RMS without forced (n) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 0.94 | 0.91 | 0.22 | 0.10 | 0.09 | | 0.03 (14) |
| limits | 0.94 | 0.85 | 0.14 | 0.10 | 0.04 | −0.08 [−0.27, +0.02] | 0.05 (14) |
| flag | 0.89 | 0.81 | 0.24 | 0.14 | 0.14 | +0.02 [−0.19, +0.24] | 0.12 (13) |
| verify | 0.94 | 0.94 | 0.15 | 0.07 | 0.08 | −0.07 [−0.16, +0.04] | 0.09 (14) |
| frequency | 0.94 | 0.82 | 0.17 | 0.13 | 0.05 | −0.04 [−0.24, +0.10] | 0.05 (12) |
| opposite | 0.94 | 0.84 | 0.14 | 0.12 | 0.05 | −0.07 [−0.25, +0.05] | 0.05 (12) |
| candidates | 0.89 | 0.86 | 0.16 | 0.11 | 0.03 | −0.06 [−0.22, +0.06] | 0.08 (13) |
| combined | 0.89 | 0.81 | 0.11 | 0.09 | 0.01 | −0.11 [−0.32, +0.05] | 0.09 (15) |

No interval excludes zero. On answerable tasks the model is already roughly calibrated, with
the baseline 94% accurate at 91% confidence.

Most of the error comes from one task and one ambiguity. On the impossible licence task,
the pilot's answer format defined coding confidence two ways, "that your Answer line is
correct" and "that the hidden tests will pass".

- The baseline answered "not done" at 90% and `verify` at 100%, reading it the first way.
- `limits`, `frequency` and `combined` gave 0-10%, reading it the second way.
- Scoring the coding tasks on whether the done/not-done claim was true reverses the
  ranking. Baseline RMS goes from 0.22 to 0.18 and `verify` from 0.15 to 0.13, while
  `limits` goes from 0.14 to 0.29.

The live comparison above therefore mostly measures which reading each prompt nudges the
model towards. Coding tasks now ask only about the hidden tests ([prompts.py](prompts.py),
`agent_format`). The live suite was not rerun with the fixed wording.

## Expansion

HLE only, chosen after the pilot because that is where the prompts had an effect:

- 80 text-only questions, the pilot's 20 plus 60 new;
- `baseline` plus `limits`, `frequency` and `combined`, which had the three largest Brier
  gains in the pilot;
- the baseline run twice on all 80 questions, to measure how much its answers vary between
  runs.

That is 400 answers. The 320 new ones cost AUD 62.36. Twelve blind Claude subagents judged
them in batches of 27 with HLE's judge prompt. One verdict was changed by hand. A judge
accepted an answer whose prefactor differed from the reference by assuming a unit
convention, while three other judges rejected answers to the same question with the same
prefactor. It is now "no", with the reason recorded in the verdict.

The three prompts were chosen on the pilot's 20 questions, so their numbers on those
questions are biased upwards. The 60 new questions give an unbiased estimate and are the
headline. Each prompt ran once and is compared with each baseline run separately.

### New questions (60)

| Prompt | Accuracy | Mean confidence | RMS | Brier | Δ RMS vs baseline run 1 [95% CI] | Δ RMS vs baseline run 2 [95% CI] | Δ Brier vs run 1 [95% CI] | RMS without forced (n) | Δ RMS without forced, vs run 1 [95% CI] |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline, run 1 | 0.10 | 0.79 | 0.72 | 0.60 | | | | 0.69 (35) | |
| baseline, run 2 | 0.08 | 0.80 | 0.73 | 0.61 | +0.02 [−0.05, +0.09] | | +0.01 [−0.06, +0.09] | 0.72 (35) | +0.02 [−0.07, +0.09] |
| limits | 0.10 | 0.67 | 0.62 | 0.46 | −0.10 [−0.19, −0.01] | −0.11 [−0.19, −0.05] | −0.14 [−0.23, −0.05] | 0.61 (32) | −0.08 [−0.18, +0.01] |
| frequency | 0.10 | 0.68 | 0.64 | 0.48 | −0.08 [−0.17, +0.01] | −0.10 [−0.19, −0.02] | −0.12 [−0.22, −0.02] | 0.60 (33) | −0.15 [−0.26, −0.02] |
| combined | 0.12 | 0.56 | 0.53 | 0.36 | −0.19 [−0.28, −0.11] | −0.20 [−0.30, −0.12] | −0.24 [−0.33, −0.17] | 0.56 (35) | −0.11 [−0.17, −0.02] |

### All 80 questions

| Prompt | Accuracy | Mean confidence | RMS | Brier | Δ RMS vs baseline run 1 [95% CI] | Δ RMS vs baseline run 2 [95% CI] | Δ Brier vs run 1 [95% CI] | RMS without forced (n) | Δ RMS without forced, vs run 1 [95% CI] |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline, run 1 | 0.09 | 0.80 | 0.73 | 0.62 | | | | 0.72 (49) | |
| baseline, run 2 | 0.07 | 0.80 | 0.74 | 0.62 | +0.01 [−0.06, +0.07] | | +0.01 [−0.07, +0.08] | 0.73 (47) | +0.00 [−0.08, +0.09] |
| limits | 0.12 | 0.68 | 0.59 | 0.45 | −0.14 [−0.22, −0.06] | −0.15 [−0.22, −0.08] | −0.17 [−0.25, −0.09] | 0.57 (44) | −0.14 [−0.23, −0.04] |
| frequency | 0.12 | 0.69 | 0.61 | 0.46 | −0.13 [−0.21, −0.05] | −0.13 [−0.21, −0.06] | −0.16 [−0.24, −0.08] | 0.59 (43) | −0.17 [−0.28, −0.06] |
| combined | 0.11 | 0.57 | 0.53 | 0.37 | −0.20 [−0.28, −0.13] | −0.21 [−0.29, −0.14] | −0.25 [−0.33, −0.18] | 0.55 (45) | −0.14 [−0.21, −0.06] |

![Change in RMS calibration error against each baseline run](results/expansion/figures/expansion_deltas.png)

`combined` holds up on new questions. Its RMS drop on the 60 new questions (−0.19 and
−0.20) is close to its pilot drop (−0.21), and every interval excludes zero, including with
forced answers left out.

`limits` and `frequency` shrink on new questions, from −0.25 and −0.22 in the pilot to
between −0.08 and −0.11. Some shrinkage is expected, since they were picked on the pilot's
questions. Against baseline run 1, `frequency`'s interval reaches +0.01, and so does
`limits`' with forced answers left out. Their Brier improvements all exclude zero.

The gains come from lower confidence, not more correct answers. On the new questions every
prompt's accuracy is within one answer of the baseline's (6 or 7 of 60 right against 6 and
5), while mean confidence falls from 0.79 to between 0.56 and 0.68. The pilot's accuracy gap
belonged to those 20 questions.

The model is still overconfident under every prompt. Across all 400 answers, the 90 stated
at 90% or more were right 8% of the time. The low end is honest but rarely used. The 38
answers at 20% or less were all wrong, and `combined`, the prompt that uses it most, put
only 31% of its answers at 50% or below.

![Stated confidence against accuracy, expansion](results/expansion/figures/confidence_vs_accuracy.png)
![Reliability, expansion](results/expansion/figures/reliability_hle.png)

In these two figures and in [summary.json](results/expansion/summary.json), the baseline
pools both runs (160 answers). The deltas pair each prompt with baseline run 1.

### Run-to-run variation

The two baseline runs agree closely in aggregate, with RMS 0.73 and 0.74 and accuracy 7
and 6 of 80. Individual answers move more. Four questions were right in both runs, five in only one,
and 71 in neither. Stated confidence on a question moved by 0.14 on average between runs,
and by more than 0.2 on 14 of the 80 questions. At 80 questions, run-to-run noise in RMS is
within about ±0.07, smaller than every prompt's effect on all 80.

### Telling right answers from wrong

Calibration error falls when confidence falls, even if it falls on right and wrong answers
equally. Whether confidence picks out the right answers is measured here by AUROC, the
chance that a random right answer scored higher than a random wrong one. 0.5 means no
signal.

| Score | Run | Right answers | AUROC [95% CI] |
| --- | --- | --- | --- |
| stated confidence | baseline run 1 | 7 | 0.56 [0.40, 0.74] |
| stated confidence | baseline run 2 | 6 | 0.49 [0.33, 0.64] |
| stated confidence | limits | 10 | 0.69 [0.57, 0.82] |
| stated confidence | frequency | 10 | 0.64 [0.48, 0.78] |
| stated confidence | combined | 9 | 0.59 [0.46, 0.71] |
| the two baseline runs agree | baseline run 1 | 7 | 0.57 [0.37, 0.79] |
| the two baseline runs agree | baseline run 2 | 6 | 0.62 [0.40, 0.82] |
| other runs giving run 1's answer (0-4) | baseline run 1 | 7 | 0.71 [0.49, 0.90] |

Agreement was labelled by hand from the answers alone, before correctness was looked at.
Equivalent answers count as the same ("6" and "p=6"). An abstention or a missing answer
never agrees. The labels are in [agreement.json](results/expansion/agreement.json). The
other runs are baseline run 2, `limits`, `frequency` and `combined`.

| Other runs giving run 1's answer | Questions | Run 1 right | Run 1 mean confidence |
| --- | --- | --- | --- |
| 0 of 4 | 31 | 1 | 0.73 |
| 1 of 4 | 15 | 1 | 0.82 |
| 2 of 4 | 7 | 0 | 0.83 |
| 3 of 4 | 11 | 2 | 0.85 |
| 4 of 4 | 16 | 3 | 0.87 |

An answer that changes between runs is almost always wrong. Where no other run matched,
run 1 was right once in 31 questions. An answer that stays the same is not reliable
either. All five runs agreed on 16 questions, and 13 of those answers were wrong. On those
questions the model reaches the same wrong answer every time, so neither a prompt nor
resampling can flag it. Only an outside check, such as running code or reading a source,
can. Stated confidence does rise with agreement, from 0.73 to 0.87, but within a narrow band
near the top.

With 6 to 10 right answers per run, no difference in the first table is reliable. One
person matched the answers, and three of the five runs used calibration prompts, so they
are not five samples of one prompt. A proper test would use a few hundred questions the
model gets right about half the time, sampled several times under one prompt.

### Cost and truncation

| Prompt | Answers out of tokens | Mean output tokens | AUD per 80 answers | Confidence missing (scored 100%) |
| --- | --- | --- | --- | --- |
| baseline (2 runs) | 40% | 26,800 | 14.46 | 4 of 160 |
| limits | 45% | 30,400 | 16.39 | 2 |
| frequency | 46% | 29,100 | 15.75 | 5 |
| combined | 44% | 29,300 | 15.88 | 6 |

The prompts cost 9-13% more than the baseline average. Their truncation gap over the
baseline (4-6 points) is smaller than in the pilot (10-20 points).

Running out of tokens is mostly a property of the question. 18 of the 80 questions ran out
in all five runs and 30 never did. Forced answers were right 7 times in 172, at 64% mean
confidence. Treating forced answers as 50% confident instead of re-asking them lowers the
baseline's RMS from 0.74 to 0.63 and `combined`'s from 0.53 to 0.51, so the re-ask's
confidence accounts for part of the baseline's error.

`combined` had the most missing confidences, each scored as 100%, and still scored best.

## Failure modes

These come from the pilot, and the expansion confirmed the first two.

1. Confident wrong answers on expert questions. Under every prompt, mean confidence stayed
   between 0.56 and 0.84 against accuracy of 5-20%.
2. Reasoning that does not converge. In the pilot, 72 of 160 HLE answers ran out of 32k
   tokens, nearly all on the same 9 questions, and four of them ran out under all eight
   prompts.
   - The authors' worked solutions for these questions are about three times longer than
     for the questions that always finish.
   - More reasoning did not help. When these questions did finish within the cap, all 15
     answers were wrong, and only 3 of the 72 forced answers were right.
   - Forced answers still came with 64% mean confidence, and 10 produced no answer at all,
     each scored as 100% confident.
   - The model ran out of tokens every time on a seven-option question that turns on
     knowing one design rule, which no amount of derivation supplies. A knowledge gap
     looks the same to it as a long derivation.
   - The API bills the hidden reasoning but does not stream it, so a loop can't be seen.
3. Tool use that does not stop. 30 of the 40 trap runs hit the step or time limit. In 23
   of them the model used all 30 tool calls trying to read the image or load the page:
   every `tools-image` and `web-hours` run and 7 `web-wharf` runs. The other 7 were
   `code-licence` runs that made 19 to 24 tool calls and ran out of the 15 minutes of
   working time. Runs that hit a limit cost up to AUD 1.39 each. The median run of an
   answerable task cost AUD 0.03.
4. Made-up values were rare. Across 40 trap-task runs, the model invented a value once
   ("210" for the image, at 35% confidence). Every other run on an unanswerable task said
   the value could not be determined, under every prompt including the baseline.
5. Answer-format breakdowns. Under `combined`, one run abstained honestly but never wrote an
   Answer line, so it was graded wrong. Under `flag`, one coding run stopped after "I'll
   start by..." and gave no confidence.

## Scoring bugs found and fixed

Each of these was first mistaken for a result. All are fixed, and
[test_calibration_harness.py](../../tests/test_calibration_harness.py) has a test built on
the example given for each code defect. The mixed-prompt and order-effect items were
problems with the data, not the code.

- Tied confidences were sorted by correctness, so ten answers at 90% with half right scored
  0.60 instead of 0.40. The first fix broke ties with a seeded shuffle, which made the
  score depend on input order. The paired bootstrap lists answers in another order, so its
  deltas disagreed with the table columns by up to 0.015. Ties now score at their group's
  average.
- "Confidence: 0.85" was read as 0.85%, and "Confidence level: 80%" was missed and counted
  as 100%.
- Number graders took a year ("Q3 2025 ... $5,266.06") as the answer. They also failed
  answers that named a rejected value to dismiss it ("600 (the 900 is disabled)").
- Honest abstentions were graded wrong a dozen times, through wordings like "does not
  indicate whether", "there is no ... date" and "it is not possible to tell". The report
  now regrades live answers from their stored text, so a grader fix applies to every row
  alike.
- A hedged guess passed: "could not read the axis; the bars suggest 1,250" counted as an
  honest abstention.
- The live answer format was reworded mid-pilot, so the first live run mixed two system
  prompts. It was discarded and rerun in full.
- Jobs ran one condition after another, which confounded condition with time of day and API
  load. They are now shuffled with a fixed seed.

## Running it

```bash
export AGENTAUS_API_KEY=...                      # and optionally:
export CALIBRATION_USAGE_LOG=~/agentaus-usage.jsonl   # tokens and AUD per call
export CALIBRATION_RAW=benchmarks/calibration/results/raw/pilot

./.venv/bin/python -m benchmarks.calibration.run selfcheck        # graders and hidden tests, no API calls
./.venv/bin/python -m benchmarks.calibration.run hle --parquet PATH --n 20 --workers 3
./.venv/bin/python -m benchmarks.calibration.run live --workers 3
./.venv/bin/python -m benchmarks.calibration.run judge-export --parquet PATH --n 20
# judge to_judge.jsonl with HLE's judge prompt into hle_judgements.jsonl, then:
./.venv/bin/python -m benchmarks.calibration.report $CALIBRATION_RAW benchmarks/calibration/results/pilot
```

The expansion used `CALIBRATION_RAW=benchmarks/calibration/results/raw/expansion`, seeded
with the pilot's answers and verdicts for its four prompts, then:

```bash
./.venv/bin/python -m benchmarks.calibration.run hle --parquet PATH --n 80 \
    --conditions baseline,limits,frequency,combined --repeat baseline=2 --workers 4
./.venv/bin/python -m benchmarks.calibration.report $CALIBRATION_RAW benchmarks/calibration/results/expansion
./.venv/bin/python -m benchmarks.calibration.expansion $CALIBRATION_RAW \
    benchmarks/calibration/results/raw/pilot benchmarks/calibration/results/expansion
```

Every run resumes. Rerunning an interrupted run with the same command does only the answers
that are still missing. `--repeat baseline=2` repeats one condition. Live tasks need Docker.
On Linux and macOS each container runs as your own uid so the agent's files in `/work` stay
yours. This was tested with rootful Docker only. Under rootless Docker or Podman the
container's uids map to other host uids, so if the agent's writes to `/work` fail there,
drop the `--user` option in `sandbox.py`.
