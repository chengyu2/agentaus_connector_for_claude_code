#!/bin/zsh
# (Re)start A/B bridges.  Usage: bridges.sh [baseline|tuned|web|all] [KEY=VALUE ...]
#   8795 baseline, web off   8796 tuned, web off     (coding + knowledge suites)
#   8793 baseline, web on    8794 tuned, web on      (data.gov.au task)
# baseline = main @ ceb597c in /Users/cheng/agentaus_ab_stock; tuned = this worktree.
# Restart only what changed: Opus reference runs go through 8795/8793 passthrough.
PY=/Users/cheng/agentaus_connector_for_claude_code/.venv/bin/python
LOGS=/Users/cheng/agentaus_ab_worktree/abbench/logs
STOCK=/Users/cheng/agentaus_ab_stock; TUNED=/Users/cheng/agentaus_ab_worktree
SNAP=/Users/cheng/agentaus_ab_snap
which=${1:-all}; shift 2>/dev/null
for kv in "$@"; do export "$kv"; done
specs=()
case $which in
  baseline) specs=("8795:${STOCK}:baseline:false" "8793:${STOCK}:baseline-web:true");;
  tuned)    specs=("8796:${TUNED}:tuned:false" "8794:${TUNED}:tuned-web:true");;
  next)     specs=("8797:${SNAP}:next:false" "8798:${SNAP}:next-web:true");;
  web)      specs=("8793:${STOCK}:baseline-web:true" "8794:${TUNED}:tuned-web:true");;
  all)      specs=("8795:${STOCK}:baseline:false" "8796:${TUNED}:tuned:false"
                   "8793:${STOCK}:baseline-web:true" "8794:${TUNED}:tuned-web:true");;
esac
for spec in $specs; do
  port=${spec%%:*}; rest=${spec#*:}; dir=${rest%%:*}; rest=${rest#*:}; name=${rest%%:*}; web=${rest#*:}
  pid=$(lsof -nP -tiTCP:$port -sTCP:LISTEN 2>/dev/null); [ -n "$pid" ] && kill $pid && sleep 1
  # A new session of its own, so stopping whichever job launched it cannot take the
  # bridge down with it - which is how 8795 died mid-benchmark and every run through it
  # scored zero on "Connection refused".
  AGENTAUS_WEB_SEARCH=$web $PY -c "import subprocess, sys; subprocess.Popen([sys.argv[1], '-u', '-m', 'agentaus_bridge', '--port', sys.argv[2]], cwd=sys.argv[3], stdin=subprocess.DEVNULL, stdout=open(sys.argv[4], 'ab'), stderr=subprocess.STDOUT, start_new_session=True)" $PY $port $dir $LOGS/bridge-$name.log
done
for spec in $specs; do
  port=${spec%%:*}
  for i in $(seq 1 40); do curl -s -m 1 http://127.0.0.1:$port/healthz >/dev/null && break; sleep 0.25; done
  printf "%s %s\n" $port "$(curl -s -m 2 http://127.0.0.1:$port/healthz | head -c 40)"
done
