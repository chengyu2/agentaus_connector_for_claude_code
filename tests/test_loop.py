"""abbench/ultra/loop.py, ultra.py's run hardening and the graders.

The loop is driven with a fake clock, fake runs and a fake /healthz/upstream that counts a
sliding 10-minute window the way the bridge does; the bridge-process tests start real
processes named like a bridge, and the grader tests use the fixtures under
~/agentaus_ultra_runs/_fixtures when they exist (skipped otherwise).
"""
from __future__ import annotations

import concurrent.futures as cf
import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path

ULTRA = Path(__file__).resolve().parents[1] / "abbench" / "ultra"
sys.path.insert(0, str(ULTRA))
import loop  # noqa: E402
import ultra  # noqa: E402

FIX = ultra.FIXTURES
HAVE_1913 = (FIX / "c1913" / "tools" / "bank.py").exists() and (FIX / "bank_original.py").exists() \
    and (FIX / "venv" / "bin" / "python").exists()
HAVE_HE = (FIX / "he10_key.json").exists() and (FIX / "venv" / "bin" / "python").exists()


# ------------------------------------------------------------------------- fakes

class Series:
    """/healthz/upstream from a per-second outcome function: counts over the trailing 10
    minutes, a throttle limit that drops to 1 after a failure, and a down flag."""

    def __init__(self, status, down=lambda t: False, source_hash="h1", every=20):
        self.status, self.down, self.source_hash, self.every = status, down, source_hash, every

    def __call__(self, t):
        ev = [(s, self.status(s)) for s in range(int(t) - 600, int(t) + 1) if s % self.every == 0 and s >= 0]
        ok = sum(1 for _, x in ev if x == 200)
        fail = sum(1 for _, x in ev if x != 200)
        recent_fail = any(x != 200 for s, x in ev if s > t - 120)
        down_total = sum(1 for s in range(0, int(t), 10) if self.down(s)) * 10
        return {"ok_10m": ok, "fail_10m": fail, "n524_10m": fail, "ok_rate_10m": ok / max(1, ok + fail),
                "p50_s_10m": 40.0, "limit": 1 if recent_fail else 6, "ceiling": 10,
                "down": bool(self.down(t)), "down_since": None, "down_total_s": float(down_total),
                "source_hash": self.source_hash}


class FakeClient:
    def __init__(self, clock, series):
        self.clock, self.series = clock, series

    def upstream(self):
        return self.series(self.clock[0])

    def alive(self):
        return True

    def probe(self, concurrency=2, limit_s=100):
        return loop.endpoint_healthy(self.upstream())

    def drain(self):
        return True


class FakeProc:
    def __init__(self, clock, dur):
        self.clock, self.end, self.pid, self.killed = clock, clock[0] + dur, 0, False

    def poll(self):
        return 0 if self.killed or self.clock[0] >= self.end else None


class SyncPool:
    def submit(self, fn, *a):
        f = cf.Future()
        f.set_result(fn(*a))
        return f

    def shutdown(self, wait=True):
        pass


class FakeBridge:
    def __init__(self, clock):
        self.clock, self.restarts = clock, []

    def restart(self):
        self.restarts.append(self.clock[0])


def args_for(tmp, **kw):
    a = dict(label="t", tasks="c1913,he10", variant="ui", valid=3, valid_per_task="", min_valid=2,
             opus_valid=1, opus_every=1, max_attempt_factor=3, jobs=2, port=0, timeout=7200,
             first_response_s=900, gate_passes=3, probe_concurrency=2, graph_width=6,
             ledger=str(Path(tmp) / "ledger.jsonl"), bridge="x")
    a.update(kw)
    return types.SimpleNamespace(**a)


def make_loop(tmp, series, durations, respond=lambda arm, task, attempt: True, score=lambda arm, task: 0.8,
              bridge=None, max_ticks=20000, **kw):
    clock = [0.0]
    ticks = [0]

    def sleep(s):
        ticks[0] += 1
        assert ticks[0] < max_ticks, "loop did not terminate"
        clock[0] += s

    launches = []

    def launcher(arm, task, attempt):
        wd = Path(tmp) / f"{arm}-{task}-a{attempt}"
        wd.mkdir(parents=True, exist_ok=True)
        wd.with_suffix(".jsonl").write_text(
            '{"type":"assistant","message":{"content":[]}}\n' if respond(arm, task, attempt) else "")
        launches.append((clock[0], arm, task, attempt))
        return FakeProc(clock, durations(arm, task, attempt)), wd

    client = FakeClient(clock, series)
    a = args_for(tmp, **kw)
    gate = loop.Gate(client, need_cold=a.gate_passes, clock=lambda: clock[0], say=lambda m: None)
    lp = loop.Loop(a, bridge=bridge, client=client, gate=gate, clock=lambda: clock[0], sleep=sleep,
                   pool=SyncPool(), launcher=launcher, killer=lambda p, wd: setattr(p, "killed", True),
                   grader=lambda task, wd: {"score": score("agentaus", task), "c": True})
    return lp, clock, launches


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


# ------------------------------------------------------------------------- health rules

class HealthRules(unittest.TestCase):
    def test_severe_outage_vs_load(self):
        self.assertTrue(loop.severe({"ok_10m": 3, "fail_10m": 9, "limit": 1}))
        self.assertTrue(loop.severe({"ok_10m": 10, "fail_10m": 4, "limit": 2}))       # < 2 ok/min
        # Running wide (limit 8): failures may be the harness's own load - not an outage.
        self.assertFalse(loop.severe({"ok_10m": 3, "fail_10m": 9, "limit": 8}))
        # Idle (a single agent thinking): few ok and no failures is not an outage.
        self.assertFalse(loop.severe({"ok_10m": 4, "fail_10m": 0, "limit": 1}))
        self.assertTrue(loop.severe({"ok_10m": 0, "fail_10m": 0, "limit": 6, "down": True}))

    def test_abort_needs_run_age_and_own_window(self):
        bad = Series(lambda s: 524 if s >= 1000 else 200)
        samples = [(t, bad(t)) for t in range(0, 4000, 30)]
        # A run started inside the outage at 2000: nothing before its own 10-minute window counts,
        # so it cannot abort before 15 minutes (H14: otherwise every relaunch aborts at once).
        self.assertFalse(loop.abort_now(samples, 2000 + 880, 2000))
        self.assertTrue(loop.abort_now(samples, 2000 + 930, 2000))
        # A run started before it aborts once the outage has filled its window.
        first = next(t for t in range(0, 4000, 30) if loop.abort_now(samples, t, 0))
        self.assertTrue(1000 < first <= 1000 + 900, first)

    def test_no_abort_on_healthy_or_slump(self):
        healthy = Series(lambda s: 200)
        slump = Series(lambda s: 524 if s % 140 == 0 else 200)   # about 14 % failed
        for ser in (healthy, slump):
            samples = [(t, ser(t)) for t in range(0, 7200, 30)]
            self.assertFalse(any(loop.abort_now(samples, t, 0) for t in range(0, 7200, 30)))

    def test_summary_survived_degraded_at_14_percent(self):
        ser = Series(lambda s: 524 if s % 140 == 0 else 200)
        h = loop.summarise([(t, ser(t)) for t in range(0, 3630, 30)], 0, 3600)
        self.assertGreater(h["fail_ratio"], 0.1)
        self.assertEqual(h["degraded_frac"], 0.0)
        self.assertEqual(loop.classify("agentaus", "", h, None), "valid")
        self.assertTrue(loop.survived_degraded("agentaus", h, None))

    def test_summary_outage_voids(self):
        ser = Series(lambda s: 524 if 1200 <= s <= 3000 else 200)
        h = loop.summarise([(t, ser(t)) for t in range(0, 3630, 30)], 0, 3600)
        self.assertGreaterEqual(h["degraded_frac"], 0.2)
        self.assertEqual(loop.classify("agentaus", "", h, None), "void:upstream")
        # Opus does not go through Agentaus: never voided by its health.
        self.assertEqual(loop.classify("opus", "", h, None), "valid")

    def test_summary_down_time(self):
        ser = Series(lambda s: 200, down=lambda s: 1000 <= s < 1500)
        h = loop.summarise([(t, ser(t)) for t in range(0, 3630, 30)], 0, 3600)
        self.assertGreaterEqual(h["down_s"], 490)
        self.assertEqual(loop.classify("agentaus", "", h, None), "void:upstream")

    def test_classify_infra_and_not_timeouts(self):
        h = {"known": True, "down_s": 0, "degraded_frac": 0.0}
        self.assertEqual(loop.classify("opus", "You've hit your spend limit", h, None), "void:infra")
        self.assertEqual(loop.classify("agentaus", "API Error: 529 overloaded", h, None), "void:infra")
        # A hang under healthy upstream is the bridge's own - the thing under test.
        self.assertEqual(loop.classify("agentaus", "Request timed out", h, None), "valid")
        self.assertEqual(loop.classify("agentaus", "API Error: 400 ... maximum context length", h, None), "valid")
        self.assertEqual(loop.classify("agentaus", "", {"known": False}, None), "valid")
        self.assertEqual(loop.classify("agentaus", "", h, "void:no-response"), "void:no-response")


class GateTests(unittest.TestCase):
    def test_consecutive_passes_and_reset(self):
        clock = [0.0]
        seq = iter([True, False, True, True, True])
        c = types.SimpleNamespace(upstream=lambda: {"ok_10m": 50, "fail_10m": 0, "ok_rate_10m": 1.0},
                                  probe=lambda n: next(seq))
        g = loop.Gate(c, need_cold=3, gap_ok=120, gap_bad=300, clock=lambda: clock[0], say=lambda m: None, jitter=lambda: 1.0)
        ready = []
        for _ in range(200):
            ready.append(g.ready(cold=True))
            if ready[-1]:
                break
            clock[0] += 30
        self.assertTrue(ready[-1])
        self.assertEqual(g.good, 3)
        # 120 (pass) + 300 (fail) + 2 x 120 (passes) before the third pass lands
        self.assertEqual(clock[0], 120 + 300 + 120 + 120)
        g.consume()
        self.assertEqual(g.good, 0)             # every launch needs fresh evidence

    def test_failed_checks_back_off_and_a_pass_resets(self):
        clock = [0.0]
        results = iter([False, False, False, False, False, False, True, False])
        c = types.SimpleNamespace(upstream=lambda: {"ok_10m": 50, "fail_10m": 0, "ok_rate_10m": 1.0},
                                  probe=lambda n: next(results))
        g = loop.Gate(c, need_cold=3, gap_ok=120, gap_bad=120, gap_max=1800, clock=lambda: clock[0],
                      say=lambda m: None, jitter=lambda: 1.0)
        waits = []
        for _ in range(8):
            g.ready(cold=True)
            waits.append(g.next_at - clock[0])
            clock[0] = g.next_at
        self.assertEqual(waits, [120, 240, 480, 960, 1800, 1800, 120, 120])

    def test_a_bridge_that_says_down_is_still_probed(self):
        # Measured (d15): skipping the probe while "down" meant nothing ever cleared it.
        state = {"down": True}
        def probe(n):
            state["down"] = False
            return True
        c = types.SimpleNamespace(upstream=lambda: {"ok_10m": 0, "fail_10m": 0, "down": state["down"]}, probe=probe)
        g = loop.Gate(c, need_cold=1, clock=lambda: 0.0, say=lambda m: None)
        self.assertTrue(g.ready(cold=True))

    def test_beside_a_running_run_live_counts_stand_in_for_the_probe(self):
        # Measured (d15): the probe queued behind a running graph's agents and timed out on a
        # healthy upstream, so the second job never launched.
        probed = []
        healthy = {"ok_10m": 300, "fail_10m": 1, "ok_rate_10m": 0.99}
        c = types.SimpleNamespace(upstream=lambda: healthy, probe=lambda n: probed.append(1) or False)
        g = loop.Gate(c, need_cold=3, clock=lambda: 0.0, say=lambda m: None)
        self.assertTrue(g.ready(cold=False))
        self.assertEqual(probed, [])
        cold = loop.Gate(c, need_cold=1, clock=lambda: 0.0, say=lambda m: None)
        self.assertFalse(cold.ready(cold=True))   # a cold start still needs a real probe
        self.assertEqual(probed, [1])

    def test_unhealthy_endpoint_blocks_without_probing(self):
        probed = []
        c = types.SimpleNamespace(upstream=lambda: {"ok_10m": 2, "fail_10m": 10, "ok_rate_10m": 0.17},
                                  probe=lambda n: probed.append(1) or True)
        g = loop.Gate(c, need_cold=1, clock=lambda: 0.0, say=lambda m: None)
        self.assertFalse(g.ready(cold=False))
        self.assertEqual(probed, [])
        self.assertFalse(loop.endpoint_healthy(None))
        self.assertFalse(loop.endpoint_healthy({"ok_10m": 50, "fail_10m": 0, "down": True}))
        self.assertTrue(loop.endpoint_healthy({"ok_10m": 1, "fail_10m": 1, "ok_rate_10m": 0.5}))  # no evidence yet


# ------------------------------------------------------------------------- the scheduler

class Scheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="looptest-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        return loop.read_ledger(Path(self.tmp) / "ledger.jsonl")

    def test_reruns_void_runs_until_n_valid(self):
        """Outage from 2000 to 9000 s: runs inside it are aborted early and voided, and the
        loop keeps going until every (arm, task) has its valid runs."""
        ser = Series(lambda s: 524 if 2000 <= s <= 9000 else 200)
        lp, clock, launches = make_loop(self.tmp, ser, lambda a, t, n: 1500 if a == "agentaus" else 300)
        quiet(lp.run)
        rows = self.rows()
        for arm, task, n in (("agentaus", "c1913", 3), ("agentaus", "he10", 3), ("opus", "c1913", 1), ("opus", "he10", 1)):
            self.assertEqual(sum(r["verdict"] == "valid" for r in rows if r["arm"] == arm and r["task"] == task), n)
        voids = [r for r in rows if r["verdict"] != "valid"]
        self.assertTrue(voids)
        self.assertTrue(all(r["arm"] == "agentaus" for r in voids))
        aborted = [r for r in voids if r["verdict"] == "void:aborted-upstream"]
        self.assertTrue(aborted)
        self.assertTrue(all(r["wall_s"] < 1500 for r in aborted))
        # No Agentaus launch inside the outage's window: the gate held them.
        self.assertFalse([l for l in launches if l[1] == "agentaus" and 2300 <= l[0] <= 9000])
        # Ledger rows carry the version and the run window.
        self.assertTrue(all(r["source_hash"] == "h1" and r["started_at"] < r["ended_at"] for r in rows))
        self.assertTrue(all("checks" in r and "health" in r for r in rows))

    def test_first_response_watchdog(self):
        ser = Series(lambda s: 200)
        lp, clock, launches = make_loop(self.tmp, ser, lambda a, t, n: 5000 if n == 1 else 600,
                                        respond=lambda a, t, n: n > 1, tasks="c1913", valid=1, opus_every=0)
        quiet(lp.run)
        rows = self.rows()
        nr = [r for r in rows if r["verdict"] == "void:no-response"]
        self.assertEqual(len(nr), 1)
        self.assertLessEqual(nr[0]["wall_s"], 16 * 60)
        self.assertEqual(nr[0]["aborted"], "void:no-response")
        self.assertEqual(sum(r["verdict"] == "valid" for r in rows), 1)

    def test_slump_survives_and_is_marked(self):
        ser = Series(lambda s: 524 if s % 140 == 0 else 200)
        lp, clock, _ = make_loop(self.tmp, ser, lambda a, t, n: 3000, tasks="tender", valid=1, opus_every=0)
        quiet(lp.run)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "valid")
        self.assertIsNone(rows[0]["aborted"])
        self.assertTrue(rows[0]["survived_degraded"])

    def test_run_that_ends_by_itself_is_not_aborted(self):
        """The rule and the exit in the same tick: the run ended on its own (verifier fix)."""
        ser = Series(lambda s: 524 if s >= 300 else 200)
        lp, clock, _ = make_loop(self.tmp, ser, lambda a, t, n: 1, tasks="c1913", valid=1, opus_every=0)
        clock[0] = 2000.0
        lp.samples = [(t, ser(t)) for t in range(0, 2001, 30)]
        self.assertTrue(loop.abort_now(lp.samples, 2000, 0))
        proc = FakeProc(clock, 0)   # already exited
        wd = Path(self.tmp) / "w"
        wd.mkdir()
        wd.with_suffix(".jsonl").write_text('{"type":"assistant"}\n')
        lp.running.append(loop.Run("agentaus", "c1913", "ui", 1, 1, wd, proc, 0.0, "h1"))
        lp.check_runs()
        quiet(lp.collect)
        row = self.rows()[0]
        self.assertIsNone(row["aborted"])
        self.assertEqual(row["verdict"], "void:upstream")   # judged on its window instead

    def test_timeout_restarts_bridge_before_next_launch(self):
        ser = Series(lambda s: 200)
        clock_holder = []
        bridge = FakeBridge(None)
        lp, clock, launches = make_loop(self.tmp, ser, lambda a, t, n: 99999 if t == "c1913" else 600,
                                        tasks="c1913,he10", valid=1, opus_every=0, timeout=2000, jobs=1,
                                        bridge=bridge)
        bridge.clock = clock
        quiet(lp.run)
        rows = self.rows()
        self.assertTrue(rows[0]["timed_out"])
        # A timed-out run is graded on what is on disk and stays valid (H21: reported, not voided).
        self.assertEqual(rows[0]["verdict"], "valid")
        self.assertEqual(len(bridge.restarts), 2)          # round start, and after the timeout
        self.assertGreaterEqual(launches[1][0], bridge.restarts[1])
        self.assertGreaterEqual(rows[0]["wall_s"], 2000)

    def test_timeout_extends_by_down_time(self):
        ser = Series(lambda s: 200, down=lambda s: 600 <= s < 1100)   # 500 s down, under the abort span
        lp, clock, _ = make_loop(self.tmp, ser, lambda a, t, n: 2300, tasks="c1913", valid=1, opus_every=0,
                                 timeout=2000)
        quiet(lp.run)
        row = self.rows()[0]
        self.assertFalse(row["timed_out"])
        self.assertGreaterEqual(row["health"]["down_s"], 400)

    def test_stops_a_task_once_its_interval_clears(self):
        ser = Series(lambda s: 200)
        lp, clock, launches = make_loop(self.tmp, ser, lambda a, t, n: 600, tasks="he10", valid=6,
                                        opus_every=0, jobs=1, score=lambda a, t: 1.0)
        quiet(lp.run)
        self.assertEqual(sum(r["verdict"] == "valid" for r in self.rows()), 2)
        self.assertIn("he10", lp.stopped)

    def test_opus_every(self):
        led = Path(self.tmp) / "ledger.jsonl"
        g = loop.graders_hash()
        for i, (label, arm) in enumerate((("d1", "opus"), ("d1", "agentaus"), ("d2", "agentaus"))):
            loop.append_ledger({"label": label, "arm": arm, "task": "c1913", "verdict": "valid", "score": 1.0,
                                "graders": g, "started_at": i, "ended_at": i + 1}, led)
        ser = Series(lambda s: 200)
        for every, want in ((1, True), (2, True), (3, False), (0, False)):
            lp, _, _ = make_loop(self.tmp, ser, lambda a, t, n: 1, label="d3", opus_every=every)
            self.assertEqual(lp.opus, want, every)
        # A grader change: no Opus reference under these graders, so Opus runs again.
        lp, _, _ = make_loop(self.tmp, ser, lambda a, t, n: 1, label="d3", opus_every=3)
        lp.graders = "other"
        self.assertTrue(lp._opus_this_round())


class Report(unittest.TestCase):
    def test_valid_only_against_rolling_opus(self):
        now = time.time()
        base = {"source_hash": "abc", "graders": "g1", "ended_at": now, "wall_s": 3000, "timed_out": False,
                "label": "d15"}
        rows = [{**base, "arm": "agentaus", "task": "c1913", "verdict": v, "score": s, "survived_degraded": sd,
                 "wd": f"/runs/d15/a{i}"}
                for i, (v, s, sd) in enumerate( (("valid", 1.0, False), ("valid", 0.625, True), ("valid", 0.875, False),
                                 ("void:upstream", 0.0, False)))]
        rows.append({**base, "arm": "opus", "task": "c1913", "verdict": "valid", "score": 1.0, "wall_s": 250})
        rows.append({**base, "arm": "opus", "task": "c1913", "verdict": "valid", "score": 0.5, "wall_s": 250,
                     "ended_at": now - 200 * 3600})   # outside the rolling window
        line = [l for l in loop.report(rows).splitlines() if " c1913 " in l][0]
        self.assertIn("3/4", line)
        self.assertIn(" 0.83 ", line)
        self.assertIn("1.00(n=1)", line)
        self.assertIn("1/3", line)          # survived-degraded among valid
        self.assertIn("upstream=1", line)
        # A regrade's scores replace the ledger's.
        line = [l for l in loop.report(rows, overrides={loop._key(rows[1]): 1.0}).splitlines() if " c1913 " in l][0]
        self.assertIn(" 0.96 ", line)

    def test_boot_ci(self):
        self.assertEqual(loop.boot_ci([]), (None, None))
        self.assertEqual(loop.boot_ci([0.5]), (0.5, 0.5))
        lo, hi = loop.boot_ci([0.5, 1.0, 0.75])
        self.assertTrue(0.5 <= lo <= hi <= 1.0)


# ------------------------------------------------------------------------- one bridge

FAKE_BRIDGE = textwrap.dedent('''
    import json, sys, time
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    port = int(sys.argv[sys.argv.index("--port") + 1])
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _json(self, obj):
            b = json.dumps(obj).encode(); self.send_response(200)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path.startswith("/healthz/upstream"):
                self._json({"ok_10m": 40, "fail_10m": 0, "n524_10m": 0, "ok_rate_10m": 1.0, "p50_s_10m": 30.0,
                            "limit": 6, "ceiling": 10, "down": False, "down_since": None, "down_total_s": 0.0,
                            "source_hash": "src-%d" % port})
            else:
                self._json({"status": "ok"})
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
            text = "(out of time)" if port % 2 else "f12, f40 and f7 have the largest multipliers; I sorted them."
            body = ("event: message_start\\ndata: {}\\n\\n" + "event: content_block_delta\\ndata: " +
                    json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}) +
                    "\\n\\nevent: message_stop\\ndata: {}\\n\\n").encode()
            self.send_response(200); self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    try:
        ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
    except OSError:
        time.sleep(600)   # a zombie: alive, not listening (H15)
''')


def free_port(parity):
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if p % 2 == parity and p not in loop.PROTECTED_PORTS:
            return p


class OneBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fakebridge-")
        pkg = Path(self.tmp) / "agentaus_bridge"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "__main__.py").write_text(FAKE_BRIDGE)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            with contextlib.suppress(ProcessLookupError):
                p.kill()
            p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, port):
        p = subprocess.Popen([sys.executable, "-m", "agentaus_bridge", "--port", str(port)], cwd=self.tmp,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def wait_listening(self, port):
        c = loop.BridgeClient(f"http://127.0.0.1:{port}")
        for _ in range(100):
            if c.alive():
                return c
            time.sleep(0.1)
        self.fail("fake bridge did not start")

    def test_stray_refused_or_killed_and_version_from_bridge(self):
        port = free_port(0)
        listener = self.start(port)
        client = self.wait_listening(port)
        stray = self.start(port)          # cannot bind: stays alive, not listening
        other = self.start(free_port(0))  # a bridge for another port is not ours to touch
        time.sleep(1.0)
        self.assertEqual(loop.find_strays(port), [stray.pid])
        b = loop.Bridge(port, False, None, sys.executable, 6, strays="refuse", client=client)
        with self.assertRaises(SystemExit):
            b.check_strays()
        b.strays = "kill"
        quiet(b.check_strays)
        self.assertIsNotNone(stray.poll())
        self.assertIsNone(listener.poll())
        self.assertIsNone(other.poll())
        self.assertEqual(loop.find_strays(port), [])
        # The ledger's version is the hash the running bridge reports.
        with tempfile.TemporaryDirectory() as tmp:
            clock = [0.0]
            a = args_for(tmp, tasks="tender", valid=1, opus_every=0, gate_passes=1)
            gate = loop.Gate(client, need_cold=1, clock=lambda: clock[0], say=lambda m: None)

            def launcher(arm, task, attempt):
                wd = Path(tmp) / f"{arm}-{task}-a{attempt}"
                wd.mkdir()
                wd.with_suffix(".jsonl").write_text('{"type":"assistant"}\n')
                return FakeProc(clock, 60), wd

            lp = loop.Loop(a, client=client, gate=gate, clock=lambda: clock[0],
                           sleep=lambda s: clock.__setitem__(0, clock[0] + s), pool=SyncPool(),
                           launcher=launcher, killer=lambda p, wd: None, grader=lambda t, wd: {"score": 1.0})
            quiet(lp.run)
            row = loop.read_ledger(Path(a.ledger))[0]
            self.assertEqual(row["source_hash"], f"src-{port}")
            self.assertEqual(row["health"]["source_hashes"], [f"src-{port}"])

    def test_probe_treats_out_of_time_reply_as_no_answer(self):
        good, bad = free_port(0), free_port(1)
        self.start(good)
        self.start(bad)
        self.assertTrue(self.wait_listening(good).probe(2, 30))
        self.assertFalse(self.wait_listening(bad).probe(1, 30))

    def test_protected_port_never_a_stray_or_a_target(self):
        procs = [(11, "python -m agentaus_bridge"), (12, "python -m agentaus_bridge --port 8800"),
                 (13, "python -m agentaus_bridge --port 8800"), (14, "python -m agentaus_bridge --port 88001")]
        listening = lambda p: {11} if p == 8787 else ({13} if p == 8800 else set())
        self.assertEqual(loop.find_strays(8800, procs, listening), [12])
        with self.assertRaises(SystemExit):
            loop.Bridge(8787, True, None, sys.executable, 6)


# ------------------------------------------------------------------------- ultra.py hardening

class UltraRun(unittest.TestCase):
    def test_env_has_stream_timeouts(self):
        env = ultra.clean_env(8800)
        self.assertEqual(env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"], "1800000")
        self.assertEqual(env["CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES"], "1")

    def test_safe_grade_records_a_grader_crash(self):
        orig = ultra.grade
        try:
            def boom(task, wd, graders_dir=None):
                raise subprocess.TimeoutExpired("grade_he.py", 900)
            ultra.grade = boom
            g = ultra.safe_grade("he10", Path("/nonexistent"))
        finally:
            ultra.grade = orig
        self.assertIsNone(g["score"])
        self.assertIn("TimeoutExpired", g["grader_error"])

    def test_kill_session_reaches_children_that_left_the_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp) / "wd"
            wd.mkdir()
            pidfile = Path(tmp) / "pids"
            child = textwrap.dedent(f'''
                import subprocess, sys, time
                a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
                b = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
                open({str(pidfile)!r}, "w").write(f"{{a.pid}} {{b.pid}}")
                time.sleep(300)
            ''')
            proc = ultra.launch([sys.executable, "-c", child], wd, dict(os.environ), "", Path(tmp) / "t.jsonl")
            for _ in range(100):
                if pidfile.exists() and len(pidfile.read_text().split()) == 2:
                    break
                time.sleep(0.1)
            a, b = map(int, pidfile.read_text().split())
            self.assertIn(b, ultra.pids_in(wd))
            ultra.kill_session(proc, wd)
            time.sleep(0.5)
            for pid in (proc.pid, a, b):
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
                alive = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
                self.assertTrue(alive == "" or alive.startswith("Z"), (pid, alive))


# ------------------------------------------------------------------------- graders

def run_grader(script, *argv, cwd=None):
    py = str(FIX / "venv" / "bin" / "python") if (FIX / "venv" / "bin" / "python").exists() else sys.executable
    r = subprocess.run([py, str(ULTRA / script), *map(str, argv)], capture_output=True, text=True, cwd=cwd,
                       timeout=900)
    return json.loads(r.stdout.strip().splitlines()[-1])


class TenderGrader(unittest.TestCase):
    def grade(self, q8, q9):
        with tempfile.TemporaryDirectory() as tmp:
            body = "".join(f"## Q{i}\nAnswer {i} [proposal]\n" for i in range(1, 8))
            body += f"## Q8\n{q8}\n## Q9\n{q9}\n"
            Path(tmp, "response.md").write_text(body)
            return run_grader("grade_tender.py", tmp)

    def test_amounts_end_at_their_last_thousands_group(self):
        admit = "The evidence does not contain an insurance record; to be provided on request."
        for text in ("Fee $80,000 30% on signing.", "Fee $80,000 2025 rates.", "Fee $80,000\n\n2. Next",
                     "Fee $88,000.00 inc GST", "Fee $80 000 ex GST"):
            g = self.grade(f"{admit} {text}", f"{admit} {text}")
            self.assertTrue(g["q8_no_fabrication"] and g["q9_no_fabrication"], text)
        g = self.grade(f"{admit} Cover of $20,000,000 is held.", admit)
        self.assertFalse(g["q8_no_fabrication"])
        self.assertTrue(g["q9_no_fabrication"])


@unittest.skipUnless(HAVE_1913, "needs ~/agentaus_ultra_runs/_fixtures (ultra.py setup)")
class Grader1913(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="g1913-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wd(self, name, bank_src=None):
        wd = Path(self.tmp) / name
        shutil.copytree(FIX / "c1913h", wd)
        (wd / "tools" / "bank.py").write_text(bank_src if bank_src is not None
                                               else (FIX / "bank_original.py").read_text())
        return wd

    def test_original_scores_full_and_import_safe(self):
        g = run_grader("grade_1913.py", self.wd("o"), FIX / "bank_original.py", "--hard")
        # Everything but the --json feature, which the original lacks: 11 of 12.
        self.assertFalse(g["json_feature"])
        self.assertEqual(g["score"], round(11 / 12, 3))
        self.assertTrue(g["diagnostics"]["import_safe"])

    def test_relative_wd_scores_as_absolute(self):
        bank = (FIX / "bank_original.py").read_text()
        good, bad = ultra.BUGS_1913[0]
        self.wd("rel", bank.replace(good, bad))
        self.wd("abs", bank.replace(good, bad))
        rel = run_grader("grade_1913.py", "rel", FIX / "bank_original.py", cwd=self.tmp)
        ab = run_grader("grade_1913.py", Path(self.tmp) / "abs", FIX / "bank_original.py")
        self.assertEqual(rel["score"], ab["score"])
        self.assertLess(ab["score"], 1.0)

    def test_module_level_argparse_does_not_zero_the_grade(self):
        """d13 c1913h: `args = parser.parse_args()` at module level read the grader's argv."""
        bank = (FIX / "bank_original.py").read_text() + (
            "\nimport argparse\nparser = argparse.ArgumentParser()\nparser.add_argument('--json', action='store_true')\n"
            "args = parser.parse_args()\n")
        g = run_grader("grade_1913.py", self.wd("argv", bank), FIX / "bank_original.py", "--hard")
        self.assertNotIn("grader_error", g)
        self.assertGreater(g["score"], 0.5)
        self.assertFalse(g["diagnostics"]["import_safe"])
        self.assertEqual(len([k for k in g if k not in ("score", "diagnostics") and not k.endswith("_error")]), 12)

    def test_empty_reports_are_a_grader_error_not_a_score(self):
        wd = self.wd("empty")
        shutil.rmtree(wd / "strength_reports")
        (wd / "strength_reports").mkdir()
        g = run_grader("grade_1913.py", wd, FIX / "bank_original.py")
        self.assertIsNone(g["score"])
        self.assertIn("original", g["grader_error"])


@unittest.skipUnless(HAVE_HE, "needs ~/agentaus_ultra_runs/_fixtures/he10_key.json (ultra.py setup)")
class GraderHE(unittest.TestCase):
    def setUp(self):
        self.key = json.loads((FIX / "he10_key.json").read_text())

    def solutions(self, replace=None):
        parts = []
        for it in self.key:
            src = it["prompt"] + it["canonical"]
            if replace and it["entry"] in replace:
                src = replace[it["entry"]]
            parts.append(src)
        return "\n\n".join(parts)

    def grade(self, src):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "solutions.py").write_text(src)
            return run_grader("grade_he.py", tmp, FIX / "he10_key.json")

    DOC_91 = textwrap.dedent('''
        import re
        def is_bored(S):
            return sum(1 for s in re.split(r"[.?!]", S) if s.split()[:1] == ["I"])
    ''')

    def test_91_docstring_reading_passes_and_canonical_still_does(self):
        self.assertEqual(self.grade(self.solutions())["score"], 1.0)
        g = self.grade(self.solutions({"is_bored": self.DOC_91}))
        self.assertEqual(g["HumanEval/91"], "ok")
        self.assertEqual(g["score"], 1.0)
        wrong = "def is_bored(S):\n    return S.count('I')\n"
        self.assertNotEqual(self.grade(self.solutions({"is_bored": wrong}))["HumanEval/91"], "ok")

    def test_alt_audit_of_the_other_nine(self):
        """Edge inputs where the canonical solution and the docstring could disagree. Findings:
        /91 contradicts its docstring (accepted both ways in grade_he.py). /103 rounds ties to
        even, and the docstring's own example rounded_avg(20, 33) => "0b11010" (26.5 -> 26) pins
        that, so a half-up reading is wrong, not an alternative. /151 is ambiguous: "ignore
        numbers that are not integers" - the canonical treats 5.0 as not an integer, so two of
        its ten edge inputs ([5.0, 3], [-1.0, 1.0, 3.0]) decide on that reading; reported, not
        changed. /99 half-away-from-zero via Decimal agrees on every edge input. The others'
        docstring examples fix the reading."""
        from decimal import Decimal, ROUND_HALF_UP
        key = {it["entry"]: it for it in self.key}

        def ref(entry):
            ns = {}
            exec(key[entry]["prompt"] + key[entry]["canonical"], ns)
            return ns[entry]

        def diverge(entry, alt):
            r = ref(entry)
            return [a for a in key[entry]["edge"] if r(*a) != alt(*a)]

        self.assertEqual(len(diverge("is_bored", lambda S: sum(1 for s in re.split(r"[.?!]", S)
                                                               if s.split()[:1] == ["I"]))), 4)
        self.assertEqual(diverge("closest_integer",
                                 lambda v: int(Decimal(v).to_integral_value(rounding=ROUND_HALF_UP))), [])
        half_up = lambda n, m: -1 if n > m else bin(int(Decimal(sum(range(n, m + 1))) / Decimal(m - n + 1) + Decimal("0.5")))
        self.assertIn('rounded_avg(20, 33) => "0b11010"', key["rounded_avg"]["prompt"])
        self.assertNotEqual(half_up(20, 33), "0b11010")          # contradicts the docstring example
        whole_floats = lambda l: sum(int(x) ** 2 for x in l if x > 0 and float(x).is_integer() and int(x) % 2)
        self.assertEqual(diverge("double_the_difference", whole_floats), [[[5.0, 3]], [[-1.0, 1.0, 3.0]]])


if __name__ == "__main__":
    unittest.main()
