#!/bin/zsh
# v5 (snapshot 1fd1564) on the clean dev splits, beside the v4 round.
cd /Users/cheng/agentaus_ab_worktree/abbench
PY=/Users/cheng/agentaus_connector_for_claude_code/.venv/bin/python
$PY ab.py run --suite he --split dev --arms agentaus_next --label cdev --jobs 5 --resume
$PY ab.py run --suite mmlu_pro --split dev --arms agentaus_next --label cdev --jobs 5 --resume
for r in 1 2 3; do $PY ab.py run --suite econ_r --arms agentaus_next --label cdev_r$r --jobs 2 --resume; done
$PY ab.py run --suite mmlu --split dev --arms agentaus_next --label cdev --jobs 5 --resume
echo DONE cnext
