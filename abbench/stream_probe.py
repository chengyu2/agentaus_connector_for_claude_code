"""Run one real Claude Code turn and timestamp every stream event it receives."""
import json, subprocess, sys, tempfile, time
CC = "/Users/cheng/.vscode/extensions/anthropic.claude-code-2.1.278-darwin-arm64/resources/native-binary/claude"
port, prompt = sys.argv[1], sys.argv[2]
settings = json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                               "ANTHROPIC_CUSTOM_MODEL_OPTION": "agentaus"}})
wd = tempfile.mkdtemp(prefix="stream-probe-")
cmd = [CC, "-p", "--model", "agentaus", "--settings", settings, "--output-format", "stream-json",
       "--include-partial-messages", "--verbose", "--max-turns", "2", prompt]
t0 = time.monotonic()
import os
env = dict(os.environ, ANTHROPIC_CUSTOM_MODEL_OPTION="agentaus",
           ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}")
env.pop("ANTHROPIC_API_KEY", None)
p = subprocess.Popen(cmd, cwd=wd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
marks = []
for line in p.stdout:
    t = time.monotonic() - t0
    try: ev = json.loads(line)
    except ValueError: continue
    if ev.get("type") == "stream_event":
        e = ev["event"]; d = e.get("delta") or {}
        kind = d.get("type") or e.get("type")
        n = len(d.get("text") or d.get("thinking") or "")
        marks.append((t, kind, n))
    elif ev.get("type") == "result":
        result = ev
p.wait()
think = [m for m in marks if m[1] == "thinking_delta"]
text = [m for m in marks if m[1] == "text_delta"]
def span(ms): return f"{len(ms)} deltas, {sum(m[2] for m in ms)} chars, {ms[0][0]:.1f}s -> {ms[-1][0]:.1f}s" if ms else "none"
print(f"port {port}: thinking {span(think)} | text {span(text)} | done {time.monotonic()-t0:.1f}s")
# growth of visible characters over time, in 2s buckets
seen, out, b = 0, [], 2.0
for t, k, n in marks:
    if k in ("thinking_delta", "text_delta"):
        while t > b: out.append(seen); b += 2.0
        seen += n
out.append(seen)
print("   chars visible every 2s:", out)
print("   answer:", repr((result.get("result") or "")[:120]))
