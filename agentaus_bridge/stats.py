"""Live in-memory counters for the /stats page.

Everything here counts events since the bridge started and resets when it restarts.
The values are recorded at the points in server.py that already log each event - the
bridge never parses its own log for these numbers.

The counts are deliberately coarsely bucketed (Agentaus vs Claude passthrough, and
errors by the failure class the operator can act on), and contain nothing per-request:
no prompts, no file contents, no keys.
"""

from __future__ import annotations

import threading

import agentaus_bridge as _root

VERSION = getattr(_root, "__version__", "unknown")

_STARTED = __import__("time").time()


class _Bucket:
    """Count / running total / slowest for one route."""

    __slots__ = ("count", "total", "slowest")

    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.slowest = 0.0

    def add(self, latency: float) -> None:
        self.count += 1
        self.total += latency
        if latency > self.slowest:
            self.slowest = latency


_lock = threading.Lock()
_requests = {"agentaus": _Bucket(), "anthropic": _Bucket()}
_input_tokens = 0
_output_tokens = 0
_compactions = 0
_compaction_saved = 0
_trims = 0
_trim_saved = 0
_errors = {"401": 0, "429": 0, "timeout": 0, "5xx": 0}


def record_request(
    route: str, latency: float, input_tokens: int = 0, output_tokens: int = 0
) -> None:
    """Record one completed request on `route` ("agentaus" or "anthropic")."""
    global _input_tokens, _output_tokens
    with _lock:
        bucket = _requests.get(route)
        if bucket is None:
            bucket = _Bucket()
            _requests[route] = bucket
        bucket.add(latency or 0.0)
        _input_tokens += int(input_tokens or 0)
        _output_tokens += int(output_tokens or 0)


def record_compact(kind: str, tokens_saved: int) -> None:
    """Record a summarising compaction ("compact") or a verbatim trim ("trim")."""
    global _compactions, _compaction_saved, _trims, _trim_saved
    with _lock:
        if kind == "trim":
            _trims += 1
            _trim_saved += int(tokens_saved or 0)
        else:
            _compactions += 1
            _compaction_saved += int(tokens_saved or 0)


def record_error(kind: str) -> None:
    """Record one error by bucket: "401", "429", "timeout" or "5xx"."""
    with _lock:
        if kind not in _errors:
            kind = "5xx"
        _errors[kind] += 1


def reset() -> None:
    """For tests: wipe every counter back to zero."""
    global _input_tokens, _output_tokens, _compactions, _compaction_saved
    global _trims, _trim_saved
    with _lock:
        for bucket in _requests.values():
            bucket.count = 0
            bucket.total = 0.0
            bucket.slowest = 0.0
        _input_tokens = 0
        _output_tokens = 0
        _compactions = 0
        _compaction_saved = 0
        _trims = 0
        _trim_saved = 0
        for key in _errors:
            _errors[key] = 0


def _fmt(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{seconds:.2f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"


def page_html() -> str:
    """Self-contained HTML for GET /stats: dark-mode readable, no external assets.

    Shows only counters - never prompts, file contents, keys or tokens. Auto-refreshes
    every 10 seconds against the same page.
    """
    s = snapshot()
    r = s["requests"]
    c = s["compaction"]
    e = s["errors"]

    def route_rows() -> str:
        rows = []
        for name, label in (("agentaus", "Agentaus"), ("anthropic", "Claude passthrough")):
            b = r.get(name) or {}
            avg = _fmt(b.get("average") or 0)
            slow = _fmt(b.get("slowest") or 0)
            rows.append(
                f"<tr><td>{label}</td><td>{b.get('count', 0)}</td>"
                f"<td>{avg}</td><td>{slow}</td></tr>"
            )
        return "\n".join(rows)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentaus bridge — stats</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 2rem auto; max-width: 60rem; padding: 0 1rem; background:#121212; color:#e6e6e6; }}
  h1 {{ font-size: 1.4rem; }}
  a {{ color:#7db4ff; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ text-align: left; padding: .5rem .75rem; border-bottom: 1px solid #333; }}
  th {{ color:#999; font-weight: 600; }}
  .muted {{ color:#999; }}
  .error {{ color:#ff7b7b; }}
</style>
</head>
<body>
<h1>Agentaus bridge stats</h1>
<p class="muted">Since the bridge started · version {s['version']} · up {_fmt(s["uptime"])}</p>
<h2>Requests</h2>
<table>
  <thead><tr><th>Route</th><th>Count</th><th>Average</th><th>Slowest</th></tr></thead>
  <tbody>{route_rows()}</tbody>
</table>
<h2>Tokens to Agentaus</h2>
<p>Input: {s["tokens"]["input"]:,} · Output: {s["tokens"]["output"]:,}</p>
<h2>Compaction</h2>
<p>Summarised: {c["count"]} · tokens saved: {c["tokens_saved"]:,}
<br>Trimmed: {c["trims"]} · tokens saved: {c["trim_tokens_saved"]:,}</p>
<h2>Errors</h2>
<p>401: {e["401"]} · 429: {e["429"]} · timeouts: {e["timeout"]} · 5xx: {e["5xx"]}</p>
<p class="muted">Auto-refreshes every 10 seconds.</p>
<script>
  setTimeout(() => {{ location.reload(); }}, 10000);
</script>
</body>
</html>"""


def snapshot() -> dict:
    """The counters for /stats, as JSON-serialisable plain data."""
    with _lock:
        requests = {
            route: {
                "count": bucket.count,
                "average": (bucket.total / bucket.count) if bucket.count else 0.0,
                "slowest": bucket.slowest,
            }
            for route, bucket in _requests.items()
        }
        return {
            "requests": requests,
            "tokens": {"input": _input_tokens, "output": _output_tokens},
            "compaction": {
                "count": _compactions,
                "tokens_saved": _compaction_saved,
                "trims": _trims,
                "trim_tokens_saved": _trim_saved,
            },
            "errors": dict(_errors),
            "uptime": __import__("time").time() - _STARTED,
            "version": VERSION,
        }
