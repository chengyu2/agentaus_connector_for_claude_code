#!/usr/bin/env python3
"""Benchmark loop for ultra.py: a round that ends with N valid runs per task, or says why not.

    loop.py run --label d15 --tasks c1913h,c1913,he10,tender,rdti --valid 3 --manage-bridge
    loop.py report --labels d15,d16
    loop.py regrade --labels d1,d2,...      # every run, current graders against HEAD's

`run` gates every Agentaus launch on the bridge's /healthz/upstream and a realistic streamed
probe, watches upstream health while runs go, aborts a run whose last 15 minutes are an
outage, voids runs whose window degraded and reruns them, and appends every attempt to an
append-only ledger (RUNS/ledger.jsonl) with the bridge's loaded-source hash, the upstream's
health over the run, the score, each check and the verdict. An Opus arm runs every round.

Measured on d1-d14, the reasons each piece exists:
  - outage runs scored 0 and were counted as real (11 of them: d8, d11, d13);
  - a run launched into an outage burned up to 7200 s - about 9.5 h in all;
  - five runs died on their first request after 62 min (Claude Code re-sent it 9 times);
  - a previous round's bridge stayed alive and kept calling Agentaus for clients long gone;
  - records carried no start time or code version, so a score could not be tied to its code;
  - "best run" status read sd 0.10-0.23 tasks as progress, against bars that were not Opus.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import statistics as st
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
import ultra  # noqa: E402

RUNS = ultra.RUNS
LEDGER = RUNS / "ledger.jsonl"
PROTECTED_PORTS = (8787,)    # the everyday bridge: never stopped, drained or counted as a stray

# --------------------------------------------------------------------------------- rules

TICK_S = 30
BUCKET_S = 600               # /healthz/upstream counts the trailing 10 minutes
# Abort (mid-run): an outage, not a slump. Measured (d8 tender): a run at a 14 % failure
# ratio still scored 1.0, so a slump is survived and only reported.
ABORT_AFTER_S = 900          # the last 15 minutes: samples whose 10-minute window lies in
ABORT_SPAN_S = 300           # the run, all severe for 5 minutes
ABORT_MIN_FAIL = 3
SEVERE_FAIL_RATIO = 0.5
SEVERE_OK_PER_MIN = 2.0
# Throttle limit at or under which failures count against the upstream. Above it the bridge
# was still running wide, and 524s can be the harness's own load (throttle.py: past about 8
# concurrent sessions), which is the thing under test, not an outage.
OUTAGE_LIMIT = 2
# Void (after the run): calibrated on d1-d13 - every run later judged valid had no degraded
# 10-minute bucket; every outage run had >= 31 % of them.
BUCKET_BAD = 0.25
VOID_DEGRADED_FRAC = 0.2
VOID_DOWN_S = 300            # the bridge reported Agentaus down this long during the run
SURVIVED_FAIL_RATIO = 0.05   # valid, but reported as "survived degraded"
FIRST_RESPONSE_S = 900       # no assistant line by then: void:no-response
EXTEND_MAX_S = 3600          # the run's timeout grows by the time Agentaus was down, up to this

# The old status bars (secondary). Per the README, rdti 0.61 and he10 0.80 are own-workflow
# means and the other three match neither Opus nor the README; the primary target is the
# rolling Opus mean.
OLD_BARS = {"rdti": 0.61, "tender": 0.775, "c1913": 0.875, "he10": 0.8, "c1913h": 0.5}
# Ended for a reason that is not the model's. Not "Request timed out": where health was bad
# the health rule voids the run, and where it was fine a hang is the bridge's own, which is
# what is being measured.
INFRA = ultra.INFRA


def log(msg: str) -> None:
    print(f"{time.strftime('%m-%d %H:%M:%S')} {msg}", flush=True)


# --------------------------------------------------------------------------------- bridge

class BridgeClient:
    """The bridge's HTTP surface: /healthz, /healthz/upstream, a probe, and a drain."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _get(self, path: str, timeout: float = 10):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=timeout) as r:
            return json.load(r)

    def alive(self) -> bool:
        try:
            self._get("/healthz", 5)
            return True
        except Exception:  # noqa: BLE001
            return False

    def upstream(self) -> dict | None:
        """The /healthz/upstream contract, or None when it cannot be read."""
        try:
            up = self._get("/healthz/upstream")
            return up if isinstance(up, dict) and "ok_10m" in up else None
        except Exception:  # noqa: BLE001
            return None

    def drain(self) -> bool:
        """Ask the bridge to cancel requests and flights with no client left (only a bridge
        that has the endpoint does)."""
        req = urllib.request.Request(f"{self.base}/admin/drain", data=b"{}", method="POST",
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def probe(self, concurrency: int = 2, limit_s: float = 150) -> bool:
        """`concurrency` realistic streamed requests at once; healthy only if every one ends
        in a real answer within `limit_s`. Measured (d14): the watcher's 10 KB, 60-token,
        non-streaming probe is not what a graph agent sends (bridge logs: bytes=87223).
        Measured (10-23, healthy upstream): this probe took 64 s - Agentaus ignores max_tokens
        and wrote 12k completion tokens - so the limit leaves room for an ordinary slow turn."""
        results = [False] * concurrency
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
        if os.environ.get("BRIDGE_TOKEN"):
            headers["x-api-key"] = os.environ["BRIDGE_TOKEN"]

        def one(i):
            req = urllib.request.Request(f"{self.base}/v1/messages", headers=headers,
                                         data=json.dumps(realistic_body(seed=i)).encode())
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=limit_s + 30) as r:
                    body = r.read().decode(errors="replace")
                    status = r.status
                text = "".join(re.findall(r'"text_delta",\s*"text":\s*"((?:[^"\\]|\\.)*)"', body))
                # A bridge's synthetic "(out of time)" reply is not an answer.
                results[i] = (status == 200 and "message_stop" in body and len(text.strip()) >= 20
                              and time.time() - t0 < limit_s)
            except Exception:  # noqa: BLE001
                results[i] = False

        threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(limit_s + 40)
        return all(results)


def realistic_body(kb: int = 80, seed: int = 0) -> dict:
    """About the size of a real graph agent's request, streamed, asking for a few hundred
    tokens of work."""
    rnd = random.Random(seed)
    lines, size = [], 0
    while size < kb * 1024:
        line = f"def f{len(lines)}(x):\n    return x * {rnd.randint(1, 99)} + {rnd.randint(1, 99)}  # step {len(lines)}\n"
        lines.append(line)
        size += len(line)
    return {"model": ultra.ARMS["agentaus"], "max_tokens": 600, "stream": True,
            "messages": [{"role": "user", "content": "".join(lines) +
                          "\nList the ten functions above with the largest multiplier, one per line, "
                          "and explain in three sentences how you chose them."}]}


def endpoint_healthy(up: dict | None) -> bool:
    if not up or up.get("down"):
        return False
    seen = (up.get("ok_10m") or 0) + (up.get("fail_10m") or 0)
    return seen < 4 or (up.get("ok_rate_10m") or 0) >= 0.75


class Gate:
    """Launch only on fresh evidence: the bridge's own 10-minute counts and a realistic probe.
    A cold start (nothing of Agentaus running, or a fresh bridge) needs `need_cold` passes in
    a row; a launch beside a running run needs one. Step-based, so runs stay monitored while
    the gate waits through an outage."""

    def __init__(self, client: BridgeClient, need_cold: int = 3, gap_ok: float = 120,
                 gap_bad: float = 120, concurrency: int = 2, clock=time.time, say=log,
                 gap_max: float = 1800, jitter=None):
        self.client, self.need_cold, self.gap_ok, self.gap_bad = client, need_cold, gap_ok, gap_bad
        self.concurrency, self.clock, self.say, self.gap_max = concurrency, clock, say, gap_max
        self.jitter = jitter or (lambda: random.uniform(0.85, 1.15))
        self.good, self.next_at, self.fails = 0, 0.0, 0

    def ready(self, cold: bool) -> bool:
        need = self.need_cold if cold else 1
        if self.good >= need:
            return True
        if self.clock() < self.next_at:
            return False
        up = self.client.upstream()
        ep = endpoint_healthy(up)
        # Beside a running run, the bridge's own live counts are the evidence: a probe queues
        # behind that run's agents for a slot and times out on a healthy upstream. Measured
        # (d15): 300 ok and 1 failure in 10 minutes, and every probe failed, so one job ran.
        live = not cold and up and (up.get("ok_10m") or 0) >= 20 and (up.get("ok_rate_10m") or 0) >= 0.9
        # While the bridge says down, a probe is still sent: a success is the only thing that clears
        # "down". Measured (d15): the gate skipped the probe whenever the endpoint said down, nothing
        # reached Agentaus, and a recovered upstream sat "down" for 19 hours.
        down = bool(up and up.get("down"))
        ok = (ep and (live or self.client.probe(self.concurrency))) or (down and self.client.probe(1)
                                                                         and endpoint_healthy(self.client.upstream()))
        self.good = self.good + 1 if ok else 0
        # Backing off: each failed check in a row doubles the wait, up to gap_max, with jitter; a
        # pass resets it. An outage that lasts all night is checked a few times an hour, not
        # every few minutes, and a recovery is still seen within the cap.
        self.fails = 0 if ok else self.fails + 1
        wait = self.gap_ok if ok else min(self.gap_max, self.gap_bad * 2 ** (self.fails - 1)) * self.jitter()
        self.next_at = self.clock() + wait
        brief = {k: up.get(k) for k in ("ok_10m", "fail_10m", "limit", "down")} if up else "unreadable"
        self.say(f"gate: upstream={brief} endpoint_ok={ep} probe_ok={ok} good={self.good}/{need}"
                 + ("" if ok else f" - next check in {wait / 60:.0f} min"))
        return self.good >= need

    def consume(self) -> None:
        self.good = 0

    def reset(self) -> None:
        self.good, self.next_at, self.fails = 0, 0.0, 0


def _pids_listening(port: int) -> set[int]:
    r = subprocess.run(["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True)
    return {int(x) for x in r.stdout.split()}


def _bridge_processes() -> list[tuple[int, str]]:
    r = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if "-m agentaus_bridge" in cmd and pid.isdigit() and int(pid) != os.getpid():
            out.append((int(pid), cmd))
    return out


def find_strays(port: int, processes=None, listening=None) -> list[int]:
    """Bridges started for `port` that are no longer its listener. Measured (H15): the d13
    bridge was alive at 00:50, not listening, still retrying about 11 requests upstream for
    clients long gone - load during an outage, and traffic missing from the new log's health.
    Only processes whose command line names this port count, and never a listener on a
    protected port (8787's bridge is someone's working session)."""
    processes = _bridge_processes() if processes is None else processes
    listening = (lambda p: _pids_listening(p)) if listening is None else listening
    ours = listening(port)
    protected = set().union(*(listening(p) for p in PROTECTED_PORTS)) if PROTECTED_PORTS else set()
    flag = re.compile(rf"--port[= ]{port}\b")
    return [pid for pid, cmd in processes
            if flag.search(cmd) and pid not in ours and pid not in protected]


def _kill(pid: int) -> None:
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        end = time.time() + wait
        while time.time() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.5)


class Bridge:
    """Exactly one bridge on `port`. With `manage`, the loop owns it: it starts it, logs it to
    a file, and restarts it between rounds and after a run that timed out or was aborted, so
    no zombie requests from a dead client hold the throttle. Without it, the loop only drains
    (when the bridge has /admin/drain) and refuses to run beside a stray."""

    def __init__(self, port: int, manage: bool, log_path: Path | None, python: str,
                 width: int, strays: str = "kill", client: BridgeClient | None = None):
        if port in PROTECTED_PORTS:
            raise SystemExit(f"port {port} is protected: the loop never runs on or restarts it")
        self.port, self.manage, self.log_path, self.python, self.width = port, manage, log_path, python, width
        self.strays = strays
        self.client = client or BridgeClient(f"http://127.0.0.1:{port}")
        self.loaded: str | None = None  # disk_source_hash() when this loop last started it

    def check_strays(self) -> None:
        strays = find_strays(self.port)
        if not strays:
            return
        if self.strays == "refuse":
            raise SystemExit(f"stray bridge process(es) for port {self.port}: {strays} - stop them "
                             "or run with --strays kill")
        for pid in strays:
            log(f"killing stray bridge {pid} (started for port {self.port}, not its listener)")
            _kill(pid)

    def restart(self) -> None:
        self.check_strays()
        if not self.manage:
            log(f"bridge drain: {'done' if self.client.drain() else 'no /admin/drain on this bridge'}")
            return
        for pid in _pids_listening(self.port):
            if pid not in set().union(*(_pids_listening(p) for p in PROTECTED_PORTS)):
                log(f"stopping bridge {pid} on {self.port}")
                _kill(pid)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC_BASE_URL"))}
        env["AGENTAUS_GRAPH_WIDTH"] = str(self.width)
        self.loaded = disk_source_hash()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a") as fh:
            fh.write(f"\n==== loop.py started this bridge at {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
            fh.flush()
            subprocess.Popen([self.python, "-u", "-m", "agentaus_bridge", "--port", str(self.port)],
                             cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        end = time.time() + 120
        while time.time() < end and not self.client.alive():
            time.sleep(1)
        if not self.client.alive():
            raise SystemExit(f"bridge on {self.port} did not come up; see {self.log_path}")
        up = self.client.upstream()
        log(f"bridge up on {self.port} (width {self.width}, log {self.log_path}, "
            f"source_hash {(up or {}).get('source_hash', '?')})")


# --------------------------------------------------------------------------------- health

def severe(u: dict) -> bool:
    """One sample's trailing 10 minutes look like an outage."""
    if u.get("down"):
        return True
    ok, fail = u.get("ok_10m") or 0, u.get("fail_10m") or 0
    if (u.get("limit") or 0) > OUTAGE_LIMIT or fail < ABORT_MIN_FAIL:
        return False
    return fail / max(1, ok + fail) >= SEVERE_FAIL_RATIO or ok < SEVERE_OK_PER_MIN * BUCKET_S / 60


def abort_now(samples: list, now: float, started: float) -> bool:
    """Abort when every sample of the last 5 minutes is severe and its 10-minute window lies
    inside this run - the last 15 minutes in all. Only the run's own window: right after an
    outage the bridge's counts still hold its failures, and judging a fresh run by them would
    abort every relaunch (H14)."""
    if now - started < ABORT_AFTER_S:
        return False
    lo = max(now - ABORT_SPAN_S, started + BUCKET_S)
    recent = [(t, u) for t, u in samples if lo <= t <= now and u]
    if len(recent) < 3 or recent[0][0] > lo + 2 * TICK_S:
        return False
    return all(severe(u) for _, u in recent)


def summarise(samples: list, start: float, end: float) -> dict:
    """Upstream health over [start, end] from /healthz/upstream samples: one 10-minute bucket
    per sample at each bucket's end, so the bucket counts are the endpoint's own."""
    win = [(t, u) for t, u in samples if start <= t <= end + TICK_S and u]
    if not win:
        return {"known": False}
    n = max(1, math.ceil((end - start) / BUCKET_S))
    ok_tot = fail_tot = n524 = 0.0
    degraded = known = severe_n = 0
    limits = [u.get("limit") for _, u in win if u.get("limit") is not None]
    for k in range(n):
        b0, b1 = start + k * BUCKET_S, min(end, start + (k + 1) * BUCKET_S)
        inb = [(t, u) for t, u in win if b0 < t <= b1 + TICK_S]
        if not inb:
            continue
        known += 1
        _, last = min(inb, key=lambda x: abs(x[0] - b1))
        frac = (b1 - b0) / BUCKET_S
        ok, fail = last.get("ok_10m") or 0, last.get("fail_10m") or 0
        ok_tot += ok * frac
        fail_tot += fail * frac
        n524 += (last.get("n524_10m") or 0) * frac
        lim = st.median([u.get("limit") or 0 for _, u in inb])
        down_frac = sum(bool(u.get("down")) for _, u in inb) / len(inb)
        bad = down_frac >= 0.5 or (lim <= OUTAGE_LIMIT and fail >= 2 and fail / max(1, ok + fail) >= BUCKET_BAD)
        degraded += bad
        severe_n += any(severe(u) for _, u in inb)
    down_s, prev = 0.0, None
    for _, u in win:
        cur = u.get("down_total_s")
        if cur is not None and prev is not None and cur > prev:
            down_s += cur - prev  # a restarted bridge starts again from 0: only rises count
        prev = cur if cur is not None else prev
    hashes = sorted({u.get("source_hash") for _, u in win if u.get("source_hash")})
    return {"known": True, "ok": round(ok_tot), "fail": round(fail_tot), "n524": round(n524),
            "fail_ratio": round(fail_tot / max(1.0, ok_tot + fail_tot), 3),
            "ok_per_min": round(ok_tot / max(1.0, (end - start) / 60), 2),
            "degraded_frac": round(degraded / max(1, known), 2), "severe_buckets": severe_n,
            "down_s": round(down_s), "limit_min": min(limits) if limits else None,
            "limit_median": st.median(limits) if limits else None, "source_hashes": hashes}


def classify(arm: str, result_text: str, h: dict, aborted: str | None) -> str:
    if aborted:
        return aborted
    if INFRA.search((result_text or "")[:300]):
        return "void:infra"
    if arm == "agentaus" and h.get("known") and (h["down_s"] >= VOID_DOWN_S
                                                  or h["degraded_frac"] >= VOID_DEGRADED_FRAC):
        return "void:upstream"
    return "valid"


def survived_degraded(arm: str, h: dict, aborted: str | None) -> bool:
    """Completed although the upstream degraded under it: kept (if valid) and reported."""
    return (arm == "agentaus" and not aborted and bool(h.get("known"))
            and (h["fail_ratio"] >= SURVIVED_FAIL_RATIO or h["degraded_frac"] > 0 or h["down_s"] > 0))


# --------------------------------------------------------------------------------- versions

def graders_hash() -> str:
    data = b"".join((HERE / f).read_bytes() for f in sorted(os.listdir(HERE)) if f.startswith("grade_"))
    return hashlib.sha1(data).hexdigest()[:10]


def disk_source_hash() -> str:
    """Fallback only, for a bridge that serves no source_hash: what is on disk now, which is
    not necessarily what the running bridge loaded."""
    h = hashlib.sha1()
    for p in sorted(list((REPO / "agentaus_bridge").glob("*.py")) + list((REPO / "agentaus_bridge" / "graphs").rglob("*"))):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(p.read_bytes())
    return "disk:" + h.hexdigest()[:10]


def bridge_committed() -> bool:
    """No uncommitted change under agentaus_bridge/: a commit is what deploys new code to a
    running round, so a half-made edit is never loaded."""
    try:
        return subprocess.run(["git", "status", "--porcelain", "--", "agentaus_bridge"], cwd=REPO,
                              capture_output=True, text=True, timeout=30).stdout.strip() == ""
    except (OSError, subprocess.SubprocessError):
        return False


def git_version() -> dict:
    def git(*a):
        return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True).stdout
    diff = git("diff", "HEAD", "--", "agentaus_bridge", "abbench/ultra")
    return {"commit": git("rev-parse", "--short", "HEAD").strip(),
            "dirty": hashlib.sha1(diff.encode()).hexdigest()[:10] if diff else ""}


# --------------------------------------------------------------------------------- ledger

_ledger_lock = threading.Lock()


def append_ledger(row: dict, path: Path) -> None:
    with _ledger_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(row) + "\n")


def read_ledger(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue  # a torn last line from a killed loop
    return rows


# --------------------------------------------------------------------------------- statistics

def boot_ci(xs: list, n: int = 4000, seed: int = 1) -> tuple:
    """Bootstrap interval of the mean. At n=3-5 on these bimodal scores it is too narrow:
    read it as indicative (audit: N >= 8 for c1913 and he10 before calling a change)."""
    if not xs:
        return (None, None)
    if len(xs) < 2:
        return (xs[0], xs[0])
    rnd = random.Random(seed)
    ms = sorted(st.mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


def p_at_least(xs: list, bar: float, n: int = 4000, seed: int = 2) -> float:
    if not xs or bar is None:
        return float("nan")
    rnd = random.Random(seed)
    return sum(st.mean(rnd.choices(xs, k=len(xs))) >= bar for _ in range(n)) / n


def opus_reference(rows: list, task: str, graders: str | None, at: float, window_h: float = 96) -> tuple:
    """The rolling Opus mean for `task`: valid Opus runs in the `window_h` hours before `at`,
    graded by the same graders when there are any (marked `*` otherwise)."""
    opus = [r for r in rows if r.get("arm") == "opus" and r.get("task") == task and r.get("verdict") == "valid"
            and r.get("score") is not None and at - (r.get("ended_at") or 0) < window_h * 3600]
    same = [r for r in opus if graders is None or r.get("graders") == graders]
    use = same or opus
    xs = [r["score"] for r in use]
    return (st.mean(xs) if xs else None, len(xs), bool(use) and not same)


# --------------------------------------------------------------------------------- the loop

class Run:
    def __init__(self, arm, task, variant, attempt, rep, wd, proc, started, source_hash):
        self.arm, self.task, self.variant, self.attempt, self.rep = arm, task, variant, attempt, rep
        self.wd, self.proc, self.started, self.source_hash = wd, proc, started, source_hash
        self.down_at_start = None
        self.responded = False


def has_response(transcript: Path) -> bool:
    try:
        with open(transcript, errors="replace") as fh:
            return any('"type":"assistant"' in line for line in fh)
    except OSError:
        return False


class Loop:
    def __init__(self, args, bridge: Bridge | None = None, client: BridgeClient | None = None,
                 gate: Gate | None = None, clock=time.time, sleep=time.sleep, pool=None,
                 launcher=None, killer=None, grader=None, ledger: Path | None = None):
        self.a = args
        self.client = client or BridgeClient(args.bridge)
        self.bridge = bridge
        self.clock, self.sleep = clock, sleep
        self.gate = gate or Gate(self.client, need_cold=args.gate_passes, concurrency=args.probe_concurrency,
                                 clock=clock)
        self.pool = pool or cf.ThreadPoolExecutor(max_workers=2)
        self.launcher = launcher or self._launch
        self.killer = killer or ultra.kill_session
        self.grader = grader or ultra.safe_grade
        self.ledger = Path(ledger or args.ledger)
        self.samples: list = []
        self.running: list[Run] = []
        self.grading: dict = {}
        self.pending_restart = False
        self.reload = False
        self.disk_hash = disk_source_hash
        self.committed = bridge_committed
        self.last_alive = clock()
        self.graders = graders_hash()
        self.git = git_version()
        self.tasks = args.tasks.split(",")
        per = dict(kv.split("=") for kv in args.valid_per_task.split(",") if kv) if args.valid_per_task else {}
        self.need = {t: int(per.get(t, args.valid)) for t in self.tasks}
        self.opus = self._opus_this_round()
        self.stopped: dict = {}

    # -- planning

    def _rows(self):
        return read_ledger(self.ledger)

    def _opus_this_round(self) -> bool:
        if self.a.opus_every <= 0:
            return False
        rows = self._rows()
        labels = []
        for r in sorted(rows, key=lambda r: r.get("started_at") or 0):
            if r["label"] not in labels and r["label"] != self.a.label:
                labels.append(r["label"])
        with_opus = [l for l in labels if any(r["label"] == l and r["arm"] == "opus" and r["verdict"] == "valid"
                                              and r.get("graders") == self.graders for r in rows)]
        if not with_opus:
            return True  # no Opus reference under these graders yet
        return len(labels) - 1 - labels.index(with_opus[-1]) >= self.a.opus_every - 1

    def _version(self) -> str:
        up = self.client.upstream()
        return (up or {}).get("source_hash") or disk_source_hash()

    def _valid(self, rows, arm, task, version):
        return [r for r in rows if r["arm"] == arm and r["task"] == task and r["verdict"] == "valid"
                and r.get("graders") == self.graders and r.get("score") is not None
                and (r["label"] == self.a.label if arm == "opus" else r.get("source_hash") == version)]

    def _target(self, rows, task):
        ref, n, _ = opus_reference(rows, task, self.graders, self.clock())
        return ref if ref is not None else OLD_BARS.get(task)

    def plan(self) -> list[tuple]:
        """What to launch next, most useful first: tasks short of a first estimate, then the
        rest, each until it has its N valid runs, or its interval clears the target."""
        rows = self._rows()
        version = self._version()
        inflight = [(r.arm, r.task) for r in self.running] + list(self.grading.values())
        out = []
        for arm in ["agentaus"] + (["opus"] if self.opus else []):
            for task in self.tasks:
                need = self.need[task] if arm == "agentaus" else self.a.opus_valid
                valid = self._valid(rows, arm, task, version)
                n = len(valid) + sum(1 for k in inflight if k == (arm, task))
                attempts = sum(1 for r in rows if r["label"] == self.a.label and r["arm"] == arm and r["task"] == task)
                attempts += sum(1 for k in inflight if k == (arm, task))
                if n >= need or attempts >= need * self.a.max_attempt_factor:
                    continue
                if arm == "agentaus" and len(valid) >= self.a.min_valid:
                    target = self._target(rows, task)
                    lo, hi = boot_ci([r["score"] for r in valid])
                    if target is not None and (lo > target or hi < target):
                        if task not in self.stopped:
                            self.stopped[task] = (len(valid), round(lo, 3), round(hi, 3), round(target, 3))
                            log(f"{task}: interval [{lo:.2f}, {hi:.2f}] clears target {target:.2f} "
                                f"after {len(valid)} valid runs - no more reps")
                        continue
                out.append(((n >= self.a.min_valid, n, attempts), arm, task, attempts + 1, len(valid) + 1))
        out.sort(key=lambda x: x[0])
        return [x[1:] for x in out]

    # -- running

    def _launch(self, arm, task, attempt):
        wd = RUNS / self.a.label / f"{arm}-{task}-{self.a.variant}-a{attempt}"
        shutil.rmtree(wd, ignore_errors=True)  # only this round's own attempt dir
        shutil.copytree(ultra.FIXTURES / task, wd)
        proc = ultra.launch(ultra.session_cmd(arm, self.a.port), wd, ultra.clean_env(self.a.port),
                            ultra.prompt_for(task, self.a.variant), wd.with_suffix(".jsonl"))
        return proc, wd

    def sample(self):
        up = self.client.upstream()
        if up is not None or self.client.alive():
            self.last_alive = self.clock()
        self.samples.append((self.clock(), up))
        cut = self.clock() - 6 * 3600 - self.a.timeout - EXTEND_MAX_S
        while self.samples and self.samples[0][0] < cut:
            self.samples.pop(0)
        return up

    def _down_total(self, since: float | None = None):
        for t, u in reversed(self.samples):
            if u and u.get("down_total_s") is not None and (since is None or t >= since):
                return u["down_total_s"]
        return None

    def check_runs(self):
        now = self.clock()
        for run in list(self.running):
            down_now = self._down_total()
            extra = 0.0
            if run.down_at_start is not None and down_now is not None and down_now > run.down_at_start:
                extra = min(EXTEND_MAX_S, down_now - run.down_at_start)
            over = now - run.started > self.a.timeout + extra
            if not run.responded and now - run.started > self.a.first_response_s:
                run.responded = has_response(run.wd.with_suffix(".jsonl"))
                no_response = not run.responded
            else:
                no_response = False
            reason = None
            if run.arm == "agentaus" and abort_now(self.samples, now, run.started):
                reason = "void:aborted-upstream"
            elif no_response:
                reason = "void:no-response"   # H16: the first request re-sent until it died
            elif self._bridge_dead(now, run):
                reason = "void:bridge-down"
            done = run.proc.poll() is not None
            if not (done or over or reason):
                continue
            # Only a run still going when the rule fired is aborted: one that exited by
            # itself in the same tick ended on its own and is judged on its window.
            aborted = reason if not done else None
            timed_out = over and not done and not aborted
            self.killer(run.proc, run.wd)
            ended = self.clock()
            self.running.remove(run)
            snapshot = list(self.samples)
            fut = self.pool.submit(self.finish, run, ended, timed_out, aborted, snapshot)
            self.grading[fut] = (run.arm, run.task)
            if timed_out or aborted:
                self.pending_restart = True

    def _bridge_dead(self, now, run) -> bool:
        """Nothing answered on the bridge's port for 5 minutes of this run."""
        return now - max(run.started, self.last_alive) > 300

    def finish(self, run: Run, ended: float, timed_out: bool, aborted: str | None, samples: list) -> dict:
        transcript = run.wd.with_suffix(".jsonl")
        try:
            lines = transcript.read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        stats = ultra.transcript_stats(lines)
        grade = self.grader(run.task, run.wd)
        h = summarise(samples, run.started, ended)
        verdict = classify(run.arm, stats.get("result_text") or "", h, aborted)
        if verdict == "valid" and grade.get("score") is None:
            verdict = "void:grader"   # kept on disk; `regrade` can recover it
        up_end = next((u for t, u in reversed(samples) if u), None) or {}
        rec = {"label": self.a.label, "arm": run.arm, "task": run.task, "variant": self.a.variant,
               "rep": run.rep, "attempt": run.attempt, "wd": str(run.wd),
               "started_at": round(run.started, 1), "ended_at": round(ended, 1),
               "wall_s": round(ended - run.started, 1), "timed_out": timed_out, "aborted": aborted,
               "verdict": verdict, "survived_degraded": survived_degraded(run.arm, h, aborted),
               "score": grade.get("score"), "checks": grade, "health": h,
               "source_hash": run.source_hash, "source_hash_end": up_end.get("source_hash"),
               "graders": self.graders, **self.git, "width": self.a.graph_width, "jobs": self.a.jobs,
               "first_response": has_response(transcript), "num_turns": stats.get("num_turns"),
               "workflow_calls": stats.get("workflow_calls"), "result_head": (stats.get("result_text") or "")[:200]}
        try:
            run.wd.with_suffix(".json").write_text(json.dumps({**rec, **stats, "grade": grade}, indent=1))
        except OSError:
            pass
        append_ledger(rec, self.ledger)
        return rec

    def collect(self):
        for fut in [f for f in self.grading if f.done()]:
            arm, task = self.grading.pop(fut)
            try:
                rec = fut.result()
            except Exception as exc:  # noqa: BLE001 - the ledger write itself failed
                log(f"{arm} {task}: finishing failed: {exc!r}")
                continue
            if rec["verdict"].startswith("void:") and rec["verdict"] != "void:grader":
                self.pending_restart = True  # zombie requests from a slump stay queued in the bridge
            h = rec["health"]
            log(f"[{rec['label']}] {arm} {task} a{rec['attempt']} score={rec['score']} {rec['verdict']}"
                f"{' SURVIVED-DEGRADED' if rec['survived_degraded'] else ''}{' TIMEOUT' if rec['timed_out'] else ''}"
                f" wall={rec['wall_s']:.0f}s health=ok {h.get('ok')}/fail {h.get('fail')} "
                f"degraded {h.get('degraded_frac')} down {h.get('down_s')}s")

    def check_reload(self):
        """New committed code on disk: launch nothing more, and restart the bridge on it once
        the running runs finish - so a fix reaches the next runs, not the next round. Valid runs
        count per source hash, so the new code collects its own. Measured (d15): the judges were
        committed at 21:58 and three runs launched after it on the 20:14 bridge's old graph."""
        b = self.bridge
        if self.reload or not (b and getattr(b, "manage", False) and getattr(b, "loaded", None)) or self.disk_hash() == b.loaded or not self.committed():
            return
        self.reload = self.pending_restart = True
        log(f"new committed code on disk (git {git_version().get('commit')}): no new launches; "
            f"the bridge restarts on it when the {len(self.running)} running run(s) finish")

    def run(self):
        if self.bridge:
            self.bridge.restart()   # between rounds: a fresh bridge, no zombies from the last
        self.gate.reset()
        if self.client.alive() and self.client.upstream() is None:
            log("WARNING: the bridge serves no /healthz/upstream - the gate cannot pass and health "
                "is unknown; run a bridge that has it")
        log(f"round {self.a.label}: tasks {self.need}, opus {'on' if self.opus else 'off'} "
            f"({self.a.opus_valid} valid), graders {self.graders}")
        while True:
            self.sample()
            self.check_runs()
            self.collect()
            self.check_reload()
            if self.pending_restart and not self.running and not self.grading and not (self.reload and not self.committed()):
                if self.bridge:
                    self.bridge.restart()
                self.gate.reset()
                if self.reload:
                    self.git = git_version()
                self.pending_restart = self.reload = False
            todo = self.plan()
            if not todo and not self.running and not self.grading:
                break
            if todo and not self.pending_restart and len(self.running) < self.a.jobs:
                arm, task, attempt, rep = todo[0]
                if arm == "agentaus":
                    cold = not any(r.arm == "agentaus" for r in self.running)
                    if not self.gate.ready(cold):
                        # An Opus run does not need the gate: start one meanwhile if any.
                        others = [x for x in todo if x[0] == "opus"]
                        if not others:
                            self.sleep(TICK_S)
                            continue
                        arm, task, attempt, rep = others[0]
                    else:
                        self.gate.consume()
                version = self._version()
                proc, wd = self.launcher(arm, task, attempt)
                run = Run(arm, task, self.a.variant, attempt, rep, Path(wd), proc, self.clock(), version)
                run.down_at_start = self._down_total()
                self.running.append(run)
                log(f"launched {arm} {task} a{attempt} (rep {rep}) on {version}")
                continue
            self.sleep(TICK_S)
        self.pool.shutdown(wait=True)
        self.collect()
        rows = [r for r in self._rows() if r["label"] == self.a.label]
        log(f"round {self.a.label} done: {sum(r['verdict'] == 'valid' for r in rows)} valid of {len(rows)} attempts")
        print(report(self._rows(), labels=[self.a.label]))


# --------------------------------------------------------------------------------- report

def report(rows: list[dict], labels: list[str] | None = None, opus_window_h: float = 96,
           overrides: dict | None = None) -> str:
    """Valid runs only, per code version and task: mean, bootstrap interval and range, against
    the rolling Opus mean (primary) and the old bar (secondary), with the timeout and
    survived-degraded rates among valid runs and the void reasons."""
    if overrides:
        rows = [{**r, "score": overrides.get(_key(r), r.get("score"))} for r in rows]
    sel = [r for r in rows if r["arm"] == "agentaus" and (not labels or r["label"] in labels)]
    groups: dict = {}
    for r in sel:
        groups.setdefault(((r.get("source_hash") or "?")[:12], r.get("graders") or "?", r["task"]), []).append(r)
    head = (f"{'version':12s} {'task':7s} {'valid/all':>9s} {'mean':>5s} {'95% CI':>11s} {'min-max':>9s} "
            f"{'opus':>10s} {'P>=opus':>7s} {'old bar':>7s} {'P>=bar':>6s} {'timeout':>7s} {'surv.deg':>8s} "
            f"{'wall p50':>8s}  voids")
    out = [head]
    for (ver, graders, task), rs in sorted(groups.items()):
        valid = [r for r in rs if r["verdict"] == "valid" and r.get("score") is not None]
        v = [r["score"] for r in valid]
        lo, hi = boot_ci(v)
        at = max(r.get("ended_at") or 0 for r in rs)
        ref, n_ref, other = opus_reference(rows, task, graders, at, opus_window_h)
        walls = sorted(r["wall_s"] for r in valid) or [0]
        voids: dict = {}
        for r in rs:
            if r["verdict"] != "valid":
                voids[r["verdict"]] = voids.get(r["verdict"], 0) + 1
        bar = OLD_BARS.get(task)
        out.append(
            f"{ver:12s} {task:7s} {len(v):4d}/{len(rs):<4d} "
            f"{(st.mean(v) if v else float('nan')):5.2f} "
            f"{(f'[{lo:.2f},{hi:.2f}]' if v else '-'):>11s} "
            f"{(f'{min(v):.2f}-{max(v):.2f}' if v else '-'):>9s} "
            f"{(f'{ref:.2f}(n={n_ref}){chr(42) if other else chr(32)}' if ref is not None else '-'):>10s} "
            f"{p_at_least(v, ref) if ref is not None else float('nan'):7.2f} "
            f"{bar if bar is not None else float('nan'):7.3f} {p_at_least(v, bar):6.2f} "
            f"{sum(r['timed_out'] for r in valid):3d}/{len(valid):<3d} "
            f"{sum(bool(r.get('survived_degraded')) for r in valid):4d}/{len(valid):<3d} "
            f"{walls[len(walls) // 2]:7.0f}s  "
            f"{', '.join(f'{k[5:]}={n}' for k, n in sorted(voids.items())) or '-'}")
    out.append("opus: rolling mean of valid Opus runs in the last "
               f"{opus_window_h:.0f} h (* = other graders). P = bootstrap share of means at or above; "
               "indicative only at n < 8.")
    return "\n".join(out)


# --------------------------------------------------------------------------------- regrade

def _key(r: dict) -> str:
    return r.get("wd") or f"{r['label']}/{r['arm']}-{r['task']}-{r.get('variant', 'ui')}-{r.get('rep')}"


def historical_runs(labels: list[str] | None) -> list[dict]:
    """Every graded run on disk: ultra.py's per-run .json records and loop.py's alike."""
    out = []
    for d in sorted(RUNS.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or (labels and d.name not in labels):
            continue
        for p in sorted(d.glob("*.json")):
            try:
                r = json.loads(p.read_text())
            except ValueError:
                continue
            if not isinstance(r, dict) or "task" not in r or not p.with_suffix("").is_dir():
                continue
            out.append({"label": d.name, "arm": r["arm"], "task": r["task"], "variant": r.get("variant", "ui"),
                        "rep": r.get("rep", r.get("attempt")), "wd": str(p.with_suffix("")),
                        "stored": (r.get("grade") or {}).get("score"), "infra_error": r.get("infra_error"),
                        "verdict": r.get("verdict")})
    return out


def graders_at(ref: str, dest: Path) -> Path:
    """The grader files as of git `ref`, for an old-vs-new comparison."""
    dest.mkdir(parents=True, exist_ok=True)
    for f in sorted(os.listdir(HERE)):
        if f.startswith("grade_"):
            r = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:abbench/ultra/{f}"], capture_output=True)
            if r.returncode == 0:
                (dest / f).write_bytes(r.stdout)
    return dest


def regrade_one(run: dict, graders_dirs: dict) -> dict:
    """Grade a copy: the run folders stay read-only (the 1913 grader stages files in its wd)."""
    out = dict(run)
    with tempfile.TemporaryDirectory(prefix="ultra-regrade-") as tmp:
        for name, gdir in graders_dirs.items():
            wd = Path(tmp) / name / Path(run["wd"]).name
            shutil.copytree(run["wd"], wd, symlinks=True,
                            ignore=shutil.ignore_patterns(".orig", "__pycache__", "node_modules"))
            try:
                g = ultra.grade(run["task"], wd, graders_dir=gdir)
            except BaseException as exc:  # noqa: BLE001
                g = {"score": None, "grader_error": repr(exc)[:300]}
            out[name] = g.get("score")
            out[f"{name}_checks"] = g
    return out


def cmd_regrade(args) -> None:
    labels = args.labels.split(",") if args.labels else None
    runs = historical_runs(labels)
    dirs = {"new": HERE}
    tmp = Path(tempfile.mkdtemp(prefix="ultra-graders-"))
    if args.compare_ref:
        dirs = {"old": graders_at(args.compare_ref, tmp / "old"), "new": HERE}
    log(f"regrading {len(runs)} runs with {', '.join(f'{k}={v}' for k, v in dirs.items())}")
    rows = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for r in pool.map(lambda x: regrade_one(x, dirs), runs):
            r["graders"] = graders_hash()
            rows.append(r)
            if args.out:
                append_ledger(r, Path(args.out))
    shutil.rmtree(tmp, ignore_errors=True)
    print(regrade_table(rows))


def regrade_table(rows: list[dict]) -> str:
    by: dict = {}
    for r in rows:
        by.setdefault((r["arm"], r["task"]), []).append(r)
    cols = [c for c in ("stored", "old", "new") if any(c in r for r in rows)]
    out = [f"{'arm':9s} {'task':7s} {'n':>3s} " + " ".join(f"{c:>7s}" for c in cols)
           + f" {'changed':>7s} {'up':>3s} {'down':>4s} {'ungraded':>8s}"]
    for (arm, task), rs in sorted(by.items()):
        def mean(c):
            xs = [r[c] for r in rs if r.get(c) is not None and not r.get("infra_error")]
            return f"{st.mean(xs):7.3f}" if xs else f"{'-':>7s}"
        base = "old" if "old" in cols else "stored"
        pairs = [(r.get(base), r.get("new")) for r in rs if r.get(base) is not None and r.get("new") is not None]
        out.append(f"{arm:9s} {task:7s} {len(rs):3d} " + " ".join(mean(c) for c in cols)
                   + f" {sum(a != b for a, b in pairs):7d} {sum(b > a for a, b in pairs):3d} "
                     f"{sum(b < a for a, b in pairs):4d} {sum(r.get('new') is None for r in rs):8d}")
    out.append("means exclude runs ultra.py flagged as infrastructure failures; "
               "stored = the score in each run's record, old/new = this regrade")
    return "\n".join(out)


# --------------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="a round: gate, launch, monitor, void and rerun until N valid")
    r.add_argument("--label", required=True)
    r.add_argument("--tasks", default="c1913h,c1913,he10,tender,rdti")
    r.add_argument("--variant", default="ui")
    r.add_argument("--valid", type=int, default=3, help="valid Agentaus runs wanted per task")
    r.add_argument("--valid-per-task", default="", help="e.g. c1913=8,he10=8")
    r.add_argument("--min-valid", type=int, default=2, help="valid runs before a task may stop early")
    r.add_argument("--opus-valid", type=int, default=2, help="valid Opus runs per task in an Opus round")
    r.add_argument("--opus-every", type=int, default=1, help="run the Opus arm every N rounds; 0 = never")
    r.add_argument("--max-attempt-factor", type=int, default=3, help="give up on a task after N x this attempts")
    r.add_argument("--jobs", type=int, default=2)
    r.add_argument("--port", type=int, default=8800)
    r.add_argument("--timeout", type=int, default=7200)
    r.add_argument("--first-response-s", type=int, default=FIRST_RESPONSE_S)
    r.add_argument("--gate-passes", type=int, default=3, help="probe passes in a row before a cold launch")
    r.add_argument("--probe-concurrency", type=int, default=2)
    r.add_argument("--manage-bridge", action="store_true",
                   help="start the bridge on --port, and restart it between rounds and after a bad run")
    r.add_argument("--graph-width", type=int, default=6, help="AGENTAUS_GRAPH_WIDTH for a bridge the loop starts")
    r.add_argument("--bridge-log", default=None, help="log file for a bridge the loop starts")
    r.add_argument("--bridge-python", default=str(REPO / ".venv" / "bin" / "python")
                   if (REPO / ".venv" / "bin" / "python").exists() else sys.executable)
    r.add_argument("--strays", choices=("kill", "refuse"), default="kill")
    r.add_argument("--ledger", default=str(LEDGER))
    r.add_argument("--once", dest="forever", action="store_false",
                   help="stop after this round (the default runs round after round: d15 -> d16 ...,"
                        " each on the code then on disk)")
    p = sub.add_parser("report", help="valid runs only, against the rolling Opus mean")
    p.add_argument("--labels", default="")
    p.add_argument("--ledger", default=str(LEDGER))
    p.add_argument("--regrade", default="", help="a regrade .jsonl whose `new` scores replace the ledger's")
    g = sub.add_parser("regrade", help="grade copies of every run again; run folders are not touched")
    g.add_argument("--labels", default="")
    g.add_argument("--compare-ref", default="HEAD", help="git ref of the graders to compare with; '' = none")
    g.add_argument("--jobs", type=int, default=4)
    g.add_argument("--out", default="", help="append each regraded run to this .jsonl")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        if not (ultra.FIXTURES / "rdti_answer_key.json").exists():
            raise SystemExit("run `ultra.py setup` first")
        args.bridge = f"http://127.0.0.1:{args.port}"
        log_path = Path(args.bridge_log or RUNS / args.label / f"bridge-{args.port}.log")
        bridge = Bridge(args.port, args.manage_bridge, log_path, args.bridge_python, args.graph_width, args.strays)
        bridge.check_strays()
        if not args.manage_bridge and not bridge.client.alive():
            raise SystemExit(f"no bridge on {args.port}; start one or pass --manage-bridge")
        while True:
            Loop(args, bridge=bridge).run()
            if not args.forever:
                break
            m = re.match(r"^(.*?)(\d+)$", args.label)
            args.label = f"{m.group(1)}{int(m.group(2)) + 1}" if m else f"{args.label}-2"
            log(f"forever: round over - next round {args.label}")
    elif args.cmd == "report":
        rows = read_ledger(Path(args.ledger))
        overrides = {r["wd"]: r.get("new") for r in read_ledger(Path(args.regrade))} if args.regrade else None
        print(report(rows, [l for l in args.labels.split(",") if l] or None, overrides=overrides))
    else:
        cmd_regrade(args)


if __name__ == "__main__":
    main()
