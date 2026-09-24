#!/bin/zsh
# Clean dev round: isolated runs, baseline vs tuned (v4).
cd /Users/cheng/agentaus_ab_worktree/abbench
PY=/Users/cheng/agentaus_connector_for_claude_code/.venv/bin/python
A=agentaus_base,agentaus_tuned
$PY ab.py run --suite mmlu_pro --split dev --arms $A --label cdev --jobs 8 --resume
for r in 1 2 3; do $PY ab.py run --suite econ_r --arms $A --label cdev_r$r --jobs 4 --resume; done
$PY ab.py run --suite mmlu --split dev --arms $A --label cdev --jobs 8 --resume
$PY ab.py run --suite he --split dev --arms $A --label cdev --jobs 8 --resume
echo DONE cdev
