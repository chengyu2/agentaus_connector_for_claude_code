#!/bin/zsh
# Final: Agentaus baseline vs tuned v4 on the held-out test halves. No Claude usage.
cd /Users/cheng/agentaus_ab_worktree/abbench
PY=/Users/cheng/agentaus_connector_for_claude_code/.venv/bin/python
A=agentaus_base,agentaus_tuned
for r in 1 2 3; do $PY ab.py run --suite econ_r --arms $A --label ctest_r$r --jobs 4 --resume; done
$PY ab.py run --suite mmlu_pro --split test --arms $A --label ctest --jobs 8 --resume
$PY ab.py run --suite mmlu --split test --arms $A --label ctest --jobs 8 --resume
$PY ab.py run --suite he --split test --arms $A --label ctest --jobs 8 --resume
echo DONE ctest_agentaus
