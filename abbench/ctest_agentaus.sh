#!/bin/zsh
# Final: Agentaus baseline vs tuned v4 on the held-out test halves. No Claude usage.
# Cds to its own directory. Needs AB_PY and the benchmark data in abbench/data/.
cd -- "$(dirname -- "$0")"
PY=${AB_PY:-python3}
A=agentaus_base,agentaus_tuned
for r in 1 2 3; do $PY ab.py run --suite econ_r --arms $A --label ctest_r$r --jobs 4 --resume; done
$PY ab.py run --suite mmlu_pro --split test --arms $A --label ctest --jobs 8 --resume
$PY ab.py run --suite mmlu --split test --arms $A --label ctest --jobs 8 --resume
$PY ab.py run --suite he --split test --arms $A --label ctest --jobs 8 --resume
echo DONE ctest_agentaus
