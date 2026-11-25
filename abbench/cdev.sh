#!/bin/zsh
# Clean dev round: isolated runs, baseline vs tuned (v4).
# Cds to its own directory, so it runs from any checkout of this repository.
# Needs AB_PY (an interpreter with agentaus_bridge's requirements installed) and the
# benchmark data downloaded into abbench/data/ - see abbench/README.md.
cd -- "$(dirname -- "$0")"
PY=${AB_PY:-python3}
A=agentaus_base,agentaus_tuned
$PY ab.py run --suite mmlu_pro --split dev --arms $A --label cdev --jobs 8 --resume
for r in 1 2 3; do $PY ab.py run --suite econ_r --arms $A --label cdev_r$r --jobs 4 --resume; done
$PY ab.py run --suite mmlu --split dev --arms $A --label cdev --jobs 8 --resume
$PY ab.py run --suite he --split dev --arms $A --label cdev --jobs 8 --resume
echo DONE cdev
