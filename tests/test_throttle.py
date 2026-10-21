"""The adaptive limit on Agentaus calls: down to one on failure, back up one step at a time."""

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
    def test_a_failure_drops_it_to_one_call_at_a_time(self):
        gate = throttle.AdaptiveLimit(10, 20, Clock())

        async def go():
            await gate.acquire()
            await gate.release(False)
        run(go())
        self.assertEqual(gate.limit, 1)

    def test_it_climbs_back_one_step_per_run_of_successes_and_never_faster_than_the_ramp(self):
        clock = Clock()
        gate = throttle.AdaptiveLimit(4, 20, clock)

        async def call(ok=True):
            await gate.acquire()
            await gate.release(ok)

        async def go():
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
        self.gate = throttle.AdaptiveLimit(5, 0, Clock())

    def client(self, handler):
        transport = throttle.LimitedTransport(httpx.MockTransport(handler), adaptive=lambda: self.gate)
        return httpx.AsyncClient(transport=transport)

    def test_a_524_is_an_overload_and_gives_its_slot_back(self):
        async def go():
            async with self.client(lambda r: httpx.Response(524, text="A timeout occurred")) as c:
                r = await c.post("https://agentaus.example/v1/chat/completions", json={})
                self.assertEqual(r.status_code, 524)
        run(go())
        self.assertEqual((self.gate.limit, self.gate.busy), (1, 0))

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
        self.assertEqual((self.gate.limit, self.gate.busy), (1, 0))


if __name__ == "__main__":
    unittest.main()
