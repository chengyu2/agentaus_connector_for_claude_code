#!/bin/zsh
# Clean Opus reference on the held-out test halves, identical setup to the Agentaus arms.
# Cds to its own directory. Needs AB_PY (and ANTHROPIC spend behind bridge 8795, bridges.sh baseline).
cd -- "$(dirname -- "$0")"
PY=${AB_PY:-python3}
for r in 1 2 3; do $PY ab.py run --suite econ_r --arms opus --label ctest_r$r --jobs 2 --resume; done
$PY ab.py run --suite mmlu_pro --split test --arms opus --label ctest --jobs 3 --resume
$PY ab.py run --suite mmlu --split test --arms opus --label ctest --jobs 3 --resume
$PY ab.py run --suite he --split test --arms opus --label ctest --jobs 3 --resume
echo DONE cref
