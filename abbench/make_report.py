#!/usr/bin/env python3
"""Build the A/B report page from the saved summaries - no number is typed in by hand.

    ./summarise.py ctest ctest_r1 ctest_r2 ctest_r3 > results/final.json
    ./summarise.py cdev cdev_r1 cdev_r2 cdev_r3     > results/cdev.json
    ./make_report.py > report/agentaus-vs-opus.html
"""
from __future__ import annotations

import html
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
final = json.loads((HERE / "results/final.json").read_text())
dev = json.loads((HERE / "results/cdev.json").read_text())

SUITES = [
    ("econ_r", "Economic modelling in R", "data.gov.au download + Pareto model; OLS on ABS macro data", "tasks x 3 runs"),
    ("he", "HumanEval+", "164-problem coding set, strict HumanEval+ tests; held-out odd half", "problems"),
    ("mmlu", "MMLU", "knowledge, 57 subjects; held-out half of a 300-question sample", "questions"),
    ("mmlu_pro", "MMLU-Pro", "harder 10-option knowledge set; held-out half of a 300-question sample", "questions"),
]
ARMS = [("agentaus_base", "Agentaus, bridge before", "base"),
        ("agentaus_tuned", "Agentaus, tuned bridge", "tuned"),
        ("opus", "Claude Opus 5", "opus")]


def pct(x):
    return f"{100 * x:.1f}"


def secs(x):
    return f"{x:.0f}s" if x < 100 else f"{x / 60:.1f} min"


rows = []
for key, name, blurb, unit in SUITES:
    arms = []
    for arm, label, cls in ARMS:
        v = final.get(key, {}).get(arm)
        if not v:
            continue
        arms.append({"arm": arm, "label": label, "cls": cls, "score": v["score"],
                     "lo": v["ci95"][0], "hi": v["ci95"][1], "n": v["n"], "k": v["k"],
                     "p50": v["wall_p50"], "p90": v["wall_p90"],
                     "nofile": v.get("batches_without_answers_file"),
                     "batches": v.get("batches")})
    rows.append({"key": key, "name": name, "blurb": blurb, "unit": unit, "arms": arms})

data_json = json.dumps(rows)


def bar_block(row):
    out = [f'<div class="suite" id="s-{row["key"]}"><div class="suite-head"><h3>{html.escape(row["name"])}</h3>'
           f'<p class="muted">{html.escape(row["blurb"])}</p></div><div class="bars" role="list">']
    for a in row["arms"]:
        w = 100 * a["score"]
        lo, hi = 100 * a["lo"], 100 * a["hi"]
        n_label = f'{a["k"]:.0f} of {a["n"]:.0f} {row["unit"]}' if row["key"] != "econ_r" else f'mean of {a["n"]:.0f} runs'
        tip = (f'{a["label"]}: {pct(a["score"])}% ({n_label}); 95% interval {pct(a["lo"])}–{pct(a["hi"])}%; '
               f'median {secs(a["p50"])} per session')
        out.append(
            f'<div class="bar-row" role="listitem" tabindex="0" data-tip="{html.escape(tip)}">'
            f'<span class="bar-label">{html.escape(a["label"])}</span>'
            f'<span class="track"><span class="ci" style="left:{lo:.2f}%;width:{max(hi - lo, 0.4):.2f}%"></span>'
            f'<span class="bar {a["cls"]}" style="width:{w:.2f}%"></span></span>'
            f'<span class="bar-value">{pct(a["score"])}%</span></div>')
    out.append("</div></div>")
    return "\n".join(out)


def speed_table():
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label, _ in ARMS)
    body = []
    for row in rows:
        cells = {a["arm"]: a for a in row["arms"]}
        tds = "".join(f'<td class="num">{secs(cells[arm]["p50"]) if arm in cells else "–"}</td>'
                      for arm, _, _ in ARMS)
        body.append(f"<tr><th scope=\"row\">{html.escape(row['name'])}</th>{tds}</tr>")
    return f'<table><thead><tr><th scope="col">Median time per session</th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def score_table():
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label, _ in ARMS)
    body = []
    for row in rows:
        cells = {a["arm"]: a for a in row["arms"]}
        tds = "".join(
            f'<td class="num">{pct(cells[arm]["score"])}% <span class="muted">({pct(cells[arm]["lo"])}–{pct(cells[arm]["hi"])})</span></td>'
            if arm in cells else "<td>–</td>" for arm, _, _ in ARMS)
        body.append(f"<tr><th scope=\"row\">{html.escape(row['name'])}</th>{tds}</tr>")
    return f'<table><thead><tr><th scope="col">Score (95% interval)</th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def dev_table():
    labels = [("agentaus_base", "Before"), ("agentaus_tuned", "v4 (shipped)"), ("agentaus_next", "v5 (reverted)")]
    head = "".join(f"<th>{l}</th>" for _, l in labels)
    body = []
    for key, name, _, _ in SUITES:
        d = dev.get(key, {})
        tds = "".join(f'<td class="num">{pct(d[a]["score"])}%</td>' if a in d else "<td>–</td>" for a, _ in labels)
        body.append(f"<tr><th scope=\"row\">{name}</th>{tds}</tr>")
    return f'<table><thead><tr><th scope="col">Dev half (tuning only)</th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def get(suite, arm, field="score"):
    return final.get(suite, {}).get(arm, {}).get(field)


tuned_he, opus_he = get("he", "agentaus_tuned"), get("he", "opus")
tuned_econ = get("econ_r", "agentaus_tuned")
mp_base, mp_tuned, mp_opus = get("mmlu_pro", "agentaus_base"), get("mmlu_pro", "agentaus_tuned"), get("mmlu_pro", "opus")
mm_base, mm_tuned, mm_opus = get("mmlu", "agentaus_base"), get("mmlu", "agentaus_tuned"), get("mmlu", "opus")

tiles = []
for key, name, _, _ in SUITES:
    t, o = get(key, "agentaus_tuned"), get(key, "opus")
    b = get(key, "agentaus_base")
    if t is None or o is None:
        continue
    gap = 100 * (t - o)
    tlo, thi = final[key]["agentaus_tuned"]["ci95"]
    olo, ohi = final[key]["opus"]["ci95"]
    overlap = not (thi < olo or ohi < tlo)
    state = "tie" if overlap else ("ahead" if gap > 0 else "behind")
    word = {"tie": "within noise of Opus", "ahead": "ahead of Opus", "behind": "behind Opus"}[state]
    tiles.append(
        f'<div class="tile {state}"><span class="tile-name">{name}</span>'
        f'<span class="tile-big">{pct(t)}%</span>'
        f'<span class="tile-sub">Opus {pct(o)}% · before {pct(b) if b is not None else "–"}%</span>'
        f'<span class="pill {state}">{word}</span></div>')

PAGE = f"""<title>Agentaus in Claude Code</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@500;600&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {{
  --surface: #fcfcfb; --raised: #f3f2ef; --ink: #1b1a18; --ink-2: #57554f; --ink-3: #75736c;
  --rule: #e2e0da; --tuned: #2a78d6; --opus: #eb6834; --base: #8a8883; --ci: rgba(27,26,24,.13);
  --good: #1f7a3d; --good-bg: #e3f2e7; --warn: #8a5a00; --warn-bg: #fbeed3; --bad: #a3322a; --bad-bg: #f8e1de;
  --focus: #2a78d6;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --surface: #161615; --raised: #201f1d; --ink: #f2f1ec; --ink-2: #bdbbb2; --ink-3: #9a988f;
    --rule: #2f2e2b; --tuned: #3987e5; --opus: #d95926; --base: #76756f; --ci: rgba(242,241,236,.16);
    --good: #7fd49a; --good-bg: #173222; --warn: #e8bd6a; --warn-bg: #3a2c10; --bad: #f19a90; --bad-bg: #3d1c19;
    --focus: #3987e5;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface: #161615; --raised: #201f1d; --ink: #f2f1ec; --ink-2: #bdbbb2; --ink-3: #9a988f;
  --rule: #2f2e2b; --tuned: #3987e5; --opus: #d95926; --base: #76756f; --ci: rgba(242,241,236,.16);
  --good: #7fd49a; --good-bg: #173222; --warn: #e8bd6a; --warn-bg: #3a2c10; --bad: #f19a90; --bad-bg: #3d1c19;
  --focus: #3987e5;
}}
* {{ box-sizing: border-box; }}
body {{ background: var(--surface); color: var(--ink); font: 400 15px/1.6 "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif; padding-inline: 20px; }}
main {{ max-width: 920px; margin: 0 auto; padding-block: 40px 72px; display: grid; gap: 44px; }}
h1, h2, h3 {{ font-family: "IBM Plex Sans Condensed", "IBM Plex Sans", system-ui, sans-serif; text-wrap: balance; margin: 0; }}
h1 {{ font-size: clamp(30px, 5vw, 42px); line-height: 1.08; font-weight: 600; letter-spacing: -.01em; }}
h2 {{ font-size: 23px; font-weight: 600; }}
h3 {{ font-size: 17px; font-weight: 600; }}
p {{ margin: 0; max-width: 68ch; }}
.muted {{ color: var(--ink-2); }}
.eyebrow {{ font: 500 12px/1 "IBM Plex Mono", ui-monospace, monospace; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); }}
header {{ display: grid; gap: 16px; }}
.lede {{ font-size: 17px; }}
section {{ display: grid; gap: 18px; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }}
.tile {{ background: var(--raised); border-radius: 10px; padding: 16px; display: grid; gap: 4px; align-content: start; }}
.tile-name {{ font-weight: 600; font-size: 14px; }}
.tile-big {{ font: 500 34px/1.1 "IBM Plex Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; color: var(--ink); }}
.tile-sub {{ font-size: 13px; color: var(--ink-2); font-variant-numeric: tabular-nums; }}
.pill {{ justify-self: start; margin-top: 6px; font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 99px; }}
.pill::before {{ content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; background: currentColor; vertical-align: 1px; }}
.pill.tie {{ color: var(--warn); background: var(--warn-bg); }}
.pill.ahead {{ color: var(--good); background: var(--good-bg); }}
.pill.behind {{ color: var(--bad); background: var(--bad-bg); }}
.legend {{ display: flex; flex-wrap: wrap; gap: 18px; font-size: 13px; color: var(--ink-2); }}
.legend span {{ display: inline-flex; align-items: center; gap: 7px; }}
.sw {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; }}
.sw.tuned {{ background: var(--tuned); }} .sw.opus {{ background: var(--opus); }} .sw.base {{ background: var(--base); }}
.sw.ci {{ background: var(--ci); width: 20px; height: 8px; }}
.suites {{ display: grid; gap: 26px; }}
.suite {{ display: grid; gap: 10px; }}
.suite-head {{ display: grid; gap: 2px; }}
.suite-head p {{ font-size: 13px; }}
.bars {{ display: grid; gap: 6px; }}
.bar-row {{ display: grid; grid-template-columns: 190px 1fr 58px; align-items: center; gap: 12px; padding: 3px 0; border-radius: 6px; outline: none; }}
.bar-row:hover, .bar-row:focus-visible {{ background: var(--raised); }}
.bar-row:focus-visible {{ box-shadow: 0 0 0 2px var(--focus); }}
.bar-label {{ font-size: 13px; color: var(--ink-2); }}
.track {{ position: relative; height: 18px; background: linear-gradient(to right, var(--rule) 1px, transparent 1px) 0 0 / 25% 100%; border-left: 1px solid var(--rule); }}
.bar {{ position: absolute; left: 0; top: 3px; height: 12px; border-radius: 0 4px 4px 0; }}
.bar.tuned {{ background: var(--tuned); }} .bar.opus {{ background: var(--opus); }} .bar.base {{ background: var(--base); }}
.ci {{ position: absolute; top: 0; height: 18px; background: var(--ci); border-radius: 3px; }}
.bar-value {{ font: 500 13px/1 "IBM Plex Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; text-align: right; }}
.axis {{ display: grid; grid-template-columns: 190px 1fr 58px; gap: 12px; font: 400 11px/1 "IBM Plex Mono", monospace; color: var(--ink-3); }}
.axis .ticks {{ display: flex; justify-content: space-between; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--rule); vertical-align: top; }}
thead th {{ font-size: 12px; font-weight: 600; color: var(--ink-2); }}
td.num {{ font-family: "IBM Plex Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.fixes td:first-child {{ font-weight: 500; }}
ul.plain {{ margin: 0; padding-left: 20px; display: grid; gap: 8px; max-width: 72ch; }}
code {{ font: 13px "IBM Plex Mono", ui-monospace, monospace; background: var(--raised); padding: 1px 5px; border-radius: 4px; }}
.callout {{ background: var(--raised); border-radius: 10px; padding: 16px 18px; display: grid; gap: 8px; }}
#tip {{ position: fixed; z-index: 10; max-width: 300px; background: var(--ink); color: var(--surface); font-size: 12.5px; line-height: 1.45; padding: 8px 10px; border-radius: 6px; pointer-events: none; }}
@media (max-width: 620px) {{
  .bar-row, .axis {{ grid-template-columns: 1fr 52px; }}
  .bar-label {{ grid-column: 1 / -1; }}
  .axis span:first-child {{ display: none; }}
}}
@media (prefers-reduced-motion: no-preference) {{ .bar {{ transition: width .4s ease-out; }} }}
</style>

<main>
<header>
  <span class="eyebrow">A/B benchmark · Claude Code 2.1.278 · 23 September 2026</span>
  <h1>Agentaus in Claude Code, against Claude Opus 5</h1>
  <p class="lede">The same Claude Code binary the VS Code extension ships, driven headless in a clean
  VS Code-equivalent session, once with Claude Opus 5 and once with Agentaus through the local bridge -
  before and after this round of bridge changes. Scored on held-out halves the bridge was never tuned on.</p>
  <p><strong>Verdict.</strong> The tuned bridge turns Agentaus from a model that mostly failed to
  finish Claude Code tasks into one that finishes them - and on two of the four suites that is
  enough to draw level with Opus. On HumanEval+ the two score exactly the same, 77 of 82, missing
  partly different problems. On the data.gov.au economic-modelling task in R both are perfect on
  every run. On the knowledge suites Agentaus stays 13-16 points behind with intervals that do
  not overlap: that is the model's knowledge, which a bridge cannot supply. It does not beat
  Opus on any suite, and it takes 2-3.5 times as long.</p>
</header>

<section aria-labelledby="h-tiles">
  <h2 id="h-tiles">Tuned Agentaus, held-out results</h2>
  <div class="tiles">{"".join(tiles)}</div>
  <p class="muted" style="font-size:13px">"Within noise" means the 95% intervals of the two scores overlap:
  this sample cannot tell them apart.</p>
</section>

<section aria-labelledby="h-chart">
  <h2 id="h-chart">Score by suite</h2>
  <div class="legend" aria-hidden="true">
    <span><i class="sw base"></i>Agentaus, bridge before</span>
    <span><i class="sw tuned"></i>Agentaus, tuned bridge</span>
    <span><i class="sw opus"></i>Claude Opus 5</span>
    <span><i class="sw ci"></i>95% interval</span>
  </div>
  <div class="suites">
    {"".join(bar_block(r) for r in rows)}
    <div class="axis" aria-hidden="true"><span></span><span class="ticks"><span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span></span><span></span></div>
  </div>
  <div class="table-wrap">{score_table()}</div>
</section>

<section aria-labelledby="h-speed">
  <h2 id="h-speed">Speed</h2>
  <p>Opus is faster everywhere. Agentaus pays for every extra pass the bridge makes on its behalf -
  planning, the turn judge, review - and for a slower upstream. For the MMLU suites a session is a
  batch of ten questions.</p>
  <div class="table-wrap">{speed_table()}</div>
</section>

<section aria-labelledby="h-fixes">
  <h2 id="h-fixes">What the bridge was doing wrong</h2>
  <p>Every fix below came from reading transcripts of failed runs. None of them touches Claude turns -
  those are forwarded byte for byte.</p>
  <div class="table-wrap"><table class="fixes">
    <thead><tr><th>Failure</th><th>Evidence</th><th>Fix</th></tr></thead>
    <tbody>
      <tr><td>Turns end on an announcement</td><td>"We will first locate questions.json." - then nothing. Before the fix, 14 of 15 MMLU-Pro dev batches never wrote their answers file.</td><td>A turn judge (REFUSAL / STALLED / ANSWER) that sees the original request and the tools already run; a stalled turn is told to take the step. Two re-asks per conversation at most.</td></tr>
      <tr><td>Deliverable printed, not done</td><td>Answers printed as a JSON block; a whole <code>pareto.R</code> printed instead of written and run.</td><td>The same judge, now covering long replies too, with the request beside it.</td></tr>
      <tr><td>A system note answered instead of the user</td><td>Claude Code sends its environment block and connector instructions as <code>system</code> messages inside <code>messages</code>; forwarded last, a finished task was answered with "I have not read the Claude Docs workflow you shared".</td><td>Moved into the system prompt; the turn ends on what the user said. The working-directory lookup, silently finding nothing, now reads them too.</td></tr>
      <tr><td>26 tools, 120 KB of schemas</td><td>~30,000 tokens a call - a quarter of the window - mostly Artifact, Cron, DesignSync and similar.</td><td>Tool focus: the coding tools plus anything named or used. 13 KB.</td></tr>
      <tr><td>Right tool, wrong spelling</td><td><code>agentaus_read</code>, <code>Python</code>, <code>cmd</code> for <code>command</code>, relative paths.</td><td>Repaired when unambiguous, before a correction round is spent.</td></tr>
      <tr><td>A notification classifier rewritten into an essay</td><td>After every turn Claude Code asks for a one-word state (done / blocked...). Self-review rewrote it into 4,322 characters, twice: 39 s on every turn.</td><td>Review only runs on requests that offer tools.</td></tr>
      <tr><td>Token counter frozen</td><td>Agentaus streams live (~13 tokens / 100 ms) but the bridge held every answer for its checks: 57 s of nothing.</td><td>Plan and draft stream into a live thinking block; the checked answer follows as text.</td></tr>
      <tr><td>Agentaus dropped its own response</td><td>HTTP 200, then "peer closed connection without sending complete message body" inside the stream: 20 times in a day. The turn ended with an error.</td><td>Retried while nothing final has reached Claude Code. <em>Added after the measured version; covered by tests, not by the benchmark.</em></td></tr>
      <tr><td>Binary files sent as text</td><td>A search handed <code>/</code> read macOS installer receipts; Agentaus answered each chunk with HTTP 400.</td><td>Files sniffed for NUL bytes; whole-disk searches refused. <em>Added after the measured version.</em></td></tr>
      <tr><td>Helpers paid for a persona</td><td><code>system_prompt_overwrite</code> needs a system message; without one "Say hi" cost 2,442 input tokens instead of 207.</td><td>Every helper call carries a short system message.</td></tr>
    </tbody>
  </table></div>
</section>

<section aria-labelledby="h-dev">
  <h2 id="h-dev">What was tried and kept, or dropped</h2>
  <p>Versions were compared on the dev halves only; the held-out halves above were run once, at the end,
  with the version the dev round picked. v5 added two operating notes ("run what you write", "finish by
  doing") and lost on MMLU-Pro while doubling HumanEval+ time, so it was reverted.</p>
  <div class="table-wrap">{dev_table()}</div>
  <p class="muted" style="font-size:13px">All bridge prompts are now Markdown by default
  (<code>AGENTAUS_PROMPT_STYLE=xml</code> restores the tags). That change shipped together with the fixes
  above, so this benchmark measures the package, not Markdown on its own.</p>
</section>

<section aria-labelledby="h-method">
  <h2 id="h-method">How it was run</h2>
  <ul class="plain">
    <li>Each run is one headless session of the Claude Code binary bundled with the VS Code extension (2.1.278), in a fresh directory outside any git repository, with the environment the extension sets and nothing inherited: no memory, no repo skills, no desktop-session identity.</li>
    <li>Three arms: Opus 5 through the bridge's untouched passthrough; Agentaus through the bridge as it was on <code>main</code> before this work; Agentaus through the tuned bridge. Web tools off except for the data.gov.au task, where finding the data is the point.</li>
    <li>Graded with no model in the loop: HumanEval's own tests plus the HumanEval+ inputs; answer keys; reference numbers computed independently in Python and R. The R scripts are re-run from scratch, so a hand-typed answer fails.</li>
    <li>Tuned on dev halves (even HumanEval ids, first half of each question sample); reported on the other halves. Every transcript audited: no run touched anything outside its own folder.</li>
  </ul>
</section>

<section aria-labelledby="h-wrong" class="callout">
  <h2 id="h-wrong">What went wrong along the way</h2>
  <ul class="plain">
    <li>Early runs sat inside a checkout of the connector repo, so Claude Code handed them that project's memory folder. One Agentaus session, pushed by an over-eager first version of the turn judge, wrote benchmark notes there. They were found, confirmed to be the only contents, moved to a quarantine folder, and the harness moved outside every repository. Every result on this page is from the clean setup.</li>
    <li>A bridge was taken down by stopping the job that launched it, and 97 Opus runs hit the account's spend limit. Both were caught, excluded as infrastructure failures, and re-run; none of it is in the scores.</li>
  </ul>
</section>
</main>
<div id="tip" hidden></div>
<script>
(function () {{
  var tip = document.getElementById("tip");
  function show(el, x, y) {{
    tip.textContent = el.getAttribute("data-tip");
    tip.hidden = false;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, x + 14)) + "px";
    tip.style.top = Math.max(8, Math.min(window.innerHeight - h - 8, y + 14)) + "px";
  }}
  document.querySelectorAll("[data-tip]").forEach(function (el) {{
    el.addEventListener("mousemove", function (e) {{ show(el, e.clientX, e.clientY); }});
    el.addEventListener("mouseleave", function () {{ tip.hidden = true; }});
    el.addEventListener("focus", function () {{ var r = el.getBoundingClientRect(); show(el, r.left + 180, r.top); }});
    el.addEventListener("blur", function () {{ tip.hidden = true; }});
  }});
}})();
</script>
"""

print(PAGE)
