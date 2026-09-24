#!/bin/zsh
# Clean Opus reference on the held-out test halves, identical setup to the Agentaus arms.
cd /Users/cheng/agentaus_ab_worktree/abbench
PY=/Users/cheng/agentaus_connector_for_claude_code/.venv/bin/python
for r in 1 2 3; do $PY ab.py run --suite econ_r --arms opus --label ctest_r$r --jobs 2 --resume; done
$PY ab.py run --suite mmlu_pro --split test --arms opus --label ctest --jobs 3 --resume
$PY ab.py run --suite mmlu --split test --arms opus --label ctest --jobs 3 --resume
$PY ab.py run --suite he --split test --arms opus --label ctest --jobs 3 --resume
echo DONE cref
