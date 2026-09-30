---
name: analyse-data
description: Analyse a dataset and report what it shows - aggregates, distributions, charts, regressions and other statistical models, in Python or R. Use when asked to analyse, model, chart, plot, visualise, or compute statistics over a CSV, spreadsheet, or a dataset pulled from a portal such as data.gov.au or the ABS. Covers checking the file is what it claims to be, running the code rather than predicting its output, and reporting the numbers with the caveats that decide whether they mean anything.
---

# Analysing data, and knowing whether the answer means anything

An analysis is wrong in two different ways, and only one of them is arithmetic. The
other is computing the right statistic over the wrong rows. Both are cheap to prevent
and neither is caught by re-reading your own code.

## 1. Find the toolchain before you write against it

Do not assume an import exists. On this machine the default `python3` carries `pandas`,
`numpy` and `openpyxl` but **not** `statsmodels`, `scipy` or `matplotlib`, and `R` is
not installed at all. A script written against the stack you expected fails on its
first run and you spend the turn on `ModuleNotFoundError` instead of the question.

One bounded command settles it:

```bash
python3 -c "import importlib;[print(m, bool(importlib.util.find_spec(m))) for m in
 ['pandas','numpy','scipy','statsmodels','matplotlib','pyarrow']]"; which Rscript
```

If a project has a virtualenv, prefer its interpreter — `./.venv/bin/python` — over the
system one, and say in your answer which interpreter you used. If something needed is
missing, install it or say plainly that you could not, and never silently substitute a
hand-rolled version of what the missing library does.

**Asked for R with no R installed**: say so and offer the Python equivalent. Do not
write R you cannot run and present it as a result.

## 2. Check the file is what it claims to be, before trusting a number from it

This is the step that separates an analysis from a calculation, and it is the one that
gets skipped. Before the headline number, establish:

- **Row count, and what a row is.** One entity? One entity-year? A total row at the
  bottom that will double your sum?
- **Which rows are not the population you were asked about.** Measured on the ATO's
  2022-23 R&D Tax Incentive file: it carries 179 late-lodged **2021-22** claims, so
  13,135 rows describe 12,956 entities for the stated year. The Gini moves in the third
  decimal; a per-entity mean moves more.
- **Columns that supersede other columns.** The same file has an *amended* expenditure
  column populated on 295 rows. Which one you use is a decision, not a detail.
- **Missing, zero and sentinel values.** How many, and whether dropping them is what
  the question meant. Report how many rows an analysis actually used.
- **Units and scale.** Dollars or thousands of dollars, nominal or real, and which base
  year. A real series compared against a nominal one is a plausible-looking chart of
  nothing.

Read the metadata sheet or the data dictionary if the file ships one. It usually says
exactly this and costs one read.

## 3. Run the code; never predict what it would print

Write the script to a file and run it. Do not answer from what the code *should*
produce — that is the one failure this whole skill exists to prevent, and a saved script
is also what makes the result reproducible by someone else.

**Report the value the library computed. Never re-derive a statistic by hand.** Measured
failure: a p-value reported as `0.3756` because it was computed from a normal
approximation, where `statsmodels` reported `0.3964` from the t-distribution on 10
degrees of freedom. Read `.pvalues`, `.bse`, `.rsquared` off the fitted object and print
them. The same goes for a confidence interval, a standard error and a test statistic.

Print the numbers you intend to quote. A number that was never printed is a number you
are about to state from memory.

## 4. Verify the artefact, do not assume it

After a script writes a chart or a file, confirm it:

```bash
ls -l rnd_distribution.png && python3 -c "from PIL import Image;im=Image.open('rnd_distribution.png');print(im.size)"
```

A model that reports "the chart is saved" without checking is sometimes reporting a
`matplotlib` call that raised after the figure was built. Check size and dimensions, not
just existence — a 0-byte or blank PNG passes an existence test.

For charts: label both axes with units, title the figure, and put the statistic you are
claiming *in* the panel where it can be read against the curve.

## 5. Report the analysis, not just the answer

The numbers are the smallest part of what was asked for. A reply that is a bare figure
or a JSON blob has answered a calculator question, not an analytical one. Measured: on
identical tasks with identical correct results, the useful reply ran 1,800-2,300
characters and the thin one ran 119.

State, briefly:

- **The headline numbers**, plainly, no hedging. You ran the code; the run is your
  evidence. Do not write "the coefficient may be 0.849" about a value you just computed.
- **What was used** — which rows, which column, which interpreter, how many observations
  entered the model after dropping missings.
- **What it means** — for a coefficient, the direction, the size in the units of the
  data, and whether it is distinguishable from zero. `n=12` and `R²=0.07` is not "no
  relationship"; it is "this sample cannot tell you".
- **What would change it** — the caveats from step 2, each with the number it moves and
  by how much, so a reader can judge whether it matters.
- **What you could not do**, if anything.

## Rules

- Never state a figure you did not see printed.
- Never present an unexecuted script's expected output as a result.
- Say how many observations a model actually used, every time.
- A statistically insignificant result is a finding. Report it as one rather than
  reaching for a specification that produces a star.
- Do not `agentaus_search` a data file to answer a question about its contents. Search
  reads prose by meaning; a 13,000-row spreadsheet is computed over, not read.
