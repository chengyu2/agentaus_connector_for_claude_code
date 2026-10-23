"""The adaptive limit on Agentaus calls: halved on failure (never below the floor), back up one
step at a time; request-specific failures leave it alone; the upstream's health is recorded."""

import asyncio
import unittest

import httpx

from agentaus_bridge import throttle


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def run(coro):
    return asyncio.run(coro)


class TheLimit(unittest.TestCase):
    def test_a_failure_halves_it_and_never_below_the_floor(self):
        gate = throttle.AdaptiveLimit(10, 20, Clock(), floor=2)

        async def go():
            for want in (5, 2, 2):
                await gate.acquire()
                await gate.release(False)
                self.assertEqual(gate.limit, want)
        run(go())

    def test_a_failure_of_a_time_up_turn_or_a_retry_leaves_it_alone(self):
        # Measured (d14): every 524 at limit 1 was a copy of a time-up turn while ordinary
        # streams succeeded beside them.
        gate = throttle.AdaptiveLimit(10, 20, Clock(), floor=2)

        async def go():
            for kind in ("time_up", "retry"):
                await gate.acquire()
                await gate.release(False, kind)
        run(go())
        self.assertEqual((gate.limit, gate.busy), (10, 0))

    def test_it_climbs_back_one_step_per_run_of_successes_and_never_faster_than_the_ramp(self):
        clock = Clock()
        gate = throttle.AdaptiveLimit(4, 20, clock, floor=1)

        async def call(ok=True):
            await gate.acquire()
            await gate.release(ok)

        async def go():
            await call(False)
            await call(False)
            self.assertEqual(gate.limit, 1)
            await call()
            self.assertEqual(gate.limit, 1, "too soon after the failure")
            clock.now = 25
            await call()
            self.assertEqual(gate.limit, 2)
            clock.now = 50
            await call()
            self.assertEqual(gate.limit, 2, "a level needs as many successes as itself")
            await call()
            self.assertEqual(gate.limit, 3)
            for t in (80, 81, 82, 110, 111, 112, 113, 140, 141, 142, 143):
                clock.now = t
                await call()
            self.assertEqual(gate.limit, 4, "never above the ceiling")
        run(go())

    def test_no_more_than_the_limit_run_at_once(self):
        gate = throttle.AdaptiveLimit(2, 0, Clock())
        peak = 0

        async def worker():
            nonlocal peak
            await gate.acquire()
            peak = max(peak, gate.busy)
            await asyncio.sleep(0.01)
            await gate.release(True)

        async def go():
            await asyncio.gather(*(worker() for _ in range(8)))
        run(go())
        self.assertEqual(peak, 2)


class TheTransport(unittest.TestCase):
    def setUp(self):
        self.gate = throttle.AdaptiveLimit(5, 0, Clock(), floor=1)
        throttle.reset()
        self.addCleanup(throttle.reset)

    def client(self, handler):
        transport = throttle.LimitedTransport(httpx.MockTransport(handler), adaptive=lambda: self.gate)
        return httpx.AsyncClient(transport=transport)

    def test_a_524_is_an_overload_and_gives_its_slot_back(self):
        async def go():
            async with self.client(lambda r: httpx.Response(524, text="A timeout occurred")) as c:
                r = await c.post("https://agentaus.example/v1/chat/completions", json={})
                self.assertEqual(r.status_code, 524)
        run(go())
        self.assertEqual((self.gate.limit, self.gate.busy), (2, 0))
        self.assertEqual(throttle.health().snapshot(2, 5)["n524_10m"], 1)

    def test_a_524_on_a_time_up_turn_is_not_the_upstreams_fault(self):
        async def go():
            async with self.client(lambda r: httpx.Response(524, text="A timeout occurred")) as c:
                throttle.REQUEST_KIND.set("time_up")
                try:
                    await c.post("https://agentaus.example/v1/chat/completions", json={})
                finally:
                    throttle.REQUEST_KIND.set("normal")
        run(go())
        self.assertEqual((self.gate.limit, self.gate.busy), (5, 0))
        snap = throttle.health().snapshot(5, 5)
        self.assertEqual((snap["fail_10m"], snap["n524_10m"]), (0, 0))

    def test_a_400_is_the_request_not_the_upstream(self):
        async def go():
            async with self.client(lambda r: httpx.Response(400, json={"error": "bad"})) as c:
                await c.post("https://agentaus.example/x", json={})
        run(go())
        self.assertEqual((self.gate.limit, self.gate.busy), (5, 0))

    def test_a_stream_holds_its_slot_until_it_is_closed(self):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: hi\n\n"

        async def go():
            async with self.client(lambda r: httpx.Response(200, stream=Body())) as c:
                async with c.stream("POST", "https://agentaus.example/x", json={}) as r:
                    self.assertEqual(self.gate.busy, 1)
                    async for _ in r.aiter_bytes():
                        pass
                self.assertEqual(self.gate.busy, 0)
        run(go())
        self.assertEqual(self.gate.limit, 5)

    def test_a_buffered_call_gives_its_slot_back(self):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"ok": true}'

        async def go():
            async with self.client(lambda r: httpx.Response(200, stream=Body())) as c:
                r = await c.post("https://agentaus.example/x", json={})
                self.assertEqual(r.json(), {"ok": True})
        run(go())
        self.assertEqual((self.gate.limit, self.gate.busy), (5, 0))

    def test_a_transport_error_is_an_overload(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        async def go():
            async with self.client(boom) as c:
                with self.assertRaises(httpx.ConnectError):
                    await c.post("https://agentaus.example/x", json={})
        run(go())
        self.assertEqual((self.gate.limit, self.gate.busy), (2, 0))

    def test_a_stream_that_goes_silent_frees_its_slot_at_the_idle_bound(self):
        # Headers, one chunk, then nothing: the slot must come back at the idle bound, not
        # after the 300 s read timeout. That is what made streamed helpers safe to turn on.
        class Silent(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: {}\n\n"
                await asyncio.sleep(3600)
                yield b""

        transport = throttle.LimitedTransport(
            httpx.MockTransport(lambda r: httpx.Response(200, stream=Silent())),
            adaptive=lambda: self.gate, idle=0.2)

        async def go():
            async with httpx.AsyncClient(transport=transport) as c:
                async with c.stream("POST", "https://agentaus.example/x", json={}) as r:
                    with self.assertRaises(httpx.ReadTimeout):
                        async for _ in r.aiter_bytes():
                            pass
            self.assertEqual(self.gate.busy, 0)
        import time
        started = time.monotonic()
        run(go())
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(throttle.health().snapshot(2, 5)["fail_10m"], 1)

    def test_a_402_means_the_upstream_is_down(self):
        async def go():
            async with self.client(lambda r: httpx.Response(402, text="quota")) as c:
                await c.post("https://agentaus.example/x", json={})
        run(go())
        self.assertTrue(throttle.health().down)
        self.assertEqual(self.gate.limit, 5, "quota is not load")

    def test_the_wait_for_a_slot_is_told_apart_from_the_upstreams(self):
        state = {"queued_at": None, "sent_at": None, "queued_s": 0.0}

        async def go():
            async with self.client(lambda r: httpx.Response(200, json={})) as c:
                throttle.WAIT_STATE.set(state)
                await c.post("https://agentaus.example/x", json={})
        run(go())
        self.assertIsNotNone(state["queued_at"])
        self.assertIsNotNone(state["sent_at"])


class TheHealth(unittest.TestCase):
    def test_gateway_failures_in_a_row_and_no_success_for_a_while_is_down(self):
        clock = Clock()
        health = throttle.Health(clock=clock, wall=lambda: 1000.0)
        for t in (10, 60, 130):
            clock.now = t
            health.record("fail", 524, 125, 4)
        self.assertTrue(health.down)
        self.assertEqual(health.snapshot(2, 10)["down_since"], 1000.0)
        clock.now = 430
        self.assertAlmostEqual(health.down_total(), 300)
        health.record("ok", 200, 13, 2)
        self.assertFalse(health.down)
        clock.now = 1000
        self.assertAlmostEqual(health.down_total(), 300, msg="only time down counts")

    def test_request_specific_failures_never_make_it_down(self):
        clock = Clock()
        health = throttle.Health(clock=clock)
        for t in (10, 60, 130, 200):
            clock.now = t
            health.record("fail", 524, 125, 4, kind="time_up")
        self.assertFalse(health.down)
        self.assertEqual(health.snapshot(4, 10)["fail_10m"], 0)

    def test_the_snapshot_has_the_contract_fields_and_ok_rate_by_width(self):
        clock = Clock()
        health = throttle.Health(clock=clock)
        health.record("ok", 200, 10, 2)
        health.record("ok", 200, 30, 4)
        health.record("fail", 524, 125, 4)
        snap = health.snapshot(2, 10)
        for field in ("ok_10m", "fail_10m", "n524_10m", "ok_rate_10m", "p50_s_10m", "limit",
                      "ceiling", "down", "down_since", "down_total_s"):
            self.assertIn(field, snap)
        self.assertEqual((snap["ok_10m"], snap["fail_10m"], snap["n524_10m"]), (2, 1, 1))
        self.assertAlmostEqual(snap["ok_rate_10m"], 0.667)
        self.assertEqual(snap["by_width_10m"], {"2": {"ok": 1, "fail": 0}, "4": {"ok": 1, "fail": 1}})
        clock.now = 700
        self.assertEqual(health.snapshot(2, 10)["ok_10m"], 0, "ten minutes, rolling")


if __name__ == "__main__":
    unittest.main()
