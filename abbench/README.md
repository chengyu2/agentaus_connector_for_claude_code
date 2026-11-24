# abbench - the Claude Code A/B harness

Headless Claude Code sessions driven through the bridge, graded with no model in the
loop: `ab.py` runs and reports, the `c*.sh` scripts are canned rounds, `bridges.sh`
starts the bridges the arms connect to, `summarise.py` aggregates runs into one JSON
summary, and `skill_use.py` / `stream_probe.py` are small probes.

Nothing in here hardcodes a machine's paths. Everything outside this repository comes
from the environment; defaults exist only where one is safe (a directory this checkout
owns, or one derived from `$HOME`).

## Environment

| Variable | Used by | Meaning | Default |
| --- | --- | --- | --- |
| `AB_RUNS` | `ab.py`, `summarise.py`, `skill_use.py` | where run artefacts (transcripts, per-run JSON) live. Must be **outside every git repository** - inside one, Claude Code treats each run directory as that repo's project and loads its skills and memory. | `~/agentaus_ab_runs` |
| `AB_PEER_DATA` | `ab.py` (econ_r suite) | read-only `agentaus-bench` data directory; files are copied into each run, the original is never touched | required for `econ_r` - no default |
| `AB_CLAUDE_CODE` | `ab.py`, `skill_use.py`, `stream_probe.py` | the Claude Code binary to drive | the newest binary under `~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude` |
| `AB_PY` | all `.sh` scripts | interpreter with `agentaus_bridge`'s requirements installed, used to run the harness and the bridges (the graders run under whichever interpreter runs `ab.py`) | `python3` |
| `AB_STOCK` | `bridges.sh` | baseline checkout (main @ ceb597c) serving bridges 8795/8793 | required for `baseline`/`web`/`all` - no default |
| `AB_SNAP` | `bridges.sh` | frozen snapshot checkout for the `next` arms (8797/8798) | required for `next` - no default |
| `AB_TUNED` | `bridges.sh` | checkout to serve the tuned arms (8796/8794) from | this checkout (`..` from the script) |
| `AB_LOGS` | `bridges.sh` | directory for the bridge logs | `./logs` (gitignored) |
| `AB_PATH` | `ab.py` | `PATH` given to each headless session, mirroring the VS Code extension's environment | the VS Code default on macOS |
| `AB_FIXTURE_SKILLS` | `skill_use.py` | the `_fixture_skills` directory its `small` tasks copy | beside the runs root |

Benchmark data downloaded for the suites lives in `abbench/data/` (gitignored;
`HumanEvalPlus.jsonl.gz`, `mmlu.json`, `mmlu_pro.json`) - fetch it once per checkout.

## A typical setup on a new machine

```bash
export AB_PY="$HOME/agentaus_connector_for_claude_code/.venv/bin/python"
export AB_STOCK="$HOME/agentaus_ab_stock"        # main @ ceb597c, for the baseline arm
export AB_PEER_DATA="$HOME/agentaus-bench/data"  # econ_r ground truth + macro data

abbench/bridges.sh all     # 8795 baseline, 8796 tuned (this checkout), 8793/8794 web on
abbench/cdev.sh            # dev round: agentaus_base vs agentaus_tuned
abbench/ctest_agentaus.sh  # held-out halves
python abbench/summarise.py ctest ctest_r1 ctest_r2 ctest_r3 > abbench/results/ctest.json
```

`cdev.sh`, `cnext.sh`, `cref.sh` and `ctest_agentaus.sh` change to their own directory
first, so they work from any checkout they are committed in.

## What is and is not portable

- The tuned bridge always serves **this checkout** (wherever `bridges.sh` lives); the
  baseline and snapshot checkouts must be named explicitly via `AB_STOCK` / `AB_SNAP`,
  because guessing them is how a run would silently measure the wrong code.
- Run artefacts default to `~/agentaus_ab_runs`, which is outside every git repository
  on any machine. Point `AB_RUNS` somewhere persistent if the home directory is not.
- Committed results under `abbench/results/` and `abbench/report/` are history; rerunning
  on a different machine writes to `AB_RUNS`, not over them.
