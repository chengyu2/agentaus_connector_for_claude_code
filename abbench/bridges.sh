#!/bin/zsh
# (Re)start A/B bridges.  Usage: bridges.sh [baseline|tuned|next|web|all] [KEY=VALUE ...]
#   8795 baseline, web off   8796 tuned, web off     (coding + knowledge suites)
#   8793 baseline, web on    8794 tuned, web on      (data.gov.au task)
# baseline = main @ ceb597c, checked out in $AB_STOCK; tuned = this worktree;
# next = frozen snapshot in $AB_SNAP.
# Restart only what changed: Opus reference runs go through 8795/8793 passthrough.
#
# Every location comes from the environment - none are baked in:
#   AB_PY      interpreter with agentaus_bridge's requirements installed (default python3)
#   AB_STOCK   baseline checkout                    - needed for baseline/web/all
#   AB_SNAP    frozen snapshot checkout             - needed for next
#   AB_LOGS    directory for the bridge logs        - default ./logs (gitignored)
# The tuned bridge always serves this worktree (the directory this script lives in).
cd -- "$(dirname -- "$0")"
HERE=$PWD
PY=${AB_PY:-python3}
LOGS=${AB_LOGS:-$HERE/logs}
STOCK=${AB_STOCK:-}; TUNED=${AB_TUNED:-$HERE/..}
SNAP=${AB_SNAP:-}
mkdir -p "$LOGS"
if ! command -v "$PY" >/dev/null 2>&1 && [ ! -x "$PY" ]; then
  echo "bridges.sh: interpreter not found: $PY - set AB_PY to one with agentaus_bridge's requirements installed" >&2
  exit 1
fi
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
# Check every selected checkout before starting anything. A typo in AB_STOCK/AB_SNAP
# must not launch a partial set of bridges or a process into a nonexistent directory.
live=()
missing=
for spec in $specs; do
  rest=${spec#*:}; dir=${rest%%:*}
  if [ -d "$dir" ]; then live+=("$spec"); else
    echo "bridges.sh: $dir does not exist - set AB_STOCK or AB_SNAP (see header)" >&2
    missing=1
  fi
done
if [ -n "$missing" ]; then exit 1; fi
for spec in $live; do
  port=${spec%%:*}; rest=${spec#*:}; dir=${rest%%:*}; rest=${rest#*:}; name=${rest%%:*}; web=${rest#*:}
  pid=$(lsof -nP -tiTCP:$port -sTCP:LISTEN 2>/dev/null); [ -n "$pid" ] && kill $pid && sleep 1
  # A new session of its own, so stopping whichever job launched it cannot take the
  # bridge down with it - which is how 8795 died mid-benchmark and every run through it
  # scored zero on "Connection refused".
  AGENTAUS_WEB_SEARCH=$web $PY -c "import subprocess, sys; subprocess.Popen([sys.argv[1], '-u', '-m', 'agentaus_bridge', '--port', sys.argv[2]], cwd=sys.argv[3], stdin=subprocess.DEVNULL, stdout=open(sys.argv[4], 'ab'), stderr=subprocess.STDOUT, start_new_session=True)" $PY $port $dir $LOGS/bridge-$name.log
done
for spec in $live; do
  port=${spec%%:*}
  for i in $(seq 1 40); do curl -s -m 1 http://127.0.0.1:$port/healthz >/dev/null && break; sleep 0.25; done
  printf "%s %s\n" $port "$(curl -s -m 2 http://127.0.0.1:$port/healthz | head -c 40)"
done
