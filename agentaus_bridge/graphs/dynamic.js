export const meta = {
  name: 'agentaus-dynamic',
  description: 'Scout, plan twice, review the plan, then run it: each subtask as many solvers as its difficulty needs, merged and checked',
  phases: [
    { title: 'Scout' }, { title: 'Plan' }, { title: 'Review' }, { title: 'Solve' },
    { title: 'Merge' }, { title: 'Check' }, { title: 'Assemble' }, { title: 'Final check' },
  ],
}

// The dynamic agent graph. The shape of the work - how many subtasks, how hard each is, how
// many solvers it gets, whether it is checked, how answers are merged - is decided by
// planner agents, in Markdown, not by this script. This script is only the runtime that
// carries a plan out: concurrency, deadlines, backups, and passing each agent's Markdown to
// the next. Every agent works from a skill file (graphs/skills/*.md) the bridge passes in.
//
// args = { task, cwd, width, skills: { name: markdown }, and, prepared by the bridge before the
//          graph starts: pristine (a snapshot of the project), texts (script-made plain-text
//          copies of the documents), functions and constants (the code's inventory, with each
//          docstring verbatim), examples (a docstring-examples runner) }

const MARK = '[agentaus-graph]'
const THINK = '[think-only]'
const TASK = String((args && args.task) || '').trim()
const CWD = String((args && args.cwd) || '').trim()
const SKILLS = (args && args.skills) || {}
const SCRATCH = `/tmp/agentaus-graph/${(CWD.replace(/^\/+/, '') || 'task').replace(/[^\w.-]+/g, '_')}-dyn`
// Every copy is made from the project as it was when the work began. Measured: the scout,
// solvers and checkers edited the live project, so each later copy started from someone
// else's half-finished change and the last writer won.
const PRISTINE = String((args && args.pristine) || '').trim()
const BASE = PRISTINE || CWD
const TEXTS = (args && args.texts) || []
const FUNCTIONS = (args && args.functions) || []
const QUESTIONS = (args && args.questions) || []
const CONSTANTS = (args && args.constants) || []
const EXAMPLES = String((args && args.examples) || '').trim()

const WIDTH = Math.max(1, Number((args && args.width) || 8))
// An agent's deadline runs from when it gets a slot. The bridge asks an agent for its result,
// without tools, a little before this (args.deadline_ms is set from the bridge's own limit).
const DEADLINE_MS = Math.max(60000, Number((args && args.deadline_ms) || 840000))
// The budget for solving, merging and checking, counted from when solving starts.
const BUDGET_MS = Math.max(1000, Number((args && args.budget_ms) || 3600000))
// The whole graph's clock, from when the script starts: args.total_ms is the hard end the
// bridge sets (its own limit, minus the session's time already spent). At HARD the graph
// stops starting model work and only the closing steps run - putting decided work in place,
// the final check and a measured repair - each clipped; at END every wait stops and the
// result is returned. Measured (d13 c1913): twelve subtasks were decided and checked, two
// stragglers held the solve phase for 75 minutes, and the 7200 s kill came before Assemble -
// the project held none of it.
const TOTAL_MS = Math.max(120000, Number((args && args.total_ms) || 5400000))
const CLOSE_MS = Math.min(1500000, Math.max(Math.round(TOTAL_MS * 0.22), Math.min(300000, Math.round(TOTAL_MS * 0.4))))
const END_MS = TOTAL_MS - Math.min(120000, Math.round(TOTAL_MS * 0.03))
// A writer (assembler, fixer) gets the bridge's writer limit, then the give-up grace.
const WRITER_MS = Math.max(DEADLINE_MS, Number((args && args.writer_ms) || 1620000))
// How long a straggler past its deadline is still waited for. The bridge answers an agent this
// far past its limit with "(out of time)"; nothing is waited for longer.
const GRACE_MS = 300000
// An exact command's own limit: the bridge's Bash limit is 600 s.
const EXACT_MS = 720000
const SCOUT_MS = 360000
const MAX_SUBTASKS = 24
// The plan chooses solver counts; the runtime keeps the total within what the upstream can
// serve - about thirty calls a minute across every session.
// Measured (d9): 40 solvers on 6 slots kept every slot busy for an hour, and most answers
// came too late to count. Four solvers per slot.
const MAX_SOLVERS_TOTAL = Math.min(40, WIDTH * 4)

let late = false
let hard = false
let ended = false
let live = 0
let busy = 0
const waiting = []
// Once the budget is spent, no new solving, merging of values or checking starts. Code is
// still merged - by running the candidates - because without it the first solver wins blind.
const LATE_SKIPS = new Set(['Solve', 'Merge', 'Check'])
// Phases that still run after the hard stop: putting decided work in place, and the final check.
const CLOSING = new Set(['Assemble', 'Final check'])
// The harness runs at most min(16, CPUs - 2) agents at once, and an agent the graph stopped
// waiting for still counts there until it settles. Model agents leave room for exact commands.
const HCAP = Math.max(4, Number((args && args.harness_cap) || 14))
const HROOM = 3
const timers = []
function later(ms, fn) {
  const t = setTimeout(fn, ms)
  timers.push(t)
  return t
}
const sleep = ms => new Promise(r => later(Math.max(0, ms), () => r(null)))
let hardNow = () => {}
let endNow = () => {}
const hardStop = new Promise(r => { hardNow = r })
const endStop = new Promise(r => { endNow = r })
later(TOTAL_MS - CLOSE_MS, () => {
  hard = true
  late = true
  log('the graph\'s time is up: putting in place everything already decided')
  // Everyone waiting wakes: the rest give up, the closing phases go on.
  for (const w of waiting.splice(0)) w.resolve()
  hardNow(null)
})
later(END_MS, () => {
  ended = true
  for (const w of waiting.splice(0)) w.resolve()
  endNow(null)
})
function startBudget() {
  later(BUDGET_MS, () => {
    late = true
    for (const w of waiting.filter(w => LATE_SKIPS.has(w.ph))) w.resolve()
    waiting.splice(0, waiting.length, ...waiting.filter(w => !LATE_SKIPS.has(w.ph)))
  })
}
const stopped = ph => ended || (hard && !CLOSING.has(ph)) || (late && LATE_SKIPS.has(ph))

// ---------------------------------------------------------------------------------
// Runtime helpers
// ---------------------------------------------------------------------------------

function q(path) {
  return `'${String(path).replace(/'/g, "'\\''")}'`
}

// A slot for one model agent. `fn` gets `free`, which gives the slot back while the agent may
// still run: the graph frees the slot of every agent it stops waiting for. Measured (d13
// c1913): two solvers held 2 of 6 slots for 75 minutes, retrying every 5. After the hard stop
// the closing phases never wait for a slot.
async function slot(fn, ph, pri = 0) {
  while ((live >= WIDTH || busy >= HCAP - HROOM) && !(hard && CLOSING.has(ph))) {
    await new Promise(resolve => waiting.push({ resolve, ph, pri }))
    if (stopped(ph)) return null
  }
  if (stopped(ph)) return null
  live += 1
  let held = true
  const free = () => { if (held) { held = false; live -= 1; wake() } }
  try { return await fn(free) } finally { free() }
}
function wake() {
  let room = Math.max(1, Math.min(WIDTH - live, HCAP - HROOM - busy))
  while (waiting.length && room-- > 0) {
    // Lowest priority number first: solver 1 of every subtask runs before solver 2 of any,
    // so a budget cut still leaves every subtask with a result.
    let k = waiting.reduce((best, w, i) => (w.pri < waiting[best].pri ? i : best), 0)
    if (late) {
      const closing = waiting.findIndex(w => !LATE_SKIPS.has(w.ph))
      if (closing >= 0) k = closing
    }
    waiting.splice(k, 1)[0].resolve()
  }
}
// Every agent the graph starts is counted until it settles, whether or not the graph still waits.
function launch(body, opts) {
  busy += 1
  const a = Promise.resolve().then(() => agent(body, opts))
  a.then(() => { busy -= 1; wake() }, () => { busy -= 1; wake() })
  return a
}

const READ_ONLY = new Set(['scout', 'plan', 'review-plan', 'solve', 'merge', 'check'])
// An agent's prompt: the role's skill, then the job. Think-only agents reason over the
// Markdown they are handed and get no tools - one call, not a tool loop.
function prompt(skill, title, lines, think) {
  return [
    MARK, think ? THINK : null, `# ${title}`, '',
    think ? 'Everything you need is below. Reply with Markdown only.' : `**Project folder:** \`${CWD}\` - every input file is there.`,
    !think && READ_ONLY.has(skill) ? '**The project folder is read-only for you.** Never write, edit or patch anything in it - work in a copy under `/tmp`. Anything written there is undone before assembly.' : null,
    '', '---', `## Your skill: ${skill}`, '', (SKILLS[skill] || '').replace(/^---[\s\S]*?---\s*/, '').trim(), '---', '',
    ...lines.filter(l => l !== null && l !== undefined),
    '', 'Print only what you need while you work - counts, `head`, a few rows. Never a whole file or table.',
    'Your final message is your result, in the Markdown your skill describes.',
  ].filter(l => l !== null).join('\n')
}

const PLAN_SHAPED = /^\s*(i|we)('ll| will| am going to| are going to)\b|^\s*(let me|next,? i|first,? i)\b/i
// Measured (d9 tender): a merger replied "I'm sorry, but that request is too large and complex
// for me to handle in a single step", and that went into the deliverable as the answer to Q2.
// (d13 c1913): both split planners replied "I'm sorry ... can't fit ... in a single turn".
const REFUSAL = /^\W{0,3}(I[’']m sorry|I am sorry|sorry,|I can(?:no|[’'])t|I[’']m unable|I am unable)\b[\s\S]{0,600}?\b(too (large|complex|big)|single (step|turn|response|reply)|can(?:no|[’'])t fit|separate (actions|steps)|unable to (complete|do|help)|break (it|the task|this|the request) (in|down))/i
// The bridge answers an agent far past its limit with this, and it is no answer - not even
// from an exact command, whose shortest output is otherwise accepted.
const OUT_OF_TIME = /^\W*out of time\W*$/i
function usable(text, short) {
  const t = String(text || '').trim()
  if (t.length < 20 && OUT_OF_TIME.test(t)) return false
  return t.length >= (short ? 1 : 20) && !PLAN_SHAPED.test(t) && !REFUSAL.test(t)
}
// Plain keyboard characters in everything an agent hands on, so the text a checker or fixer
// quotes matches the file, which is normalised the same way.
const plain = s => String(s).replace(/[‐-‒−]/g, '-').replace(/[    ]/g, ' ').replace(/【([^】\n]{1,80})】/g, '[$1]')

// One agent with a backup: a dead, empty, plan-shaped or rejected reply is asked again,
// once. The deadline runs from when the agent gets its slot, not from when it was queued, and
// an attempt that runs past it stays in the race beside its backup: the first usable answer
// wins. Measured (d9): the deadline counted queue time, so with 40 solvers on 6 slots most
// "timed out" before they started, ran anyway, and 57 of 82 answers were thrown away - among
// them all three that fixed count_top.
// Every wait is bounded: past its deadline an attempt is waited for GRACE_MS more at most, and
// never past the graph's own clock. Measured (d13 c1913): a code solver's single attempt was
// awaited with no bound, ran 4503 s, and held its subtask - and the whole wave - until the kill.
// The slot of every attempt the graph stops waiting for is freed at once.
function firstUsable(runs, ok) {
  return new Promise(resolve => {
    let left = runs.length
    if (!left) resolve(null)
    const settle = out => {
      const t = typeof out === 'string' ? out : out ? JSON.stringify(out) : ''
      if (ok(t)) resolve(plain(t))
      else if (--left === 0) resolve(null)
    }
    for (const r of runs) r.then(settle, () => settle(null))
  })
}

// A backup still queued when an answer arrives never starts. Measured (d10): five queued
// backups ran 15-30 minutes after their first attempt had answered, in the same solver copy,
// and two of them reverted a correct fix. opts.once: no backup at all.
// opts.wait: a writer (assembler, fixer) is never raced against a backup of itself - a backup
// running beside a live first attempt wrote the same files at the same time; past the writer
// limit and the grace it is given up. opts.exact: an exact command, which the bridge runs
// without the model - it takes no slot and is never stopped by the budget.
async function ask(text, label, ph, accept, opts = {}) {
  const ok = t => usable(t, opts.short) && (!accept || accept(t))
  const runs = []
  const frees = []
  let answered = false
  const finish = out => { if (out) answered = true; for (const f of frees) f(); return out }
  // The graph's clock: model work stops at the hard stop, the closing steps at END. An exact
  // command is never cut by it - its own deadline bounds it.
  const clock = opts.exact ? new Promise(() => {}) : CLOSING.has(ph) ? endStop : Promise.race([hardStop, endStop])
  const giveUpOn = (opts.exact ? false : stopped(ph))
  if (giveUpOn) return null
  for (const attempt of [1, 2]) {
    if (attempt === 2 && (opts.once || (!opts.exact && stopped(ph)))) {
      // A straggler gets the grace, never an unbounded wait.
      return finish(runs.length ? await Promise.race([firstUsable(runs, ok), sleep(GRACE_MS), clock]) : null)
    }
    const body = attempt === 1 ? text
      : `${text}\n\n**Backup attempt:** the previous agent returned nothing usable or a plan instead of the result. Do the work now, then reply with the result.`
    let begin = () => {}
    const begun = new Promise(r => { begin = r })
    const call = () => launch(body, { label: attempt === 1 ? label : `${label}:backup`, phase: ph })
    if (opts.exact) { begin(); runs.push(call()) }
    else {
      runs.push(slot(free => {
        if (answered) return null
        frees.push(free)
        begin()
        return call()
      }, ph, (opts.pri || 0) + (attempt === 2 ? 10 : 0)))
    }
    const limit = opts.deadline || (opts.wait ? WRITER_MS + GRACE_MS : DEADLINE_MS)
    let timedOut = false
    const timeout = begun.then(() => sleep(limit)).then(() => { timedOut = true; return null })
    const out = await Promise.race([firstUsable(runs, ok), timeout, clock])
    if (out) return finish(out)
    if (ended || (!opts.exact && hard && !CLOSING.has(ph))) return finish(null)
    if (opts.wait && timedOut) return finish(null)
  }
  return finish(null)
}

// The headings agents write back. A section runs to the next of these at its level or above,
// not to the next heading of any kind: an answer holding its own `### Fee` or `## Q3` lost
// everything after it.
const KEYS = 'Answer|Key values|Evidence|Change|Tests|Why|Problem|Problems|Repro|Written|Fixed|Checklist|Verdict|Deliverables|Assembly|Plan|Brief|Subtask\\s*\\d+.*'
function section(md, heading) {
  const text = String(md || '')
  const m = text.match(new RegExp(`^(#{1,3})\\s*${heading}\\s*:?\\s*$`, 'im'))
  if (!m) return ''
  const rest = text.slice(m.index + m[0].length)
  const end = rest.search(new RegExp(`^#{1,${m[1].length}}\\s*(${KEYS})\\s*:?\\s*$`, 'im'))
  return (end >= 0 ? rest.slice(0, end) : rest).trim()
}

// Markdown handed on to another agent sits below the prompt's own outline: its headings are
// pushed down so they start at `top`, and none of them reads as part of the prompt.
function nest(md, top) {
  const lines = String(md || '').split('\n')
  let fence = false
  const levels = []
  for (const l of lines) {
    if (/^\s*(```|~~~)/.test(l)) fence = !fence
    else if (!fence && /^#{1,6}\s/.test(l)) levels.push(l.match(/^#+/)[0].length)
  }
  const shift = levels.length ? Math.max(0, top - Math.min(...levels)) : 0
  fence = false
  return lines.map(l => {
    if (/^\s*(```|~~~)/.test(l)) { fence = !fence; return l }
    if (fence || !shift || !/^#{1,6}\s/.test(l)) return l
    const level = l.match(/^#+/)[0].length + shift
    const text = l.replace(/^#+\s*/, '')
    return level <= 6 ? `${'#'.repeat(level)} ${text}` : `**${text}**`
  }).join('\n').trim()
}

// The values a result settles, each as `- **name:** value`.
function keyValues(md) {
  return section(md, 'Key values').split('\n').map(l => l.trim()).filter(l => /^[-*]\s+\S/.test(l))
    .map(l => {
      const body = l.replace(/^[-*]\s+/, '').replace(/\*\*/g, '')
      const m = body.match(/^([^:]{1,80}):\s*(.+)$/)
      return m ? `- **${m[1].trim()}:** ${m[2].trim()}` : `- ${body}`
    })
}

// One result as another agent sees it: the answer first, its key values in bold under their
// own heading, the change it saved, and - only where the reader judges the work - the evidence.
// Measured: values buried in a long result were missed by the assembler and recomputed,
// wrongly, by the final checkers.
function card(title, md, opts = {}) {
  const raw = String(md || '').trim()
  const top = opts.top || 3
  const h = n => '#'.repeat(Math.min(6, top + n))
  if (!raw) return `${h(0)} ${title}\n\n**No result** - this part was not settled.`
  const answer = section(raw, 'Answer')
  const keys = keyValues(raw)
  const change = section(raw, 'Change')
  const tests = section(raw, 'Tests')
  const evidence = section(raw, 'Evidence') || section(raw, 'Why')
  return [
    `${h(0)} ${title}`,
    opts.status ? `**Check:** ${opts.status}` : null,
    '', `${h(1)} Answer`, nest(answer || raw, top + 2),
    keys.length ? `\n${h(1)} Key values - carry these exactly\n${keys.join('\n')}` : null,
    change && !/^none\.?$/i.test(change) ? `\n${h(1)} Change\n**Saved at:** ${change}` : null,
    opts.evidence && tests && !/^none\.?$/i.test(tests) ? `\n${h(1)} Tests\n${tests}` : null,
    opts.evidence && evidence && answer ? `\n${h(1)} Evidence\n${nest(evidence, top + 2)}` : null,
  ].filter(l => l !== null).join('\n')
}

// A result must show its work, and must not claim the inputs are missing - that was always
// written without looking. Only a claim about the files themselves: "the documents do not
// state the fee" or "not found in the RFQ files" is an honest answer. Measured: the wider
// pattern (any file word within 80 characters of "not found") rejected those.
const FILES_MISSING = /\bno such file\b|\b(the )?(input )?(files?|directory|folder|xlsx|csv|docx|pdfs?)\b[^.\n]{0,30}\b(are|is|were|was) (missing|absent|not present|empty)\b|\b(files?|directory|folder)\b[^.\n]{0,30}\bdo(es)? not exist\b/i
const shown = md => section(md, 'Evidence').length >= 20 && !FILES_MISSING.test(section(md, 'Answer'))

// ---------------------------------------------------------------------------------
// Reading a Markdown plan
// ---------------------------------------------------------------------------------

function parsePlan(md) {
  const text = String(md || '')
  // One deliverable per bullet: its first backticked token, and only if it looks like a path.
  // A bullet's description often quotes `## Qn` or `[proposal]` too, and each was read as a
  // file to write - then failed as missing.
  const deliverables = section(text, 'Deliverables').split('\n')
    .map(line => (line.match(/^\s*[-*]\s*`([^`]+)`/) || [])[1])
    .filter(p => p && /^[\w./~-][\w ./~-]*\.\w{1,5}$/.test(p.trim()) && !/^\[|^#/.test(p))
    .map(p => p.trim().replace(/^\.\//, ''))
  const subtasks = []
  // `## Subtask tools/bank.py`, with no number, is a subtask too - one was skipped, and the
  // plan ran nothing.
  const re = /^##\s*Subtask\b\s*(\d+)?\s*[:.\-]?\s*(.*)$/gim
  const heads = [...text.matchAll(re)].map((h, k) => { const c = [...h]; c.index = h.index; if (!c[1]) c[1] = String(90 + k); return c })
  heads.forEach((h, k) => {
    const start = h.index + h[0].length
    const end = k + 1 < heads.length ? heads[k + 1].index : (text.search(/^##\s*Assembly\s*$/im) > start ? text.search(/^##\s*Assembly\s*$/im) : text.length)
    const body = text.slice(start, end)
    const field = name => ((body.match(new RegExp(`^\\s*[-*]\\s*\\**${name}\\**\\s*:\\s*(.+)$`, 'im')) || [])[1] || '').trim()
    const difficulty = (field('Difficulty').toLowerCase().match(/easy|medium|hard/) || ['medium'])[0]
    const solvers = Math.min(5, Math.max(1, parseInt(field('Solvers'), 10) || { easy: 1, medium: 2, hard: 3 }[difficulty]))
    const task = ((body.split(/^###\s*Task\s*$/im)[1]) || '').trim() || h[2].trim()
    const deps = (field('Depends on').match(/\d+/g) || []).map(Number).filter(d => d !== Number(h[1]))
    subtasks.push({
      n: Number(h[1]), title: h[2].trim() || `Subtask ${h[1]}`, difficulty, solvers,
      check: !/^no\b/i.test(field('Check')),
      merge: (field('Merge').toLowerCase().match(/vote|best-evidence|combine|union|pick-change/) || ['best-evidence'])[0],
      kind: (field('Kind').toLowerCase().match(/value|text|code/) || ['value'])[0],
      deps, task,
    })
  })
  // A number used twice is two subtasks: the second gets a fresh number. Sharing one, they
  // shared a result and a solver folder.
  const seen = new Set()
  let spare = Math.max(0, ...subtasks.map(st => st.n)) + 1
  for (const st of subtasks) {
    if (!seen.has(st.n)) { seen.add(st.n); continue }
    const old = st.n
    st.n = spare++
    seen.add(st.n)
    st.deps = st.deps.filter(d => d !== old)
  }
  return { deliverables, subtasks: subtasks.slice(0, MAX_SUBTASKS), assembly: section(text, 'Assembly'), markdown: text }
}

const planOk = md => parsePlan(md).subtasks.length > 0

// ---------------------------------------------------------------------------------
// 1. Scout
// ---------------------------------------------------------------------------------

phase('Scout')
// What a script already knows about the project, exactly: the document copies and the code's
// functions. Measured: a scout typed a "copy" of a proposal by hand, cut it at 30 lines and
// added a total the document does not have; planners named functions `function_1` to `function_10`.
const INVENTORY = [
  TEXTS.length ? ['## Plain-text copies of the documents - made by a script, complete, tables included',
    ...TEXTS.map(t => `- \`${t.source}\` -> \`${t.text}\` (${t.lines} lines)`)].join('\n') : null,
  QUESTIONS.length ? ['## The questions the request must answer - copied by a script, word for word',
    ...QUESTIONS.map(x => `- Q${x.n} (\`${x.file}\` line ${x.line}): ${x.text}`)].join('\n') : null,
  TEXTS.length ? 'A file with no copy listed here (`.md`, `.txt`, `.csv`, code) is plain text already: read it at its own path.' : null,
  FUNCTIONS.length ? ['## Functions in the code - exact names',
    ...FUNCTIONS.slice(0, 100).map(f => `- \`${f.fn}\` in \`${f.file}\` line ${f.line}${f.stub ? ' - **a stub, to implement**' : ''}`)].join('\n') : null,
  CONSTANTS.length ? ['## Module-level tables and constants',
    ...CONSTANTS.map(c => `- \`${c.file}\`: ${c.names.slice(0, 30).map(n => `\`${n}\``).join(', ')}`)].join('\n') : null,
].filter(Boolean).join('\n\n')
const scouted = await ask(prompt('scout', 'Survey the project', [
  TEXTS.length ? '**The documents already have plain-text copies** (listed below). Read those; never write or rewrite them.' : `**Scratch folder for plain-text copies:** \`${SCRATCH}/inputs\` (make it first).`, '',
  '## The request - for other agents; you only survey', '```text', TASK, '```', '',
  INVENTORY || null,
]), 'scout', 'Scout', null, { deadline: SCOUT_MS }) || '# Brief\n\n(The scout returned nothing. Read the files yourself.)'
const brief = INVENTORY ? `${scouted}\n\n${nest(INVENTORY, 2)}` : scouted

// ---------------------------------------------------------------------------------
// 2. Plan - two planners with different stances, then a reviewer writes the plan
// ---------------------------------------------------------------------------------

phase('Plan')
const STANCES = [
  'Lean: as few subtasks and solvers as will get every output right.',
  'Careful: find every output and every trap, and give the risky ones more solvers and a check.',
]
const plans = (await parallel(STANCES.map((stance, i) => () => ask(prompt('plan', `Plan the work (${i === 0 ? 'lean' : 'careful'})`, [
  `**Your stance:** ${stance}`, '',
  '## The request', '```text', TASK, '```', '',
  '## The brief from the scout', brief,
], true), `plan-${i + 1}`, 'Plan', planOk)))).filter(Boolean)

phase('Review')
let planMd = plans.length > 1
  ? await ask(prompt('review-plan', 'Write the plan the work will follow', [
      '## The request', '```text', TASK, '```', '',
      '## The brief from the scout', brief, '',
      '## Plan A', plans[0], '', '## Plan B', plans[1],
    ], true), 'review-plan', 'Review', planOk)
  : null
planMd = planMd || plans.find(planOk) || null
let plan = planMd ? parsePlan(planMd) : null
if (!plan || !plan.subtasks.length) {
  // No plan could be read: one hard subtask, the whole request, solved three ways.
  plan = { deliverables: [], subtasks: [{ n: 1, title: 'The whole request', difficulty: 'hard', solvers: 3, check: true,
    merge: 'best-evidence', kind: 'text', deps: [], task: TASK }], assembly: 'Write every deliverable the request asks for.', markdown: '' }
  log('no plan could be read: running the whole request as one hard subtask')
}
// The planner's choices, held to a floor. Measured: planners marked subtasks "medium" and
// gave them one solver, leaving nothing to compare; they also kept step subtasks ("load and
// normalise the claims") whose output - a 13,000-row table - cannot be passed on, and
// "assemble response.md" subtasks that raced the assembler.
const OUTPUT_MARK = /`[^`]+`|\bQ\d{1,2}\b|\b[a-z]\w*_\w+\b/i
const STEP = /^(load|read|parse|normali[sz]e|prepare|gather (data|inputs|sources)|collect|derive|build|assemble|write|compile|produce|create)\b|cross[- ]?check|verify all|summary of findings|^`?(bug[-_ ‐-―]?fix(es)?[\s_]+)?summary\b|^verif(y|ication)\b/i
{
  const dropped = new Set()
  const kept = plan.subtasks.filter(st => {
    const step = STEP.test(st.title.replace(/`/g, '')) && !OUTPUT_MARK.test(st.title.replace(/^(write|produce|create)\s+`[^`]+`$/i, ''))
    const deliverableWriter = /^(assemble|write|compile|produce|create)\b/i.test(st.title) && plan.deliverables.some(d => st.title.includes(d.split('/').pop()))
    if ((step || deliverableWriter) && plan.subtasks.length > 1) { dropped.add(st.n); return false }
    return true
  })
  if (kept.length) {
    for (const st of kept) st.deps = st.deps.filter(d => !dropped.has(d))
    plan.subtasks = kept
  }
  if (dropped.size) log(`plan: dropped step/assembly subtask(s) ${[...dropped].join(', ')}`)
  for (const st of plan.subtasks) {
    const floor = { easy: 1, medium: 2, hard: 3 }[st.difficulty] || 2
    st.solvers = Math.max(st.solvers, floor)
    if (st.difficulty !== 'easy') st.check = true
    if (st.kind === 'code' && st.merge !== 'pick-change') st.merge = 'pick-change'
  }
}

// Keep the total within what the upstream can serve, trimming the most generous first.
let total = plan.subtasks.reduce((n, s) => n + s.solvers, 0)
while (total > MAX_SOLVERS_TOTAL) {
  const most = plan.subtasks.slice().sort((a, b) => b.solvers - a.solvers)[0]
  if (most.solvers <= 1) break
  most.solvers -= 1
  total -= 1
}
log(`plan: ${plan.subtasks.length} subtask(s), ${total} solver(s): ${plan.subtasks.map(s => `${s.n}:${s.difficulty}x${s.solvers}`).join(' ')}`)

// ---------------------------------------------------------------------------------
// 3-5. Run the plan: solve, merge, check - each subtask once its dependencies are done
// ---------------------------------------------------------------------------------

// Code subtasks that are one named Python function work on that function alone: a solver
// saves just its function, a merger tests every candidate, and the assembler swaps the chosen
// one in by name. Ten diffs against stubs in one file collided at assembly before.
const AST_FIND = "n=[n for n in ast.parse(code).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn][0]; s=(n.decorator_list[0].lineno if n.decorator_list else n.lineno)-1"
function extractCommand(repoFile, fn, out) {
  return `python3 - ${q(repoFile)} ${fn} ${q(out)} <<'PY'\nimport ast,sys\nf,fn,out=sys.argv[1:4]; code=open(f).read(); lines=code.splitlines(keepends=True)\n${AST_FIND}\nopen(out,'w').write(''.join(lines[s:n.end_lineno])); print(open(out).read()[:3000])\nPY`
}
function replaceCommand(target, fn, src) {
  return `python3 - ${q(target)} ${fn} ${q(src)} <<'PY'\nimport ast,sys\nt,fn,src=sys.argv[1:4]; code=open(t).read(); lines=code.splitlines(keepends=True); new=open(src).read().rstrip(chr(10))+chr(10)\ntry:\n    m=ast.parse(new); cs=[x for x in m.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name==fn]\n    if cs and len(m.body)>1:\n        sl=new.splitlines(keepends=True); c0=cs[0]; a=(c0.decorator_list[0].lineno if c0.decorator_list else c0.lineno)-1; new=''.join(sl[a:c0.end_lineno]).rstrip(chr(10))+chr(10)\nexcept Exception:\n    pass\nfound=[n for n in ast.parse(code).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn]\nif found:\n    n=found[0]; s=(n.decorator_list[0].lineno if n.decorator_list else n.lineno)-1\n    try:\n        c=[x for x in ast.parse(new).body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name==fn][0]; d=ast.get_docstring(c) is not None; body=c.body[1:] if d else c.body\n        if ast.get_docstring(n) is not None and body and body[0].lineno>(c.body[0].end_lineno if d else c.lineno):\n            new=''.join(lines[s:n.body[0].end_lineno])+''.join(new.splitlines(keepends=True)[body[0].lineno-1:c.end_lineno])\n    except Exception:\n        pass\n    lines[s:n.end_lineno]=[new]\nelse:\n    lines.append(chr(10)+chr(10)+new)\nopen(t,'w').write(''.join(lines)); print('replaced' if found else 'appended', fn)\nPY`
}
function functionOf(st) {
  if (st.kind !== 'code') return null
  // A feature or command-line change is a whole-copy change. Measured (d12): "Implement
  // `--json`" named `lead_domain` as an output key, was bound to it, lost its `__main__`
  // change, and its untouched `lead_domain` put the bug back over the fix.
  if (/(?<![\w-])--[a-z][\w-]*|__main__|command[- ]line|\bCLI\b|^\W*Feature\b/i.test(st.title)) return null
  const text = `${st.title}\n${st.task}`
  // A constant (`SPELLING`) is not a function, and a name the code does not define is not one.
  // The function a subtask is about: a backticked name in its title, else in its task, else a
  // `name(` or a snake_case word in its title, or a title that is the name. Never a plain word:
  // measured (d10), "top-five" bound a subtask to `top`, and "first name" to `first`. And
  // (d9) a reviewer wrote `## Subtask 1: is_bored` without backticks.
  const inv = n => !/^[A-Z][A-Z0-9_]*$/.test(n) && (!FUNCTIONS.length || FUNCTIONS.some(f => f.fn === n))
  const tick = t => [...new Set((t.match(/`([A-Za-z_]\w*)(?:\(\))?`/g) || []).map(n => n.replace(/[`()]/g, '')))].filter(inv)
  let names = tick(st.title)
  if (!names.length) names = tick(st.task)
  if (!names.length) {
    const bare = (st.title.match(/\b[A-Za-z_]\w*\b/g) || []).filter(n => FUNCTIONS.some(f => f.fn === n))
    const lead = (st.title.trim().match(/^(?:Audit\s+)?([A-Za-z_]\w*)\b/) || [])[1]
    names = [...new Set(bare.filter(n => n.includes('_') || n === lead || new RegExp(`\\b${n}\\(`).test(st.title)))].filter(inv)
  }
  const known = FUNCTIONS.length ? names.filter(n => FUNCTIONS.some(f => f.fn === n)) : names
  const fn = known.length === 1 ? known[0] : null
  if (!fn) return null
  const found = FUNCTIONS.filter(f => f.fn === fn)
  if (found.length === 1) return { fn, file: found[0].file }
  const pys = plan.deliverables.filter(d => /\.py$/.test(d)).map(d => d.replace(CWD + '/', ''))
  const named = (text.match(/[\w./-]+\.py\b/g) || []).map(f => f.replace(CWD + '/', '').replace(/^\.\//, ''))
  const file = named.find(f => pys.includes(f)) || (pys.length === 1 ? pys[0] : named.find(f => !/test/i.test(f)))
  return file ? { fn, file } : null
}

// Where the function's own spec is the whole job - a stub to implement, or an audit - the
// planner's paraphrase is left out. Measured (d9, d10): the planner's "use absolute value"
// was the rule every order_by_points solver followed, against the docstring's own example.
function specOnly(st) {
  const f = functionOf(st)
  const inv = f && FUNCTIONS.find(x => x.fn === f.fn && x.file === f.file)
  return !!(inv && inv.doc && (inv.stub || /^\W*Audit\b/i.test(st.title)))
}

// A function's specification, verbatim from the original file.
function specOf(st) {
  const f = functionOf(st)
  const inv = f && FUNCTIONS.find(x => x.fn === f.fn && x.file === f.file)
  if (!inv) return null
  return [`## The spec - \`${inv.fn}\` in \`${inv.file}\`, verbatim from the original file`,
    'Where the subtask\'s wording disagrees with this, **this wins**. Its examples are tests your change must pass.', '',
    '```python', inv.signature, inv.doc ? `    """${inv.doc}"""` : '    (no docstring - judge it by its name, its callers and the README)', '```'].join('\n')
}

// Deliverables the request never named are dropped, with the subtasks that only write them:
// planners kept adding a `bug_fixes.txt` nobody asked for.
{
  const named = d => TASK.includes(d.split('/').pop())
  const invented = plan.deliverables.filter(d => !named(d))
  if (invented.length && plan.deliverables.some(named)) {
    plan.deliverables = plan.deliverables.filter(named)
    const only = st => invented.some(d => st.title.includes(d.split('/').pop())) && !plan.deliverables.some(d => st.title.includes(d.split('/').pop()))
    const gone = new Set(plan.subtasks.filter(only).map(st => st.n))
    plan.subtasks = plan.subtasks.filter(st => !gone.has(st.n))
    for (const st of plan.subtasks) st.deps = st.deps.filter(d => !gone.has(d))
    log(`plan: dropped invented deliverable(s) ${invented.join(', ')}`)
  }
  if (!plan.subtasks.length) {
    const alt = plans.map(parsePlan).find(p => p.subtasks.length)
    plan.subtasks = alt ? alt.subtasks : [{ n: 1, title: 'The whole request', difficulty: 'hard', solvers: 3, check: true,
      merge: FUNCTIONS.length ? 'pick-change' : 'best-evidence', kind: FUNCTIONS.length ? 'code' : 'text', deps: [], task: TASK }]
    log('plan: no subtask left after the filters - fell back')
  }
}

// Whether the request says there may be problems nobody reported.
const AUDITING = /\b(more|other|further|any|every|all|hidden|unreported)\b[^.\n]{0,40}\b(bugs?|problems?|issues?|errors?|defects?)\b|\baudit\b/i.test(TASK)
// Whole-file subtasks no planner could split: their symptoms are handed to the Audits.
const unsplit = []
// A sentence that only asks for an audit or names a flag is no symptom (flags get a Feature).
const AUDITING_ONLY = /^\W*(there may be|also|please|and)\b|\b(more|other|further|hidden|unreported) (bugs?|problems?|issues?)\b|(?<![\w-])--[a-z]/i
// A code subtask that is a whole file - "`tools/bank.py`", fix everything - is split by one
// more planner into one subtask per reported symptom and per function to audit. Two rounds
// of planners kept producing it whatever their skill said.
phase('Review')
{
  const wholeFile = st => st.kind === 'code' && !functionOf(st) && (/\.\w{1,4}`?\s*$|^`?[\w./-]+\.\w{1,4}`?$/.test(st.title.trim())
    || /\b(whole|entire|all|every|audit(ed)?|bug[- ‐-―]?fix(ed)?|implementation)\b[^\n]{0,60}[\w./-]+\.(py|js|ts)\b|[\w./-]+\.(py|js|ts)\b[^\n]{0,60}\b(audit(ed)?|bug[- ‐-―]?fix(ed)?|implementation)\b/i.test(st.title))
  const targets = plan.subtasks.filter(wholeFile)
  for (const st of targets) {
    const md = await ask(prompt('plan', `Split the code subtask "${st.title}"`, [
      'This subtask covers a whole file. Split it into subtasks in the plan format: one per reported symptom, and - if the request says there may be more problems - one `Audit \\`function\\`` subtask per function in the file. Each is `Kind: code`, at least `Difficulty: medium`, `Merge: pick-change`, and its Task names the function and the file.',
      'Write only the `## Subtask N: ...` sections, numbered from 1.', '',
      '## The request', '```text', TASK, '```', '', '## The subtask', st.task, '', '## The brief from the scout', brief,
    ], true), `split-${st.n}`, 'Review', md => parsePlan(md).subtasks.length > 1)
      // A refused split is asked once more, smaller: the request and the file's functions,
      // without the brief. Measured (d13 c1913): both splitters replied "I'm sorry ... can't
      // fit", the whole file went to five solvers for up to 1042 s and a merger for 833 s.
      || await ask(prompt('plan', `Split the code subtask "${st.title}" (short)`, [
        'Split this code subtask into subtasks in the plan format: one per symptom the request reports' + (AUDITING ? ', and one `Audit \\`function\\`` subtask per function below' : '') + '. Each is `Kind: code`, `Difficulty: medium`, `Merge: pick-change`, and its Task names the function and the file. Write only the `## Subtask N: ...` sections, numbered from 1.', '',
        '## The request', '```text', TASK, '```', '',
        FUNCTIONS.length ? ['## The functions', ...FUNCTIONS.slice(0, 60).map(f => `- \`${f.fn}\` in \`${f.file}\``)].join('\n') : null,
      ], true), `split-${st.n}.short`, 'Review', md => parsePlan(md).subtasks.length > 1)
    if (!md) {
      // Still refused. When the request asks for an audit, the coverage pass below gives every
      // function its own Audit, so the whole file is dropped and its symptoms go to the Audits
      // they name; otherwise it stays - it may be the only subtask there is.
      if (AUDITING && FUNCTIONS.length) {
        unsplit.push(st)
        plan.subtasks = plan.subtasks.filter(o => o !== st)
        for (const o of plan.subtasks) o.deps = o.deps.filter(d => d !== st.n)
        log(`plan: subtask ${st.n} could not be split - its symptoms go to the Audits of the functions they name`)
      }
      continue
    }
    const parts = parsePlan(md).subtasks
    const base = Math.max(...plan.subtasks.map(o => o.n)) + 1
    parts.forEach((p, k) => { p.n = base + k; p.deps = []; p.kind = 'code' })
    plan.subtasks = plan.subtasks.filter(o => o !== st).concat(parts)
    for (const o of plan.subtasks) o.deps = o.deps.flatMap(d => d === st.n ? parts.map(p => p.n) : [d])
    log(`plan: split subtask ${st.n} into ${parts.length}`)
  }
}

// The plan covers the code's inventory: every stub is implemented, and - when the request
// says there may be more problems - every function in the files being fixed is audited, plus
// the module-level tables. Measured: the one bug no symptom pointed to (`count_top`, `<` for
// `<=`) was found whenever a solver was pointed at its function, and almost never otherwise.
{
  let next = Math.max(0, ...plan.subtasks.map(o => o.n)) + 1
  const covered = fn => plan.subtasks.some(st => { const f = functionOf(st); return f && f.fn === fn })
  // "function 3" or `function_3` is the third function in the code.
  for (const st of plan.subtasks) {
    const m = st.kind === 'code' && !functionOf(st) && st.title.match(/\bfunction[ _]?(\d{1,2})\b/i)
    const f = m && FUNCTIONS[Number(m[1]) - 1]
    if (f && !covered(f.fn)) { st.title = `\`${f.fn}\` in \`${f.file}\``; st.task = `Implement \`${f.fn}\` in \`${f.file}\` to its docstring.\n\n${st.task}` }
  }
  const added = []
  for (const f of FUNCTIONS.filter(f => f.stub && !covered(f.fn))) {
    added.push({ n: next++, title: `\`${f.fn}\` in \`${f.file}\``, difficulty: 'medium', solvers: 2, check: true, merge: 'pick-change',
      kind: 'code', deps: [], task: `Implement \`${f.fn}\` in \`${f.file}\` so it does exactly what its docstring says, every example included.` })
  }
  const codeFiles = [...new Set(plan.subtasks.concat(unsplit).filter(st => st.kind === 'code').flatMap(st => `${st.title}\n${st.task}`.match(/[\w./-]+\.py\b/g) || [])
    .concat(TASK.match(/[\w./-]+\.py\b/g) || []).map(f => f.replace(CWD + '/', '').replace(/^\.\//, '')))]
  const inFixed = f => codeFiles.some(c => c === f.file || c.endsWith('/' + f.file) || f.file.endsWith('/' + c) || c.split('/').pop() === f.file.split('/').pop())
  const auditing = AUDITING
  if (auditing) {
    for (const f of FUNCTIONS.filter(f => !f.stub && inFixed(f) && !covered(f.fn) && !/^_*(main|cli)$/.test(f.fn))) {
      added.push({ n: next++, title: `Audit \`${f.fn}\` in \`${f.file}\``, difficulty: 'medium', solvers: 2, check: true, merge: 'pick-change',
        kind: 'code', deps: [], task: `Audit \`${f.fn}\` in \`${f.file}\` for bugs nobody reported, with the audit checklist in your skill. Fix what is wrong; change nothing that is right.` })
    }
    for (const c of CONSTANTS.filter(c => inFixed({ file: c.file }))) {
      added.push({ n: next++, title: `Audit the module-level tables in ${c.file}`, difficulty: 'medium', solvers: 2, check: true, merge: 'pick-change',
        kind: 'code', deps: [], task: `Audit every module-level table, mapping and threshold in \`${c.file}\` - ${c.names.map(n => `\`${n}\``).join(', ')} - against the README and any model or standard the request names. The project's own documents often print these groups (a chart or a table by group): find them in the plain-text copies (\`grep -n -i -A60 '<a group name>' <copy>\`), copy that listing with its \`file:line\` as the expected table, then print the code's groups and compare member by member. Memory of a model is not a source. Fix only entries that are wrong. **Never change a table's keys to match what the code produces now** - the code that produces them may be the bug; and keep every entry the rest of the code looks up.` })
    }
  }
  // A flag or option the request asks to add is a feature, with its own subtask. Measured
  // (d10): the split planned one subtask per symptom and per function, and `--json` was in none.
  if (plan.subtasks.some(st => st.kind === 'code')) {
    for (const flag of new Set(TASK.match(/(?<![\w-])--[a-z][\w-]*/g) || [])) {
      if (plan.subtasks.concat(added).some(st => `${st.title}\n${st.task}`.includes(flag))) continue
      const sentence = TASK.split(/(?<=[.!?])\s+|\n+/).find(x => x.includes(flag)) || flag
      added.push({ n: next++, title: `Feature: \`${flag}\``, difficulty: 'medium', solvers: 2, check: true, merge: 'pick-change',
        kind: 'code', deps: [], task: `Add what the request asks for, word for word:\n\n> ${sentence.trim()}\n\nKeep every existing behaviour, and run the program both with and without it.` })
    }
  }
  // A count the docs state that the program does not print is a bug to find, not a line to
  // confirm. Measured (d9, d10, d12 c1913h): "twelve profiles" against "bank size: 11" was
  // planned as a check that confirms the summary, waited behind eight subtasks, never ran, and
  // the one-character bug behind it cost six checks every time.
  if (plan.subtasks.some(st => st.kind === 'code')) {
    const WORDS = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15, twenty: 20 }
    const nums = t => new Set((String(t).toLowerCase().match(/\d[\d,]*|\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|twenty)\b/g) || [])
      .map(x => String(WORDS[x] || x.replace(/,/g, ''))))
    const rows = section(scouted, 'Stated outputs').split('\n').filter(l => /^\s*\|/.test(l) && !/^\s*\|\s*:?-{3}/.test(l)).slice(1)
      .map(l => l.split('|').slice(1, -1).map(c => c.trim())).filter(r => r.length >= 3 && r[0] && r[2])
    const differs = r => { const a = nums(r[0]), b = nums(r[2]); return a.size > 0 && [...a].some(x => !b.has(x)) }
    for (const [claim, src, now] of rows.filter(differs).slice(0, 3)) {
      added.push({ n: next++, title: `Docs: ${claim.replace(/`/g, '').slice(0, 70)} - code prints ${now.replace(/`/g, '').slice(0, 50)}`,
        difficulty: 'hard', solvers: 2, check: true, merge: 'pick-change', kind: 'code', deps: [],
        task: `The docs say (${src}):\n\n> ${claim}\n\nThe program prints now:\n\n> ${now}\n\nThe docs are right; the code is under audit. Run the program, find the item that is missing or extra, follow it back through each function to the line that drops or adds it, and fix that line. It is fixed when the program prints what the docs say.` })
    }
    // In a plan whose deliverables are code, a subtask that only confirms or verifies is
    // a check the runtime already runs.
    if (plan.deliverables.length && plan.deliverables.every(d => /\.(py|js|ts|mjs|cjs|sh)$/.test(d))) {
      const confirm = st => st.kind !== 'code' && /\b(confirm|verify|verif(ies|ication)|matches|check that)\b/i.test(`${st.title}\n${st.task}`)
      const gone = new Set(plan.subtasks.filter(confirm).map(st => st.n))
      if (gone.size) {
        plan.subtasks = plan.subtasks.filter(st => !gone.has(st.n))
        for (const st of plan.subtasks) st.deps = st.deps.filter(d => !gone.has(d))
        log(`plan: dropped confirm-only subtask(s) ${[...gone].join(', ')} - the runtime checks`)
      }
    }
  }
  if (added.length) {
    plan.subtasks = plan.subtasks.concat(added).slice(0, MAX_SUBTASKS)
    log(`plan: added ${added.length} subtask(s) so every function is covered`)
  }
}

// The symptoms of a whole-file subtask no planner could split go to the Audits of the functions
// they name, or of those functions' callers - never to every Audit, which invites changes in
// correct functions. A symptom that names no function is its own subtask.
if (unsplit.length) {
  const sentences = [...new Set(unsplit.flatMap(st => `${st.task}\n${TASK}`.split(/(?<=[.!?])\s+|\n+/))
    .map(x => x.trim()).filter(x => x.length > 12 && !/^#/.test(x)))]
  // Which function calls which, read from the original code by a script.
  const callsOut = PRISTINE ? await runExactly('callers', `python3 - ${q(BASE)} <<'PY'\nimport ast,os,sys\nroot=sys.argv[1]\nfor dp,dn,fs in os.walk(root):\n    dn[:]=[d for d in dn if d not in ('.git','__pycache__','node_modules','.venv','venv')]\n    for f in fs:\n        if not f.endswith('.py'): continue\n        try: t=ast.parse(open(os.path.join(dp,f)).read())\n        except Exception: continue\n        for n in t.body:\n            if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) or (isinstance(n,ast.If) and '__main__' in ast.unparse(n.test)):\n                me=getattr(n,'name','main')\n                for c in ast.walk(n):\n                    if isinstance(c,ast.Call) and isinstance(c.func,(ast.Name,ast.Attribute)): print('CALLS',me,getattr(c.func,'id',None) or c.func.attr)\nPY`, 'Review') : ''
  const callers = new Map()
  for (const m of String(callsOut || '').matchAll(/^CALLS (\w+) (\w+)$/gm)) {
    if (!callers.has(m[2])) callers.set(m[2], new Set())
    callers.get(m[2]).add(m[1])
  }
  const names = t => new Set((t.match(/[A-Za-z_]\w*/g) || []).concat(/__main__|command[- ]line|\bprints?\b|\boutput\b/i.test(t) ? ['main'] : []))
  let next = Math.max(0, ...plan.subtasks.map(o => o.n)) + 1
  const symptomLines = sentences.filter(x => /\b(returns?|prints?|shows?|gives?|outputs?|crash(es)?|wrong|instead|incorrect|missing|lacks?|errors?|fails?|breaks?|should)\b/i.test(x))
  for (const line of symptomLines) {
    const said = names(line)
    const audits = plan.subtasks.filter(st => { const f = functionOf(st)
      return f && (said.has(f.fn) || [...(callers.get(f.fn) || [])].some(c => said.has(c))) })
    for (const st of audits) st.task += `\n\nA symptom the request reports - it may or may not come from this function:\n> ${line}`
    if (!audits.length && plan.subtasks.length < MAX_SUBTASKS && !AUDITING_ONLY.test(line)) {
      plan.subtasks.push({ n: next++, title: `Symptom: ${line.replace(/`/g, '').slice(0, 90)}`, difficulty: 'hard', solvers: 2, check: true,
        merge: 'pick-change', kind: 'code', deps: [], task: `The request reports:\n\n> ${line}\n\nFind the line behind it and fix it.` })
    }
  }
}

// A text subtask answering three or more questions is one subtask per question, and every
// question the request or its files ask has one. Measured: one subtask for nine questions gave
// each solver nine answers to write, and facts in the README never reached Q3; and (d9) a plan
// that wrote "Q1, Q2, ... Q9" was split into Q1, Q2 and Q9 only.
{
  const qNums = text => {
    const set = new Set()
    const t = String(text || '')
    for (const m of t.matchAll(/\bQ(\d{1,2})\s*(?:-|‐|‑|‒|–|—|to|through|\.\.\.|…|,\s*(?:\.\.\.|…)\s*,?)\s*Q?(\d{1,2})\b/gi)) {
      const a = Number(m[1]), b = Number(m[2])
      if (b > a && b - a < 15) for (let k = a; k <= b; k++) set.add(k)
    }
    for (const m of t.matchAll(/\bQ(\d{1,2})\b/gi)) set.add(Number(m[1]))
    return set
  }
  const asked = new Set([...qNums(TASK), ...QUESTIONS.map(x => Number(x.n))])
  const textSubtasks = plan.subtasks.filter(st => st.kind === 'text')
  const qs = st => {
    const own = qNums(`${st.title}\n${st.task}`)
    // A text subtask naming several questions, or the only text subtask, answers every one asked.
    const whole = own.size >= 2 || textSubtasks.length === 1
    return [...new Set([...own, ...(whole && asked.size >= 3 ? asked : [])])].sort((a, b) => a - b)
  }
  // Each question's subtask holds the question, word for word, and only the notes about it.
  // Measured (d9): a merger handed the notes for all nine answered "too large for one step".
  const questionTask = (k, groupTask) => {
    const found = QUESTIONS.find(x => Number(x.n) === k)
    const notes = String(groupTask || '').split('\n').filter(l => new RegExp(`\\bQ${k}\\b`).test(l) && !/\b(create|write|begin|ends?|heading|file)\b/i.test(l))
    return [`Answer **Q${k}** of the request, and only Q${k}.`, '',
      found ? `The question, word for word (\`${found.file}\` line ${found.line}):\n\n> ${found.text}` : 'Quote the question word for word from the file that asks it.', '',
      'Answer every part of it from the documents, citing each fact. Where the documents lack something it asks for, say so plainly and say what we would supply.',
      notes.length ? `\nThe planner's notes on Q${k} (context only - the question and the documents win):\n${notes.join('\n')}` : ''].join('\n')
  }
  let next = Math.max(0, ...plan.subtasks.map(o => o.n)) + 1
  const out = []
  for (const st of plan.subtasks) {
    const list = st.kind === 'text' ? qs(st) : []
    if (list.length < 3) { out.push(st); continue }
    const parts = list.map(k => ({ n: next++, title: `Q${k}`, difficulty: st.difficulty === 'easy' ? 'medium' : st.difficulty,
      solvers: Math.max(2, Math.min(3, st.solvers)), check: true, merge: 'combine', kind: 'text', deps: [], task: questionTask(k, st.task) }))
    for (const o of plan.subtasks) o.deps = o.deps.flatMap(d => d === st.n ? parts.map(p => p.n) : [d])
    out.push(...parts)
    log(`plan: split subtask ${st.n} into one per question (${list.map(k => `Q${k}`).join(', ')})`)
  }
  plan.subtasks = out
  // A question asked but in no subtask gets one.
  const has = k => plan.subtasks.some(st => new RegExp(`^\\W*Q${k}\\b`, 'i').test(st.title))
  const missing = [...asked].filter(k => !has(k)).sort((a, b) => a - b)
  if (missing.length && asked.size >= 3 && plan.subtasks.some(st => st.kind === 'text')) {
    plan.subtasks.push(...missing.map(k => ({ n: next++, title: `Q${k}`, difficulty: 'medium', solvers: 2, check: true,
      merge: 'combine', kind: 'text', deps: [], task: questionTask(k, '') })))
    log(`plan: added ${missing.map(k => `Q${k}`).join(', ')} - asked, but in no subtask`)
  }
  // A subtask for one asked question carries that question word for word, never the planner's
  // words. Measured (d10): the plan's Q5-Q8 tasks described milestones, a schedule and contact
  // details - not the questions - and every solver answered what the task said.
  for (const st of plan.subtasks) {
    if (st.kind !== 'text') continue
    const own = [...qNums(st.title)]
    if (own.length !== 1 || !QUESTIONS.some(x => Number(x.n) === own[0])) continue
    st.title = `Q${own[0]}`
    st.task = questionTask(own[0], '')
  }
  // In question order, so the sections are.
  const qOf = st => (st.title.match(/^\W*Q(\d{1,2})\b/i) || [])[1]
  if (plan.subtasks.every(st => st.kind !== 'text' || qOf(st))) {
    plan.subtasks.sort((x, y) => (Number(qOf(x)) || 0) - (Number(qOf(y)) || 0))
  }
  plan.subtasks = plan.subtasks.slice(0, MAX_SUBTASKS)
}

// An Audit is judged against the spec, never against what the code does now. Measured (d10): a
// planner wrote "check that count_top counts ranks < k" and "validate that mean_rank computes
// the median" - the bugs themselves, as the spec - and the checkers held the fixes to it.
// Then the floors, after every pass that adds or rewrites subtasks.
for (const st of plan.subtasks) {
  const f = functionOf(st)
  if (st.kind === 'code' && f && /^\W*Audit\b/i.test(st.title)) {
    st.task = `Audit \`${f.fn}\` in \`${f.file}\` for bugs nobody reported, with the audit checklist in your skill. Judge it by its name, its docstring, its callers, the README and the documents - never by what it does now. Fix what is wrong; change nothing that is right.`
  }
  if (st.kind === 'code' && st.difficulty === 'easy') st.difficulty = 'medium'
  st.solvers = Math.max(st.solvers, { easy: 1, medium: 2, hard: 3 }[st.difficulty] || 2)
  if (st.difficulty !== 'easy') st.check = true
}

// One subtask per function: several bound to the same function are one, the symptom's words
// first. Measured (d12 c1913): three subtasks were bound to `lead_domain`; each began from the
// original, and at apply the last one's untouched copy put the bug back.
{
  const auditing = AUDITING
  const groups = new Map()
  for (const st of plan.subtasks) {
    const f = functionOf(st)
    if (!f) continue
    const key = `${f.file}:${f.fn}`
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(st)
  }
  const gone = new Set()
  for (const [, group] of groups) {
    group.sort((a, b) => (/^\W*Audit\b/i.test(a.title) ? 1 : 0) - (/^\W*Audit\b/i.test(b.title) ? 1 : 0))
    const [first, ...rest] = group
    for (const st of rest) {
      if (!/^\W*Audit\b/i.test(st.title)) first.task += `\n\nAlso, in the same function (from "${st.title}"):\n${st.task}`
      first.solvers = Math.max(first.solvers, st.solvers)
      gone.add(st.n)
    }
    // Measured (d12 c1913h): the function a symptom named was never audited, and its other
    // bug survived three rounds.
    const f = functionOf(first)
    if (auditing && !/^\W*Audit\b/i.test(first.title)) {
      first.task += `\n\nThen audit the whole of \`${f.fn}\` with the audit checklist in your skill - every threshold and constant tried on the real files, and the README's stated counts compared with what the program prints.`
    }
  }
  if (gone.size) {
    plan.subtasks = plan.subtasks.filter(st => !gone.has(st.n))
    for (const st of plan.subtasks) st.deps = st.deps.filter(d => !gone.has(d)).map(d => d)
    log(`plan: merged subtask(s) ${[...gone].join(', ')} into the subtask for the same function`)
  }
}

// Code subtasks each change their own copy of the original project; none waits on another.
// Measured (d9): a `--json` subtask waited 45 minutes behind seventeen others, then lost its
// only run to the budget.
for (const st of plan.subtasks) {
  if (st.kind === 'code') st.deps = st.deps.filter(d => !plan.subtasks.some(o => o.n === d && o.kind === 'code'))
}

// Keep the total, now the plan is final.
// Audits give up solvers first. Measured (d12 c1913): the trim took the reported symptoms
// down to one solver each while the audits of functions with no bug kept two.
total = plan.subtasks.reduce((n, s) => n + s.solvers, 0)
{
  const audit = st => /^\W*Audit\b/i.test(st.title) ? 0 : 1
  while (total > MAX_SOLVERS_TOTAL) {
    const most = plan.subtasks.filter(s => s.solvers > 1).sort((a, b) => audit(a) - audit(b) || b.solvers - a.solvers)[0]
    if (!most) break
    most.solvers -= 1
    total -= 1
  }
}
log(`plan (final): ${plan.subtasks.length} subtask(s), ${total} solver(s)`)

const METHOD = [
  'Use the most direct method.',
  'Use a DIFFERENT method from the obvious one: another library, another reading of the data, or first principles.',
  'Be a sceptic: look for the trap first (zero values, blanks, duplicates, edge cases), then solve.',
  'Work from the raw files directly, without any library you would normally reach for.',
  'Solve it, then try hard to break your own answer, and fix it if it breaks.',
]

function codeSteps(st, dir) {
  const f = functionOf(st)
  // Made once: a backup continues in the same copy rather than wiping the first attempt's work.
  const copy = `- Copy (safe to repeat): \`mkdir -p ${q(dir)} && { [ -f ${q(dir + '/.copied')} ] || { rm -rf ${q(dir + '/repo')} && mkdir -p ${q(dir + '/repo')} && cp -R ${q(BASE)}/. ${q(dir + '/repo')}/ && chmod -R u+w ${q(dir + '/repo')} && touch ${q(dir + '/.copied')}; }; }\` - work **only** in \`${dir}/repo\`.`
  const examples = f && EXAMPLES && PRISTINE
    ? `- **The docstring's examples are the spec.** Run them before and after your change:\n\`\`\`bash\npython3 ${q(EXAMPLES)} ${q(`${PRISTINE}/${f.file}`)} ${q(`${dir}/repo/${f.file}`)} ${f.fn}\n\`\`\`\n  Every line must read \`ok\`. Paste its last line under \`## Evidence\`.`
    : null
  const tests = `- Save your tests as \`${dir}/tests.py\`: a plain script that imports what it tests from the project's own module and \`assert\`s, run as \`cd ${q(dir + '/repo')} && python3 ${q(dir + '/tests.py')}\`.`
  if (f) {
    // The function alone, in its own file: the solver changes it there and puts it into its
    // copy by command. Measured (d12 he10): ten identical stubs in one file - `Edit` found ten
    // matches, `apply_patch` does not exist here, and 21 of 25 solvers never got a change in.
    const own = `${dir}/${f.fn}.py`
    const seed = `- Copy, and your function alone in its own file (safe to repeat):\n\`\`\`bash\nmkdir -p ${q(dir)} && { [ -f ${q(dir + '/.copied')} ] || { rm -rf ${q(dir + '/repo')} && mkdir -p ${q(dir + '/repo')} && cp -R ${q(BASE)}/. ${q(dir + '/repo')}/ && chmod -R u+w ${q(dir + '/repo')} && touch ${q(dir + '/.copied')} && ${extractCommand(`${dir}/repo/${f.file}`, f.fn, own).replace("<<'PY'", ">/dev/null <<'PY'")}\n}; }\ncat ${q(own)}\n\`\`\``
    return [`\n## Your copy - this subtask is the function \`${f.fn}\` in \`${f.file}\``, seed,
      `- **Change \`${f.fn}\` in \`${own}\`, and only there** - \`Write\` that whole small file. Never edit \`repo/${f.file}\` itself.`,
      `- Change only the body - never its name, arguments, defaults or docstring. Put any \`import\` it needs inside the function.`,
      `- Put it into your copy and run the examples - exactly, after every change:\n\`\`\`bash\n${replaceCommand(`${dir}/repo/${f.file}`, f.fn, own)}${EXAMPLES && PRISTINE ? `\npython3 ${q(EXAMPLES)} ${q(`${PRISTINE}/${f.file}`)} ${q(`${dir}/repo/${f.file}`)} ${f.fn}` : ''}\n\`\`\`\n  Every example line must read \`ok\`. Paste the last line under \`## Evidence\`.`,
      tests,
      `- Under \`## Change\`, give \`${own}\`; under \`## Tests\`, \`${dir}/tests.py\`.`].join('\n')
  }
  return ['\n## Your copy', copy, tests,
    `- **Leave your change in \`${dir}/repo\`** - the runtime reads it from there. Never write or edit a patch by hand.`,
    `- Under \`## Change\`, give \`${dir}/repo\`; under \`## Tests\`, \`${dir}/tests.py\`.`].join('\n')
}

function solvePrompt(st, k, deps, feedback) {
  const dir = `${SCRATCH}/s${st.n}/${k}`
  return prompt('solve', `Solve subtask ${st.n}: ${st.title} (solver ${k})`, [
    `**Kind:** ${st.kind}. **Difficulty:** ${st.difficulty}.`, '',
    '## The request - the whole job, of which your subtask is one part', '```text', TASK, '```', '',
    '## Your subtask', `**${st.title}**`, '',
    specOf(st), '',
    specOnly(st) ? null : specOf(st) ? "## The planner's notes - a paraphrase that may be wrong; the spec above wins" : null, specOnly(st) ? null : nest(st.task, 3), '',
    deps.length ? '## Results it depends on' : null, ...deps,
    '', '## The brief from the scout', brief, '',
    `## Your method\n${METHOD[(k - 1) % METHOD.length]}`,
    st.kind === 'code' ? codeSteps(st, dir) : null,
    feedback ? `\n## A checker found a problem with the first answer\n${feedback}\nSolve it again with this in mind.` : null,
  ])
}

// One agent that runs one exact command. The marker lets the bridge run it without the model,
// so it takes no upstream slot. Measured (d13 c1913): with stuck agents holding slots, a
// restore or apply queued behind them.
function runExactly(label, cmd, ph) {
  return ask(prompt('assemble', label, [
    'Run exactly this command - nothing else - then reply with what it printed:', '', '[exact-command]', '```bash', cmd, '```',
  ]), label, ph, null, { wait: true, short: true, exact: true, deadline: EXACT_MS })
}

// A function's candidates are picked by a script, not an agent: each solver's copy of the
// function goes into a fresh copy of the project and is measured - the docstring's examples,
// every solver's tests pooled and pointed at that copy, a branch on an example's own input,
// self-checks it added and inputs near the examples that make it raise, and which candidates
// behave alike. A solver whose reply never arrived still has its copy on disk.
// Measured (d9): every merge of a function was given up on or skipped, so the first solver
// won blind; and three solvers that fixed count_top in their copies were never heard from.
// (d13 he10): `if nums == [1, 11, -1, -11, -12]` passed every example and won. (d13 he10)
// a candidate ending `assert count1 == count2` raised on "Is the sky blue?" and won. (d12-d14)
// 32 of 39 contested picks were ties broken by candidate number, compared as a string.
const PICK = `${SCRATCH}/pick-v2.py`
const PICK_PY = String.raw`import ast,hashlib,json,os,re,shutil,subprocess,sys,tempfile,time
# The candidates of one function, measured. usage: pick.py ROOT REL FN EXAMPLES|- k|dir|replied ... -- tests...
# env: PICK_OUT (the frozen choice), PICK_AUDIT=1 (the original competes, and a change must beat
# it on a measurement), PICK_S (the whole pick's time, 420 s by default).
T0=time.time(); LIMIT=float(os.environ.get('PICK_S') or 420)
left=lambda: LIMIT-(time.time()-T0)
root,rel,fn,ex=sys.argv[1:5]; rest=sys.argv[5:]; i=rest.index('--'); cands=rest[:i]; tests=[t for t in rest[i+1:] if os.path.exists(t)]
AUDIT=os.environ.get('PICK_AUDIT')=='1'; frozen=os.environ.get('PICK_OUT')
def node(code):
    for n in ast.parse(code).body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn: return n
def fnsrc(path):
    code=open(path).read(); n=node(code)
    if n: l=code.splitlines(True); return ''.join(l[(n.decorator_list[0].lineno if n.decorator_list else n.lineno)-1:n.end_lineno])
def put(path,new):
    code=open(path).read(); l=code.splitlines(True); n=node(code)
    if n: l[(n.decorator_list[0].lineno if n.decorator_list else n.lineno)-1:n.end_lineno]=[new.rstrip(chr(10))+chr(10)]
    else: l.append(chr(10)*2+new)
    open(path,'w').write(''.join(l))
spec_code=open(os.path.join(root,rel)).read(); spec=node(spec_code); orig=fnsrc(os.path.join(root,rel)) or ''
doc=(ast.get_docstring(spec) or '') if spec else ''
def calls(text):
    out=[]
    for m in re.finditer(r'(?<![\w.])'+re.escape(fn)+r'\(',text):
        d=0
        for j in range(m.end()-1,min(len(text),m.end()+400)):
            d+=text[j]=='('; d-=text[j]==')'
            if d==0:
                c=text[m.start():j+1]
                try: ast.parse(c,mode='eval'); out.append(c)
                except SyntaxError: pass
                break
    return out
EXAMPLE_CALLS=list(dict.fromkeys(calls(doc)))
PARAMS=[a.arg for a in (spec.args.posonlyargs+spec.args.args+spec.args.kwonlyargs)] if spec else []
def bound(c):
    # The literal value each parameter takes in one call.
    try: call=ast.parse(c,mode='eval').body
    except SyntaxError: return {}
    out={}
    for k,a in enumerate(call.args):
        if k<len(PARAMS):
            try: out[PARAMS[k]]=ast.literal_eval(a)
            except Exception: pass
    for kw in call.keywords:
        try: out[kw.arg]=ast.literal_eval(kw.value)
        except Exception: pass
    return out
EX_VALUES={}
for c in EXAMPLE_CALLS:
    for p,v in bound(c).items(): EX_VALUES.setdefault(p,[]).append(v)
def telling(v):
    # A value worth naming only to hit one example: not 0, 1, an empty or one-item container.
    if isinstance(v,bool) or v is None: return False
    if isinstance(v,(int,float)): return v not in (-1,0,1,2)
    if isinstance(v,str): return len(v)>=3
    if isinstance(v,(list,tuple,dict,set)): return len(v)>=2
    return False
def hardcoded(src):
    # A comparison of a bare parameter with that parameter's own example value. Never a
    # constant against a local name: measured, "i < 10" in a correct generate_integers was
    # flagged by a rule that matched any example scalar.
    hits=[]
    try: c=node(src)
    except Exception: return hits
    if not c: return hits
    def names(x):
        if isinstance(x,ast.Name) and x.id in EX_VALUES: return [x.id]
        if isinstance(x,(ast.Tuple,ast.List)) and x.elts and all(isinstance(e,ast.Name) and e.id in EX_VALUES for e in x.elts): return [e.id for e in x.elts]
        return []
    for n in ast.walk(c):
        if not isinstance(n,ast.Compare): continue
        sides=[n.left,*n.comparators]
        for k,op in enumerate(n.ops):
            for a,b in ((sides[k],sides[k+1]),(sides[k+1],sides[k])):
                ps=names(a)
                if not ps: continue
                try: v=ast.literal_eval(b)
                except Exception: continue
                want=[tuple(x) for x in zip(*[EX_VALUES[p] for p in ps])] if len(ps)>1 else EX_VALUES[ps[0]]
                vals=[v] if isinstance(op,(ast.Eq,ast.NotEq,ast.Is,ast.IsNot)) else list(v) if isinstance(op,(ast.In,ast.NotIn)) and isinstance(v,(list,tuple,set,frozenset)) else []
                for x in vals:
                    x=tuple(x) if len(ps)>1 and isinstance(x,(list,tuple)) else x
                    if any(x==w for w in want) and (len(ps)>1 or telling(x)): hits.append(n.lineno)
    return sorted(set(hits))
def selfchecks(src):
    try: c=node(src)
    except Exception: return 0
    # Asserts and raises, not counting a stub's NotImplementedError.
    stub=lambda x: isinstance(x,ast.Raise) and 'NotImplementedError' in ast.unparse(x)
    return sum(isinstance(x,(ast.Assert,ast.Raise)) and not stub(x) for x in ast.walk(c)) if c else 0
ORIG_CHECKS=selfchecks(orig) if orig else 0
# Module-level names the function assigns again: a local copy of a table shadows the real one.
# Measured (d15 c1913h): an audited parse() pasted in SPELLING with the old bug, overriding the
# fixed module-level table.
try: MODULE_NAMES={t.id for n in ast.parse(open(os.path.join(root,rel)).read()).body if isinstance(n,(ast.Assign,ast.AnnAssign)) for t in (n.targets if isinstance(n,ast.Assign) else [n.target]) if isinstance(t,ast.Name)}
except Exception: MODULE_NAMES=set()
def shadows(src):
    try: c=node(src)
    except Exception: return 0
    return len({x.id for x in ast.walk(c) if isinstance(x,ast.Name) and isinstance(x.ctx,ast.Store) and x.id in MODULE_NAMES}) if c else 0
def variants(v):
    out=[]
    if isinstance(v,str):
        out+=[p.strip() for p in re.split(r'(?<=[.!?])\s+',v) if p.strip() and p.strip()!=v][:3]
        out+=[w for w in v.split()[:3] if w!=v]
        out+=[v[:len(v)//2]] if len(v)>=4 else []
    elif isinstance(v,(list,tuple)) and len(v)>=2:
        out+=[type(v)(v[:-1]),type(v)(v[1:]),type(v)(list(v)[::-1])]
    elif isinstance(v,int) and not isinstance(v,bool):
        out+=[v+1,v-1]
    return out
PERTURBED=[]
for c in EXAMPLE_CALLS:
    try: call=ast.parse(c,mode='eval').body; vals=[ast.literal_eval(a) for a in call.args]
    except Exception: continue
    if call.keywords: continue
    for k,v in enumerate(vals):
        for x in variants(v):
            args=vals[:k]+[x]+vals[k+1:]; PERTURBED.append(fn+'('+', '.join(repr(a) for a in args)+')')
PERTURBED=list(dict.fromkeys(PERTURBED))[:40]
TEST_CALLS=[c for t in tests for c in calls(open(t).read())]
PROBES=list(dict.fromkeys(EXAMPLE_CALLS+TEST_CALLS))[:120]
PROBE_PY=r'''import json,os,sys,io,contextlib,signal
p,fn=sys.argv[1],sys.argv[2]; probes=json.loads(sys.stdin.read())
def stop(*_): raise TimeoutError('slow')
signal.signal(signal.SIGALRM,stop)
os.chdir(os.path.dirname(p)); sys.path.insert(0,os.path.dirname(p)); sys.argv=[p,'--agentaus-not-an-option']; sys.dont_write_bytecode=True
ns={'__name__':'candidate','__file__':p}
try:
    signal.alarm(10)
    with contextlib.redirect_stdout(io.StringIO()): exec(compile(open(p).read(),p,'exec'),ns)
    signal.alarm(0)
except BaseException as e:
    print(json.dumps(['IMPORT '+type(e).__name__]*len(probes))); sys.exit(0)
out=[]
for c in probes:
    try:
        signal.alarm(3)
        with contextlib.redirect_stdout(io.StringIO()): out.append(repr(eval(c,ns)))
    except BaseException as e: out.append('EXC '+type(e).__name__)
    finally: signal.alarm(0)
print(json.dumps(out))'''
def probe(path,calls_):
    if not calls_: return []
    try:
        r=subprocess.run([sys.executable,'-c',PROBE_PY,path,fn],input=json.dumps(calls_),capture_output=True,text=True,timeout=max(5,min(60,left()-5)))
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        return ['PROBE '+type(e).__name__]*len(calls_)
dirs=[c.split('|')[1] for c in cands]
def repoint(t,w,td):
    # A solver's tests pin sys.path to its own copy; pointed at the candidate's copy instead,
    # they test the candidate. Measured: a test that imported from s4/1/repo passed a candidate
    # that returned [42].
    s=open(t).read()
    for d in sorted(set(dirs),key=len,reverse=True): s=s.replace(d.rstrip('/')+'/repo',w)
    s=s.replace(root.rstrip('/'),w)
    p=os.path.join(td,'t%d.py'%len(os.listdir(td))); open(p,'w').write(s); return p
rows=[]; notes=[]; best=None
def key(r,votes):
    return (r['hard'],r['bad'],r['raises'],-r['passed'],-votes.get(r['sig'],0),-r['replied'],int(r['k']) if r['k'].isdigit() else 999)
def rank():
    votes={}
    for r in rows:
        if not r['hard']: votes[r['sig']]=votes.get(r['sig'],0)+1
    pool=rows
    if AUDIT:
        o=[r for r in rows if r['k']=='0']
        if o:
            o=o[0]
            # A change beats the original only on a measurement: fewer broken examples, or no
            # new broken example and a pooled test that fails on the original and passes on it.
            def wins(r):
                if not r['changed'] or r['hard']: return False
                if r['bad']<o['bad']: return True
                return r['bad']<=o['bad'] and r['miss']<=o['miss'] and any(a and not b for a,b in zip(r['tests'],o['tests']))
            pool=[o]+[r for r in rows if r['k']!='0' and (wins(r) or not r['changed'])]
    pool=sorted(pool,key=lambda r:key(r,votes))
    return pool,votes
def report(r,votes,final=False):
    print('CHOSEN',frozen if frozen else r['path'],'mismatches',99 if r['hard'] else r['bad'],'from',r['path'],'changed',r['changed'],'votes',votes.get(r['sig'],0),'examples',r['nex'],'candidate',r['k'],flush=True)
order=([('0',os.path.join(root,'.'),'0')] if AUDIT else [])+[tuple(c.split('|')) for c in cands]
td=tempfile.mkdtemp()
for k,d,replied in order:
    if left()<20:
        notes.append(f'candidate {k}: not measured - the pick ran out of time'); continue
    path=None; new=None
    if k=='0':
        path,new=os.path.join(root,rel),orig
    else:
        for p in (os.path.join(d,fn+'.py'),os.path.join(d,'repo',rel)):
            try: s=fnsrc(p) if os.path.exists(p) else None
            except Exception as e: notes.append(f'candidate {k} unreadable: {p} {type(e).__name__}'); s=None
            if s and (new is None or (new.strip()==orig.strip() and s.strip()!=orig.strip())): path,new=p,s
    if not new: notes.append(f'candidate {k} has no {fn}'); continue
    w=tempfile.mkdtemp(); shutil.copytree(root,w,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','.git')); put(os.path.join(w,rel),new)
    bad=0; out=''; nex=0; miss=set()
    if ex!='-':
        try:
            out=subprocess.run([sys.executable,ex,os.path.join(root,rel),os.path.join(w,rel),fn],capture_output=True,text=True,timeout=max(5,min(60,left()-10))).stdout
            bad=out.count('MISMATCH'); miss={l.split(' -> ')[0] for l in out.splitlines() if l.startswith('MISMATCH')}
            m=re.search(r'(\d+)/(\d+) docstring examples pass',out); nex=int(m.group(2)) if m else 0
        except Exception: bad=99
        # An import failure is a full set of mismatches, never one.
        if 'MISMATCH import' in out: bad=99
    res=[]
    for j,t in enumerate(tests):
        try: res.append(subprocess.run([sys.executable,repoint(t,w,td)],cwd=w,env=dict(os.environ,PYTHONPATH=w,PYTHONDONTWRITEBYTECODE='1'),stdin=subprocess.DEVNULL,capture_output=True,timeout=max(3,min(60,(left()-10)/max(1,len(tests)-j)))).returncode==0)
        except Exception: res.append(False)
    outs=probe(os.path.join(w,rel),PROBES+PERTURBED)
    raised=sum(1 for x in outs[len(PROBES):] if str(x).startswith(('EXC','IMPORT','PROBE')))
    hard=hardcoded(new); changed=new.strip()!=orig.strip()
    r=dict(k=k,path=path,out=out,bad=bad,miss=len(miss),nex=nex,tests=res,passed=sum(res),replied=replied=='1',hard=bool(hard),
           raises=max(0,selfchecks(new)-ORIG_CHECKS)+raised+3*max(0,shadows(new)-(shadows(orig) if orig else 0)),sig=hashlib.sha1(json.dumps(outs[:len(PROBES)]+outs[len(PROBES):]).encode()).hexdigest()[:8],changed=changed)
    rows.append(r)
    notes.append(f'candidate {k}: {path} - docstring example mismatches {bad}, pooled tests passed {sum(res)}/{len(tests)}, self-checks and raising probes {r["raises"]}, behaviour {r["sig"]}, replied {replied}, changed {changed}'+(f', BRANCHES ON AN EXAMPLE INPUT (line {hard[0]})' if hard else ''))
    pool,votes=rank()
    # The frozen choice and a CHOSEN line every time the best improves: a pick cut short still
    # leaves its best so far.
    if pool and (best is None or pool[0] is not best):
        best=pool[0]
        if frozen: os.makedirs(os.path.dirname(frozen),exist_ok=True); open(frozen,'w').write(fnsrc(best['path']) if best['k']!='0' else orig)
        report(best,votes)
pool,votes=rank()
for n in notes: print(n[:400])
if rows: print('behaviour groups:',', '.join(f'{s} x{n}' for s,n in sorted(votes.items(),key=lambda x:-x[1])))
r0=pool[0] if pool else None
for l in [l for l in (r0['out'].splitlines() if r0 else []) if 'MISMATCH' in l][:6]: print('EXAMPLE',l[:300])
if r0:
    if r0 is not best and frozen: open(frozen,'w').write(fnsrc(r0['path']) if r0['k']!='0' else orig)
    report(r0,votes,True)
else:
    print('CHOSEN none mismatches -1 from - changed False votes 0 examples 0')
`
let pickReady = false
const solverDir = (st, k) => `${SCRATCH}/s${st.n}/${k}`
// The picker installs itself if it is not there yet - written whole, then moved into place,
// so a pick running beside the install never reads half a file.
const installPick = `mkdir -p ${q(SCRATCH)} && { [ -s ${q(PICK)} ] || { cat > ${q(PICK + '.part')}.$$ <<'AGENTAUS_PICK_END' && mv ${q(PICK + '.part')}.$$ ${q(PICK)}; }; }\n${PICK_PY}AGENTAUS_PICK_END`
// An Audit's original competes, and a change replaces it only on a measurement.
const isAudit = st => /^\W*Audit\b/i.test(st.title)
async function pickFunction(st, f, ks, replies, audit) {
  const cands = ks.map(k => `${k}|${solverDir(st, k)}|${replies[k] ? 1 : 0}`)
  const tests = ks.map(k => `${solverDir(st, k)}/tests.py`)
  // The chosen function is frozen at once: a solver still running cannot change it afterwards.
  const frozen = `${SCRATCH}/chosen/s${st.n}/${f.fn}.py`
  const out = await runExactly(`pick-${st.n}`, `${installPick}\nPICK_OUT=${q(frozen)}${audit ? ' PICK_AUDIT=1' : ''} PICK_S=420 python3 ${q(PICK)} ${q(BASE)} ${q(f.file)} ${f.fn} ${EXAMPLES && PRISTINE ? q(EXAMPLES) : '-'} ${cands.map(q).join(' ')} -- ${tests.map(q).join(' ')}`, 'Pick')
  // The last CHOSEN line is the pick's best: it prints one each time the best improves.
  const all = [...String(out || '').matchAll(/^CHOSEN (\S+) mismatches (-?\d+) from (\S+) changed (\w+)(?: votes (\d+) examples (\d+) candidate (\S+))?/gm)]
  const m = all[all.length - 1]
  if (!m || m[1] === 'none') return null
  const k = Number(m[7] || (m[3].match(new RegExp(`/s${st.n}/(\\d+)/`)) || [])[1] || 0)
  const reply = (k && replies[k]) || (k ? `## Answer\nCandidate ${k}'s \`${f.fn}\` - chosen by the measurements below.` : `## Answer\n\`${f.fn}\` is kept as it is: no change beat the original on a measurement.`)
  return { result: `${reply}\n\n## Why\n${(out.match(/^candidate .*$/gm) || []).join('\n')}`, change: m[1], dir: k ? solverDir(st, k) : null, mismatches: Number(m[2]),
    changed: m[4] !== 'False', votes: Number(m[5] || 1), examples: Number(m[6] || 0), picked: true,
    evidence: (out.match(/^EXAMPLE .*$/gm) || []).map(l => l.replace(/^EXAMPLE\s+/, '')).join('\n') }
}

// Think-only drafts: the function written whole, from its spec and its code now, in one call.
// They are counted apart from the solver cap, so every function subtask has two independent
// candidates. Measured (d13 c1913h at width 6): the cap trimmed every Audit to one solver
// before any time pressure, and every wrong function was a single-candidate pick.
async function drafts(st, f, n, ph) {
  const src = `${SCRATCH}/s${st.n}/original-${f.fn}.py`
  const now = await runExactly(`draft-source-${st.n}`, `mkdir -p ${q(`${SCRATCH}/s${st.n}`)} && ${extractCommand(`${BASE}/${f.file}`, f.fn, src)}`, ph)
  const md = await ask(prompt('solve', `Draft \`${f.fn}\` for subtask ${st.n}: ${st.title}`, [
    '**Kind:** code. You get no tools: write the function from what is below.', '',
    '## The request', '```text', TASK, '```', '',
    '## Your subtask', `**${st.title}**`, '', specOf(st), '',
    specOnly(st) ? null : nest(st.task, 3), '',
    now ? ['## The function now, in the original file', '```python', String(now).slice(0, 3000), '```'].join('\n') : null, '',
    `Write ${n > 1 ? `${n} different versions` : 'one version'} of \`${f.fn}\`, each the whole function in its own \`\`\`python block - the same name, arguments and docstring, any import inside it. Where the function is already right, write it unchanged. Never branch on an example's literal input, and add no asserts.`,
  ], true), `draft-${st.n}`, ph, t => new RegExp(`def ${f.fn}\\(`).test(t), { pri: 1 })
  if (!md) return []
  const blocks = [...md.matchAll(/```python\n([\s\S]*?)```/g)].map(x => x[1]).filter(b => new RegExp(`def ${f.fn}\\(`).test(b)).slice(0, n)
  const dk = blocks.map((_, j) => 41 + j)
  if (!blocks.length) return []
  await runExactly(`draft-save-${st.n}`, blocks.map((b, j) =>
    `mkdir -p ${q(solverDir(st, dk[j]))} && cat > ${q(`${solverDir(st, dk[j])}/${f.fn}.py`)} <<'AGENTAUS_DRAFT_END'\n${b}AGENTAUS_DRAFT_END`).join('\n') + '\necho saved', ph)
  return dk
}

// When every candidate breaks a docstring example, the reading of the words is wrong. One
// agent writes five readings as whole functions, a command saves them, and the picker - which
// ranks example mismatches first - chooses among solvers and readings alike. Measured (d9,
// d10): every order_by_points candidate kept the planner's "absolute value" reading and failed
// the docstring's own example; agents then patched the example's values in by hand.
async function readings(st, f, evidence, round) {
  const md = await ask(prompt('solve', `Read the spec of ${f.fn} five ways${round > 1 ? ' (again)' : ''}`, [
    specOf(st), '',
    evidence ? `## The best candidate so far breaks these examples (what it returned is shown)\n\`\`\`text\n${evidence}\n\`\`\`` : null, '',
    '## Method',
    '1. List every rule the docstring states in plain words - how ties are ordered, what is included - and keep each one fixed. A reading may change only what the words leave open.',
    '2. Work each failing example backwards: under those fixed rules, write what the function must compute for each input element for the expected output to come out (inequalities are enough).',
    '3. Find readings of the open words that give exactly those values - the main computation (how a sign, a digit, a boundary, "the word" versus "the letters", a rounding rule is meant), not only the tie order.',
    '4. Check every reading by hand on every example before you write it.', '',
    `Write **five different readings**, each as one complete Python function named \`${f.fn}\`, imports inside it, in its own \`\`\`python block. Never branch on an example's literal input.`,
  ], true), `readings-${st.n}${round > 1 ? `.${round}` : ''}`, 'Readings', t => (t.match(/```python/g) || []).length >= 2)
  if (!md) return []
  const blocks = [...md.matchAll(/```python\n([\s\S]*?)```/g)].map(x => x[1]).filter(b => new RegExp(`def ${f.fn}\\(`).test(b)).slice(0, 6)
  const rk = blocks.map((_, j) => 10 * (round + 1) + 1 + j)
  if (!blocks.length) return []
  await runExactly(`readings-save-${st.n}`, blocks.map((b, j) =>
    `mkdir -p ${q(solverDir(st, rk[j]))} && cat > ${q(`${solverDir(st, rk[j])}/${f.fn}.py`)} <<'AGENTAUS_READING_END'\n${b}AGENTAUS_READING_END`).join('\n') + '\necho saved', 'Readings')
  return rk
}

// Answers that say the same thing need no merger. Measured: unanimous answers went through a
// merger anyway, which rewrote them, dropped their citations and swapped in non-breaking hyphens.
// Lines appended to the end of a result's Answer section, at whatever heading level it uses.
function withAnswerLines(md, lines) {
  const text = String(md || '')
  const m = text.match(/^(#{1,3})\s*Answer\s*:?\s*$/im)
  if (!m) return `${text.trimEnd()}\n\n${lines.join('\n')}\n`
  const start = m.index + m[0].length
  const rest = text.slice(start)
  const end = rest.search(new RegExp(`^#{1,${m[1].length}}\\s*(${KEYS})\\s*:?\\s*$`, 'im'))
  const at = end >= 0 ? start + end : text.length
  return `${text.slice(0, at).trimEnd()}\n\n${lines.join('\n')}\n\n${text.slice(at).trimStart()}`.trimEnd()
}
const sameText = t => section(t, 'Answer').replace(/[‐-―]/g, '-').replace(/\s+/g, ' ').trim().toLowerCase()

async function merge(st, results, strategy) {
  if (results.length === 1) return results[0]
  if (st.kind !== 'code' && sameText(results[0]) && results.every(r => sameText(r) === sameText(results[0]))) return results[0]
  // Code is merged by running it, so its merger has tools; everything else is judged on
  // the evidence written down, in one call.
  const code = st.kind === 'code' || strategy === 'pick-change'
  const f = functionOf(st)
  const runIt = code ? [
    '## How to test each candidate',
    `1. Pool every \`## Tests\` file below.`,
    `2. For candidate N: \`rm -rf ${q(`${SCRATCH}/m${st.n}/cN`)} && mkdir -p ${q(`${SCRATCH}/m${st.n}/cN`)} && cp -R ${q(BASE)} ${q(`${SCRATCH}/m${st.n}/cN/repo`)} && chmod -R u+w ${q(`${SCRATCH}/m${st.n}/cN`)}\`, then put the candidate in place - ` +
      (f ? `its function, with:\n\`\`\`bash\n${replaceCommand(`${SCRATCH}/m${st.n}/cN/repo/${f.file}`, f.fn, 'CANDIDATE_FUNCTION_FILE')}\n\`\`\`` : `copy the candidate's own folder over it: \`cp -R CANDIDATE_REPO/. ${q(`${SCRATCH}/m${st.n}/cN/repo`)}/\`, where CANDIDATE_REPO is the \`repo\` folder under its \`## Change\`.`),
    f && EXAMPLES && PRISTINE ? `3. **Docstring examples first:** \`python3 ${q(EXAMPLES)} ${q(`${PRISTINE}/${f.file}`)} ${q(`${SCRATCH}/m${st.n}/cN/repo/${f.file}`)} ${f.fn}\`. A candidate that fails any example loses to one that passes them all, whatever the pooled tests say.` : null,
    `${f && EXAMPLES && PRISTINE ? 4 : 3}. Run every pooled tests file from \`${SCRATCH}/m${st.n}/cN/repo\`, count the passes, and keep the candidate that passes most.`,
  ].join('\n') : null
  const merged = await ask(prompt('merge', `Merge results for subtask ${st.n}: ${st.title}`, [
    `**Strategy:** \`${code ? 'pick-change' : strategy}\``, '', '## The subtask', `**${st.title}**`, '', specOnly(st) ? null : nest(st.task, 3), '',
    specOf(st), '', runIt, '',
    ...results.map((r, k) => card(`Result ${k + 1}`, r, { top: 2, evidence: true })),
  ].filter(l => l !== null), !code), `merge-${st.n}`, code ? 'Pick' : 'Merge', md => code || !!section(md, 'Answer'), { pri: -1 })
  if (!merged) return bestOf(results)
  // Every figure two or more solvers gave stays. Measured (d10): all three solvers gave the
  // GST-inclusive total, and the merger kept only the two figures the task had named.
  if (st.kind === 'text') {
    const nums = t => new Set(((section(t, 'Answer') + '\n' + section(t, 'Key values')).match(/\d[\d,]{2,}(?:\.\d+)?/g) || []).map(n => n.replace(/,/g, '').replace(/\.0+$/, '')))
    const count = new Map()
    for (const r of results) for (const n of nums(r)) count.set(n, (count.get(n) || 0) + 1)
    const agreed = [...count].filter(([, c]) => c >= 2).map(([n]) => n)
    const lost = agreed.filter(n => !nums(merged).has(n))
    // The merged answer stays, and each lost figure comes back with its solver's sentence.
    // Measured (d11 tender): swapping in the fullest solver answer instead lost "1,122" and
    // "Liesveld", which the merged text held.
    if (lost.length) {
      const sentences = t => section(t, 'Answer').split(/(?<=[.!?])\s+|\n+/).map(x => x.trim()).filter(Boolean)
      const holds = (x, n) => new RegExp(`(^|[^\\d.])${n.replace(/\./g, '\\.')}(?![\\d])`).test(x.replace(/,/g, ''))
      const adds = []
      for (const n of lost) {
        const s = results.flatMap(sentences).find(x => holds(x, n))
        if (s && !adds.includes(s) && !merged.includes(s)) adds.push(s)
      }
      log(`merge-${st.n}: the merged answer dropped ${lost.join(', ')}, given by two or more solvers - added back ${adds.length} sentence(s)`)
      if (adds.length) return withAnswerLines(merged, adds.map(s => `- ${s.replace(/^[-*]\s*/, '')}`))
    }
  }
  // A merger that names no saved change keeps the first solver's, so the assembler still has
  // one to put in place.
  const paths = results.flatMap(r => section(r, 'Change').match(/[^\s`'"]+\.(py|patch)\b/g) || [])
  if (paths.length && !paths.some(p => merged.includes(p))) {
    return `${merged}\n\n## Change\n${section(results.find(r => /\.(py|patch)\b/.test(section(r, 'Change'))), 'Change')}`
  }
  return merged
}

// The result whose evidence is fullest, when no merger answered.
const bestOf = results => results.filter(r => section(r, 'Answer'))
  .sort((a, b) => section(b, 'Evidence').length - section(a, 'Evidence').length)[0] || results[0]

const VERDICT = md => (String(md || '').match(/\*\*Verdict:\*\*\s*(PASS|FAIL)/i) || [])[1]


// A whole-copy subtask's solver copies, read from disk: each is snapshotted at once - a solver
// still running cannot change it afterwards - and counts as changed only when a Python file the
// original has differs. Measured (mock, after the bounded waits): every solver the graph stops
// waiting for leaves a reply-less copy, and a feature held only in such a copy was lost.
const snapDir = (st, k) => `${SCRATCH}/snap/s${st.n}/${k}`
async function readCopies(st, ks) {
  const out = await runExactly(`copies-${st.n}`, `python3 - ${q(BASE)} ${ks.map(k => q(`${k}:${solverDir(st, k)}:${snapDir(st, k)}`)).join(' ')} <<'PY'\nimport filecmp,os,shutil,sys\nbase=sys.argv[1]; skip=('.git','__pycache__','node_modules','.venv','venv')\nfor a in sys.argv[2:]:\n    k,d,dst=a.split(':',2); repo=os.path.join(d,'repo')\n    if not os.path.isdir(repo): print('none',k); continue\n    shutil.rmtree(dst,ignore_errors=True); os.makedirs(dst,exist_ok=True)\n    try: shutil.copytree(repo,os.path.join(dst,'repo'),symlinks=True,ignore=shutil.ignore_patterns(*skip))\n    except Exception as e: print('none',k,type(e).__name__); continue\n    ch=[]\n    for dp,dn,fs in os.walk(base):\n        dn[:]=[x for x in dn if x not in skip]\n        for f in fs:\n            if not f.endswith('.py'): continue\n            rel=os.path.relpath(os.path.join(dp,f),base); c=os.path.join(dst,'repo',rel)\n            if os.path.exists(c) and not filecmp.cmp(os.path.join(dp,f),c,shallow=False): ch.append(rel)\n    print('changed' if ch else 'same',k,' '.join(ch[:5]))\nPY`, 'Pick')
  const changed = new Set([...String(out || '').matchAll(/^changed (\d+)/gm)].map(m => Number(m[1])))
  const seen = new Set([...String(out || '').matchAll(/^(?:changed|same) (\d+)/gm)].map(m => Number(m[1])))
  return { changed, seen }
}

// Where a code result's change lives: the solver copy it names, or the first that replied -
// its snapshot where there is one.
function dirOf(st, result, ks, replies, snapped) {
  const named = Number((String(result || '').match(new RegExp(`/s${st.n}/(\\d+)/`)) || [])[1])
  const k = ks.includes(named) ? named : ks.find(x => replies[x])
  if (!k) return null
  return snapped && snapped.has(k) ? snapDir(st, k) : solverDir(st, k)
}

// A code result is tested in a copy holding that change - never in the project, which holds
// other subtasks' decided work. Measured (d9): a checker imported the project's module and
// failed a correct change it had never loaded.
function testCopy(st, d, bare) {
  if (st.kind !== 'code' || !d) return null
  const f = functionOf(st)
  const c = `${SCRATCH}/c${st.n}`
  const make = f && d.change
    ? `rm -rf ${q(c)} && mkdir -p ${q(c)} && cp -R ${q(BASE)}/. ${q(c)}/ && ${replaceCommand(`${c}/${f.file}`, f.fn, d.change)}`
    : d.dir ? `rm -rf ${q(c)} && mkdir -p ${q(c)} && cp -R ${q(d.dir + '/repo')}/. ${q(c)}/` : null
  if (bare) return make
  return make ? `## Test it in a copy holding the change - never in the project\n\`\`\`bash\n${make}\n\`\`\`\nThen run everything from \`${c}\`.` : null
}

// Decided changes go into the project as soon as they are decided (see `checkpoint`).
const provisional = {}
let sealed = false

// One subtask: its solvers (and, for a function, drafts), a pick or a merge, then a check.
// opts.last: one last solver after the budget, for a subtask that has no result.
async function runSubtask(st, deps, opts = {}) {
  const f = functionOf(st)
  const byScript = st.kind === 'code' && !!f && pickReady
  const audit = byScript && isAudit(st)
  const ph = opts.last ? 'Last' : 'Solve'
  const ks = Array.from({ length: st.solvers }, (_, k) => k + 1)
  const solving = opts.last ? [st.solvers + 2] : ks
  const every = opts.last ? [...ks, ...solving] : ks
  // Every function subtask has two independent candidates, the solver cap notwithstanding.
  const nDrafts = byScript ? Math.max(0, 2 - solving.length) : 0
  const drafted = nDrafts ? drafts(st, f, nDrafts, ph).catch(() => []) : Promise.resolve([])
  // A code solver needs no backup: its copy on disk is read whether or not it replies.
  const outs = await parallel(solving.map(k => () => ask(solvePrompt(st, k, deps), `solve-${st.n}.${opts.last ? 'last' : k}`, ph, shown,
    { pri: k - 1, once: st.kind === 'code', deadline: opts.last ? Math.min(DEADLINE_MS, 900000) : undefined })))
  const replies = {}
  solving.forEach((k, i) => { if (outs[i]) replies[k] = outs[i] })
  const dks = await drafted
  const pool = [...every, ...dks]
  let snapped = null
  if (st.kind === 'code' && !f) {
    // Copies on disk: a reply-less changed copy is a candidate only when no solver that replied
    // left a changed copy, and a reply is never dropped.
    const { changed, seen } = await readCopies(st, every)
    snapped = seen
    if (![...changed].some(k => replies[k])) {
      for (const k of every) {
        if (changed.has(k) && !replies[k]) replies[k] = `## Answer\nSolver ${k}'s change, read from its copy - its reply never arrived.\n\n## Evidence\nThe runtime found Python files in its copy that differ from the original.\n\n## Change\n${snapDir(st, k)}/repo`
      }
    }
  }
  const decide = async kset => {
    if (byScript) {
      const picked = await pickFunction(st, f, kset, replies, audit)
      if (picked) return picked
    }
    const results = kset.map(k => replies[k]).filter(Boolean)
    if (!results.length) return null
    const result = await merge(st, results, st.merge)
    let dir = st.kind === 'code' ? dirOf(st, result, kset, replies, snapped) : null
    if (dir) {
      const frozen = `${SCRATCH}/chosen/s${st.n}`
      if (/frozen/.test(await runExactly(`freeze-${st.n}`, `rm -rf ${q(frozen)} && mkdir -p ${q(frozen)} && cp -R ${q(dir + '/repo')} ${q(frozen + '/repo')} && echo frozen`, 'Pick') || '')) dir = frozen
    }
    return { result, dir, votes: 1 }
  }
  let d = await decide(pool)
  const nReplied = () => Object.keys(replies).length
  if (!d) return { st, result: '', why: 'no solver returned a result', solved: nReplied(), drafts: dks.length }
  // Up to two rounds of readings while the best candidate breaks an example - and no checker
  // or redo then: they only restate a mismatch the script measured. Measured (d12 he10): two
  // checkers restated it, and the redo fitted the example by breaking the docstring's tie rule.
  let all = []
  for (const round of [1, 2]) {
    if (!(byScript && d.mismatches > 0) || late) break
    const rk = await readings(st, f, d.evidence, round)
    if (!rk.length) break
    all = all.concat(rk)
    const again = await pickFunction(st, f, [...pool, ...all], replies, audit)
    d = { ...(again || d), readings: all }
    log(`subtask ${st.n}: readings round ${round}; the chosen candidate breaks ${d.mismatches} example(s)`)
  }
  const counts = { solved: nReplied(), drafts: dks.length }
  // Decided: into the project now, before any check - a kill keeps it.
  if (st.kind === 'code' && !sealed) { provisional[st.n] = { st, ...d, ...counts, check: 'not checked yet' }; checkpoint() }
  if (byScript && d.mismatches > 0) return { st, ...d, ...counts, check: `unsettled: the best candidate breaks ${d.mismatches} docstring example(s)` }
  if (st.check && !late) {
    // The runtime makes the checker's copy and shows the change. Measured (d12 c1913h): 11 of
    // 17 FAILs never built their copy, read the project's old code, and failed fixed code.
    const c = `${SCRATCH}/c${st.n}`
    const make = testCopy(st, d, true)
    const ready = make && /copy ready/.test(await runExactly(`copy-${st.n}`, `${make}\necho copy ready`, 'Check') || '')
    const diff = ready ? await runExactly(`diff-${st.n}`, `cd ${q(c)} && diff -ru -x __pycache__ -x .git ${q(BASE)} . | head -150`, 'Check') : ''
    const checkOnce = n => ask(prompt('check', `Check subtask ${st.n}: ${st.title} (checker ${n})`, [
      '## The request', '```text', TASK, '```', '',
      '## What was asked of this part', `**${st.title}**`, '', specOf(st), '', specOnly(st) ? null : nest(st.task, 3), '',
      ready ? `## The change under check - already in \`${c}\`\n\`\`\`diff\n${diff || '(no difference: the code is kept as it was)'}\n\`\`\`\nRun everything from \`${c}\`. The project folder holds other subtasks' decided work, not this change: judge only the copy. A problem in code this change does not touch belongs to another subtask - name it, it is not a FAIL here.` : testCopy(st, d), '',
      card('The result to check', d.result, { top: 2, evidence: true }),
    ]), `check-${st.n}${n > 1 ? `.${n}` : ''}`, 'Check', md => !!VERDICT(md), { pri: -1 })
    const tested = md => !ready || String(md).includes(c)
    let check = await checkOnce(1)
    if (check && /fail/i.test(VERDICT(check)) && !tested(check)) return { st, ...d, ...counts, check: 'FAIL not tested in the copy - kept' }
    // A failure is acted on only when a second checker, who has not seen the first, finds it
    // too. Measured: one checker's mistaken FAIL brought in a redo that replaced three
    // solvers' correct median.
    const second = check && /fail/i.test(VERDICT(check)) ? await checkOnce(2) : null
    if (second && /fail/i.test(VERDICT(second))) {
      const k = st.solvers + 1
      const feedback = `What the checkers found:\n${section(check, 'Problem') || check}\n${section(second, 'Problem')}\n\nReply in your solve format - \`## Answer\`, \`## Key values\`, \`## Evidence\` - never a verdict.`
      const redo = await ask(solvePrompt(st, k, deps, feedback), `solve-${st.n}.redo`, 'Solve', shown, { pri: 5, once: st.kind === 'code' })
      // The redo joins the original solvers rather than facing the merged result alone, so
      // one new answer cannot outvote three.
      if (redo) replies[k] = redo
      const again = redo || byScript ? await decide([...pool, k, ...(d.readings || [])]) : null
      if (again) d = again
      else if (!section(d.result, 'Answer')) d = { ...d, result: bestOf(Object.values(replies)) }
      if (st.kind === 'code' && !sealed) { provisional[st.n] = { st, ...d, ...counts }; checkpoint() }
      return { st, ...d, ...counts, solved: nReplied(), check: redo ? 'FAIL (confirmed) then redone' : 'FAIL (confirmed); the redo returned nothing' }
    }
    if (check && /fail/i.test(VERDICT(check))) return { st, ...d, ...counts, check: 'FAIL not confirmed by a second checker - kept' }
    return { st, ...d, ...counts, check: check ? (d.changed === false ? `${VERDICT(check)} (no change)` : VERDICT(check)) : 'not checked' }
  }
  return { st, ...d, ...counts, check: st.check ? 'skipped: time budget' : 'not needed' }
}

// ---------------------------------------------------------------------------------
// Putting decided changes into the project - at once, and again at assembly
// ---------------------------------------------------------------------------------

// Every code change goes in by exact command: a chosen function by name, any other change
// region by region from the solver's copy. Measured (d9): solvers named patch files they never
// wrote or wrote by hand, malformed; an assembler then copied one solver's whole file over the
// project.
const status = (cmd, n) => cmd.includes("<<'PY'")
  ? cmd.replace("<<'PY'", `<<'PY' && echo "applied subtask ${n}" || echo "FAILED subtask ${n}"`)
  : `{ ${cmd}; } && echo "applied subtask ${n}" || echo "FAILED subtask ${n}"`
// A whole-copy change goes in region by region - each top-level statement it changed, never a
// function another subtask owns. Measured (d12 c1913): a table audit's patch also rewrote
// `short`; it half-applied, and the assembler stacked its `short` edit on a correct one.
// A region an earlier change already rewrote is merged three ways; a true conflict is FAILED
// for the later subtask, with its other regions still put in.
const REGIONS = `${SCRATCH}/regions.py`
const REGIONS_PY = String.raw`import ast,difflib,os,sys
# A whole-copy change, put in region by region. usage: regions.py BASE CANDIDATE LIVE [owned,fns]
base,cand,live=sys.argv[1:4]; owned=set(filter(None,sys.argv[4].split(','))) if len(sys.argv)>4 else set()
def nodes(src):
    t=src.splitlines(True); out={}; c={}
    for n in ast.parse(src).body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): k='def:'+n.name
        elif isinstance(n,ast.If) and '__main__' in ast.unparse(n.test): k='main'
        elif isinstance(n,(ast.Import,ast.ImportFrom)): k='import:'+ast.unparse(n)
        elif isinstance(n,(ast.Assign,ast.AnnAssign,ast.AugAssign)): k='set:'+ast.unparse(n.targets[0] if isinstance(n,ast.Assign) else n.target)
        else:
            kind=type(n).__name__; c[kind]=c.get(kind,0)+1; k=kind+'#'+str(c[kind])
        s=(n.decorator_list[0].lineno if getattr(n,'decorator_list',None) else n.lineno)-1
        out.setdefault(k,(s,n.end_lineno,''.join(t[s:n.end_lineno])))
    return out
def edits(B,X):
    return [(i1,i2,X[j1:j2]) for op,i1,i2,j1,j2 in difflib.SequenceMatcher(None,B,X,autojunk=False).get_opcodes() if op!='equal']
def overlap(a,b):
    (a1,a2,_),(b1,b2,_)=a,b
    if a1==a2 and b1==b2: return a1==b1
    if a1==a2: return b1<a1<b2
    if b1==b2: return a1<b1<a2
    return a1<b2 and b1<a2
def subseq(small,big):
    it=iter(big); return all(any(x==y for y in it) for x in small)
def merge3(b,l,c):
    # Both sides' edits to one region, three ways. Compatible edits are kept: disjoint lines,
    # one side already holding the other's change (a superset), or both adding lines at the
    # same place (entries appended to one table). Anything else is a true conflict -> None.
    if l==b or l==c: return c
    if c==b: return l
    B,L,C=b.splitlines(True),l.splitlines(True),c.splitlines(True)
    el=[e+('L',) for e in edits(B,L)]; ec=[e+('C',) for e in edits(B,C)]
    es=sorted(el+ec,key=lambda e:(e[0],e[1]))
    clusters=[]
    for e in es:
        if clusters and any(overlap(e[:3],x[:3]) for x in clusters[-1]): clusters[-1].append(e)
        else: clusters.append([e])
    out=[]; i=0
    for cl in clusters:
        s=min(e[0] for e in cl); t=max(e[1] for e in cl)
        ls=[e for e in cl if e[3]=='L']; cs=[e for e in cl if e[3]=='C']
        def version(side):
            v=[]; j=s
            for e in sorted(side,key=lambda e:e[0]): v+=B[j:e[0]]+e[2]; j=e[1]
            return v+B[j:t]
        if not ls or not cs: new=version(ls or cs)
        else:
            lv,cv=version(ls),version(cs)
            ladd=[x for e in ls for x in e[2]]; cadd=[x for e in cs for x in e[2]]
            lrem={k for e in ls for k in range(e[0],e[1])}; crem={k for e in cs for k in range(e[0],e[1])}
            if lv==cv: new=lv
            elif lrem<=crem and subseq(ladd,cadd): new=cv
            elif crem<=lrem and subseq(cadd,ladd): new=lv
            elif not lrem and not crem: new=ladd+[x for x in cadd if x not in ladd]
            else: return None
        out+=B[i:s]+new; i=t
    m=''.join(out+B[i:])
    # Line edits merged into code that no longer parses are a conflict too.
    try: ast.parse(m)
    except SyntaxError: return None
    return m
done=[]; failed=[]
for dp,dn,fs in os.walk(cand):
    dn[:]=[x for x in dn if x not in ('.git','__pycache__','node_modules','.venv','venv')]
    for f in fs:
        if not f.endswith('.py'): continue
        rel=os.path.relpath(os.path.join(dp,f),cand); b=os.path.join(base,rel); l=os.path.join(live,rel)
        if not os.path.exists(b) or not os.path.exists(l): continue
        cs=open(os.path.join(dp,f)).read(); bs=open(b).read()
        if cs==bs: continue
        try:
            B,C=nodes(bs),nodes(cs); src=open(l).read(); L=nodes(src)
        except SyntaxError as e:
            failed.append(rel+': '+str(e)); continue
        lines=src.splitlines(True)
        # Conflicts are counted per file; a file's regions that do not conflict still go in.
        conflicts=[]; put={}
        todo=[k for k in C if k in B and C[k][2]!=B[k][2] and not (k.startswith('def:') and k[4:] in owned)]
        for k in todo:
            if k not in L: conflicts.append(k+' (gone from the project)'); continue
            # A region an earlier change already rewrote is merged three ways, never overwritten.
            # Measured (d13 c1913h): subtask 16 replaced 'main' from its own copy and the
            # '--json' handling subtask 6 had put there was gone.
            m=merge3(B[k][2],L[k][2],C[k][2])
            if m is None: conflicts.append(k)
            elif m!=L[k][2]: put[k]=m
        new=[k for k in C if k not in B and not (k.startswith('def:') and k[4:] in owned)]
        add=[]
        for k in new:
            if k in L:
                # Two subtasks added the same region: the same text, or one holding the other.
                if L[k][2]==C[k][2]: continue
                m=merge3('',L[k][2],C[k][2])
                if m is None: conflicts.append(k)
                else: put[k]=m
            else: add.append(k)
        for k in sorted(put,key=lambda k:-L[k][0]):
            s,e,_=L[k]; lines[s:e]=[put[k].rstrip(chr(10))+chr(10)]
        L2=nodes(''.join(lines)) if put else L
        for k in [k for k in add if k.startswith('import:')]:
            last=max([v[1] for kk,v in L2.items() if kk.startswith('import:')] or [0]); lines.insert(last,C[k][2].rstrip(chr(10))+chr(10))
        tail=[C[k][2] for k in add if not k.startswith('import:')]
        if tail:
            L3=nodes(''.join(lines))
            at=L3['main'][0] if 'main' in L3 else len(lines)
            lines[at:at]=[chr(10).join(x.rstrip(chr(10)) for x in tail)+chr(10)*3]
        if put or add:
            open(l,'w').write(''.join(lines)); done.append(rel+': '+', '.join(list(put)+add))
        for k in conflicts:
            failed.append(f'conflict {rel}: {k} - an earlier subtask changed it too; this subtask\'s version is in {os.path.join(dp,f)}')
print('regions', '; '.join(done) or 'none')
if failed:
    for x in failed: print('could not place', x)
    sys.exit(1)
`
// Every changed module still imports cleanly, with a command line that is not its own.
// Measured (mock): a feature's solver put `args = parser.parse_args()` at module level, and
// importing the module then parsed the importer's arguments and exited. (The d13 c1913h module
// that did this was written by the main loop after the graph, not by a solver.)
const IMPORTS = `${SCRATCH}/imports.py`
const IMPORTS_PY = String.raw`import filecmp,json,os,subprocess,sys
# Every module that differs between BEFORE and AFTER still imports, with a command line that is
# not its own. usage: imports.py BEFORE AFTER PRISTINE
# A failure counts only where the pristine module imported cleanly: a script that never
# imported is not flagged. A module inside a package is imported by its dotted name, so its
# relative imports work. Each import runs in its own process, with a time limit and no stdin.
before,after,pristine=sys.argv[1:4]
SKIP=('.git','__pycache__','node_modules','.venv','venv')
PROBE=r'''import contextlib,importlib,importlib.util,io,os,sys
root,p=sys.argv[1],sys.argv[2]; d=os.path.dirname(p); parts=[os.path.splitext(os.path.basename(p))[0]]; top=d
while top!=root and top.startswith(root) and os.path.exists(os.path.join(top,'__init__.py')):
    parts.insert(0,os.path.basename(top)); top=os.path.dirname(top)
sys.argv=['import-check','--agentaus-not-an-option']; sys.dont_write_bytecode=True
sys.path[:0]=[top,d]; os.chdir(d); out=io.StringIO()
try:
    with contextlib.redirect_stdout(out),contextlib.redirect_stderr(out):
        if len(parts)>1: importlib.import_module('.'.join(parts))
        else:
            s=importlib.util.spec_from_file_location('m_check',p); s.loader.exec_module(importlib.util.module_from_spec(s))
    print('OK')
except BaseException as e:
    print('FAILED',type(e).__name__,(out.getvalue() or str(e))[-300:].replace(chr(10),' '))'''
def probe(root,p):
    try:
        r=subprocess.run([sys.executable,'-c',PROBE,os.path.abspath(root),os.path.abspath(p)],capture_output=True,text=True,timeout=30,stdin=subprocess.DEVNULL,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        last=(r.stdout.strip().splitlines() or ['FAILED no output '+r.stderr[-200:].replace(chr(10),' ')])[-1]
        return last
    except subprocess.TimeoutExpired:
        return 'FAILED TimeoutExpired importing it ran for 30 s'
cache_path=os.path.join(os.path.dirname(os.path.abspath(pristine)),'import-baseline.json')
try: cache=json.load(open(cache_path))
except Exception: cache={}
for dp,dn,fs in os.walk(after):
    dn[:]=[x for x in dn if x not in SKIP]
    for f in sorted(fs):
        if not f.endswith('.py'): continue
        p=os.path.join(dp,f); rel=os.path.relpath(p,after); b=os.path.join(before,rel); o=os.path.join(pristine,rel)
        if os.path.exists(b) and filecmp.cmp(b,p,shallow=False): continue
        if not os.path.exists(o): continue
        now=probe(after,p)
        if now=='OK': print('IMPORT OK',rel); continue
        if rel not in cache: cache[rel]=probe(pristine,o)
        if cache[rel]!='OK': print('IMPORT WAS FAILING',rel,'- the original did not import either'); continue
        print('IMPORT FAILED',rel,now[len('FAILED '):])
try: json.dump(cache,open(cache_path,'w'))
except Exception: pass
`
const MEASURE = `${SCRATCH}/measure.py`
const MEASURE_PY = String.raw`import json,os,re,subprocess,sys,tempfile,time
# What the project does now, measured: each final checker's repro, the docstring examples of
# every changed file, every solver's tests (pointed at the project), and imports.
# usage: measure.py MANIFEST.json OUT.json [BEFORE.json] - writes OUT.json and prints a summary
# first; with BEFORE, also the verdict on a repair: ACCEPT only when fewer repros fail and
# nothing that passed before fails now.
m=json.load(open(sys.argv[1])); cwd=m['cwd']; T0=time.time(); left=lambda: m.get('limit',300)-(time.time()-T0)
res={'repros':[],'examples':{},'tests':{},'imports':{}}; detail=[]
env=dict(os.environ,PYTHONPATH=cwd,PYTHONDONTWRITEBYTECODE='1')
for k,cmd in enumerate(m.get('repros',[])):
    try:
        r=subprocess.run(['/bin/bash','-c',cmd],cwd=cwd,capture_output=True,text=True,timeout=max(5,min(60,left()/2)),stdin=subprocess.DEVNULL,env=env)
        ok=r.returncode==0; detail.append(f'repro {k+1}: {"passes" if ok else "FAILS"} - '+(r.stdout+r.stderr)[-300:].replace(chr(10),' '))
    except Exception as e:
        ok=False; detail.append(f'repro {k+1}: FAILS - {type(e).__name__}')
    res['repros'].append(ok)
if m.get('examples'):
    for rel in m.get('files',[]):
        spec=os.path.join(m['pristine'],rel); cur=os.path.join(cwd,rel)
        if not (os.path.exists(spec) and os.path.exists(cur)) or left()<10: continue
        try: out=subprocess.run([sys.executable,m['examples'],spec,cur],capture_output=True,text=True,timeout=max(5,min(60,left()/2)),stdin=subprocess.DEVNULL).stdout
        except Exception as e: out='MISMATCH import timeout'
        for l in out.splitlines():
            if l.startswith(('ok','MISMATCH')):
                res['examples'][rel+'::'+l.split(' -> ')[0].split(None,1)[-1]]=l.startswith('ok')
td=tempfile.mkdtemp()
for k,t in enumerate(m.get('tests',[])):
    if not os.path.exists(t) or left()<10: continue
    s=open(t).read()
    for d in sorted(m.get('dirs',[]),key=len,reverse=True): s=s.replace(d.rstrip('/')+'/repo',cwd)
    s=s.replace(m['pristine'].rstrip('/'),cwd)
    p=os.path.join(td,f't{k}.py'); open(p,'w').write(s)
    try: ok=subprocess.run([sys.executable,p],cwd=cwd,capture_output=True,timeout=max(3,min(30,left()/4)),stdin=subprocess.DEVNULL,env=env).returncode==0
    except Exception: ok=False
    res['tests'][t]=ok
if m.get('imports'):
    try:
        out=subprocess.run([sys.executable,m['imports'],m['pristine'],cwd,m['pristine']],capture_output=True,text=True,timeout=max(10,min(120,left()))).stdout
        for l in out.splitlines():
            mm=re.match(r'IMPORT (OK|FAILED) (\S+)',l)
            if mm: res['imports'][mm.group(2)]=mm.group(1)=='OK'
    except Exception: pass
json.dump(res,open(sys.argv[2],'w'))
bad=lambda d: sum(1 for v in d.values() if not v)
print(f"MEASURE repros failing {res['repros'].count(False)}/{len(res['repros'])}, examples failing {bad(res['examples'])}/{len(res['examples'])}, tests failing {bad(res['tests'])}/{len(res['tests'])}, imports failing {bad(res['imports'])}/{len(res['imports'])}")
if len(sys.argv)>3:
    b=json.load(open(sys.argv[3]))
    # A module not listed before was the original, which imported: a failure now is lost too.
    lost=[f'{kind} {k}' for kind in ('examples','tests','imports') for k,v in res[kind].items() if v is False and b[kind].get(k,kind=='imports')]
    fewer=res['repros'].count(False)<b['repros'].count(False)
    print('VERDICT','ACCEPT' if fewer and not lost else 'REJECT', '' if fewer else 'no fewer failing repros', ('now failing: '+'; '.join(lost[:5])) if lost else '')
for l in detail: print(l[:400])
`
const PRE = `${SCRATCH}/pre-change`
const owners = new Map()
for (const st of plan.subtasks) { const f = functionOf(st); if (f) owners.set(f.fn, st.n) }
const done = {}
const decided = st => (done[st.n] && done[st.n].result) ? done[st.n] : (provisional[st.n] && provisional[st.n].result) ? provisional[st.n] : null
function changeSet(exclude) {
  const lines = []
  const ns = []
  for (const st of plan.subtasks.filter(st => st.kind === 'code' && decided(st) && !(exclude && exclude.has(st.n)))) {
    const f = functionOf(st)
    const d = decided(st)
    const change = d.change || (f && (String(d.result || '').match(new RegExp(`[^\\s\`'"]+\\/${f.fn}\\.py`)) || [])[0])
    let line = null
    // An unchanged function is never put back. Measured (d12 c1913): a subtask whose chosen copy
    // was the original's put the bug back over another subtask's fix.
    if (f && change) line = d.changed === false ? `echo "no change in subtask ${st.n}"` : status(replaceCommand(`${CWD}/${f.file}`, f.fn, change), st.n)
    else if (d.dir) {
      const others = [...owners].filter(([, n]) => n !== st.n).map(([fn]) => fn).join(',')
      line = `python3 ${q(REGIONS)} ${q(BASE)} ${q(d.dir + '/repo')} ${q(CWD)} ${q(others)} && echo "applied subtask ${st.n}" || echo "FAILED subtask ${st.n}"`
    }
    if (line) { lines.push(line); ns.push(st.n) }
  }
  return { lines, ns }
}
const PY_ONLY = `--exclude .git --exclude __pycache__ --exclude node_modules --exclude .venv --exclude venv --include='*/' --include='*.py' --exclude='*'`
// Each change: the project's .py files are saved, the change is made, and every module it
// touched is imported; a change that breaks an import that worked is undone at once and
// reported. Status lines are printed as they happen and details only at the end: the bridge
// keeps the head and the tail of a long output, and a lost "applied subtask N" line was a
// change counted as failed.
const guarded = (line, n) => `rsync -a --delete ${PY_ONLY} ${q(CWD)}/ ${q(PRE)}/
r=$( {
${line}
} 2>&1 )
printf '%s\\n' "--- subtask ${n}" "$r" >> "$APPLY_LOG"
st=$(printf '%s\\n' "$r" | grep -E '^(applied|FAILED|no change in) subtask ${n}$' | tail -1)
case "$st" in
"no change"*) echo "$st" ;;
*) imp=$(python3 ${q(IMPORTS)} ${q(PRE)} ${q(CWD)} ${q(PRISTINE || BASE)} 2>&1 | grep '^IMPORT FAILED' | head -1)
   if [ -n "$imp" ]; then rsync -a ${PY_ONLY} ${q(PRE)}/ ${q(CWD)}/; echo "FAILED subtask ${n} - undone, it breaks an import: $(printf '%s' "$imp" | cut -c1-240)"
   elif [ "$st" = "applied subtask ${n}" ]; then echo "applied subtask ${n}"
   else echo "FAILED subtask ${n} - $(printf '%s\\n' "$r" | grep -E 'could not place|Error|error' | head -1 | cut -c1-300)"; fi ;;
esac`
// A same-size change in the same second as a restore was loaded from the stale .pyc (mock).
const PYCACHE = `python3 - ${q(PRISTINE || BASE)} ${q(CWD)} <<'PY'\nimport glob,os,sys\np,c=sys.argv[1:3]\nfor dp,dn,fs in os.walk(p):\n    dn[:]=[x for x in dn if x not in ('.git','__pycache__','node_modules','.venv','venv')]\n    for f in fs:\n        if f.endswith('.py'):\n            for x in glob.glob(os.path.join(c,os.path.relpath(dp,p),'__pycache__',f[:-3]+'.*.pyc')):\n                try: os.remove(x)\n                except OSError: pass\nPY`
let applied = ''
let changeNs = []
let failedChanges = []
let restoreOk = !PRISTINE
let puts = 0
// The project from the original plus every decided change, built again each time - so a kill
// at any moment leaves every change decided so far in place. Measured (d13 c1913): the kill
// came before Assemble, and twelve decided, checked subtasks never reached the project.
async function putInPlace(final, exclude) {
  if (!PRISTINE) return
  const { lines, ns } = changeSet(exclude)
  const log_ = `${SCRATCH}/apply.log`
  const cmd = [
    `mkdir -p ${q(SCRATCH)} ${q(PRE)} && cat > ${q(REGIONS)} <<'AGENTAUS_REGIONS_END' && cat > ${q(IMPORTS)} <<'AGENTAUS_IMPORTS_END'\n${REGIONS_PY}AGENTAUS_REGIONS_END\n${IMPORTS_PY}AGENTAUS_IMPORTS_END`,
    `APPLY_LOG=${q(log_)}; : > "$APPLY_LOG"`,
    // The project goes back to how it was when the work began: every edit made since - by anyone -
    // is undone, and only the decided changes go in. Files created since are left.
    `out=$(rsync -ai --checksum --exclude .git --exclude __pycache__ --exclude node_modules --exclude .venv --exclude venv --exclude '*.pyc' ${q(PRISTINE)}/ ${q(CWD)}/ 2>&1); rc=$?; echo "restored rc=$rc"; printf '%s\\n' "--- restore" "$out" | grep -v '/$' | head -40 >> "$APPLY_LOG"`,
    ...lines.map((l, k) => guarded(l, ns[k])),
    `find ${q(CWD)} \\( -name '*.orig' -o -name '*.rej' \\) -newer ${q(SCRATCH + '/pristine')} -delete 2>/dev/null`,
    PYCACHE,
    `echo "--- details"; tail -c 2500 "$APPLY_LOG"`,
  ].join('\n')
  puts += 1
  const out = await runExactly(final ? 'apply-changes' : `checkpoint-${puts}`, cmd, 'Assemble')
  restoreOk = /^restored rc=0\s*$/m.test(out || '')
  if (!restoreOk) log(`${final ? 'restore' : 'checkpoint'}: the restore failed - changes go in over whatever the project holds now`)
  applied = out || '(the changes could not be applied)'
  changeNs = ns
  // A change counts as applied only when its own line says so. Measured (d10): a reply that
  // dropped every FAILED line was read as "0 failed", and no one made the changes by hand.
  const okNs = new Set([...applied.matchAll(/^(?:applied|no change in) subtask (\d+)\s*$/gm)].map(m => Number(m[1])))
  failedChanges = changeNs.filter(n => !okNs.has(n))
  if (!final) log(`checkpoint: ${okNs.size} of ${ns.length} decided change(s) in the project`)
}
// Checkpoints are coalesced: one runs at a time, and any number asked for meanwhile become one more.
let cpRun = null
let cpDirty = false
function checkpoint() {
  if (!PRISTINE || sealed) return cpRun
  if (cpRun) { cpDirty = true; return cpRun }
  cpRun = (async () => {
    do {
      cpDirty = false
      try { await putInPlace(false) } catch (e) { log(`checkpoint: ${e}`) }
    } while (cpDirty && !sealed)
    cpRun = null
  })()
  return cpRun
}

// ---------------------------------------------------------------------------------
// Running the plan: each subtask as soon as its own dependencies are done
// ---------------------------------------------------------------------------------

phase('Solve')
startBudget()
pickReady = plan.subtasks.some(st => st.kind === 'code' && functionOf(st))
const byN = new Map(plan.subtasks.map(st => [st.n, st]))
{
  // A dependency on a missing subtask, or one that closes a cycle, is dropped.
  const state = new Map()
  const visit = st => {
    state.set(st.n, 1)
    st.deps = [...new Set(st.deps)].filter(d => {
      if (!byN.has(d) || state.get(d) === 1) return false
      if (!state.get(d)) visit(byN.get(d))
      return true
    })
    state.set(st.n, 2)
  }
  for (const st of plan.subtasks) if (!state.get(st.n)) visit(st)
}
const hasResult = st => !!(done[st.n] && done[st.n].result)
const depCards = st => st.deps.filter(d => hasResult(byN.get(d))).map(d => card(`Subtask ${d}: ${done[d].st.title}`, done[d].result))
const record = (st, o, note) => {
  if (sealed) return
  done[st.n] = o && o.result ? { ...o, check: note ? `${o.check} - ${note}` : o.check } : { st, result: '', why: (o && o.why) || 'failed', solved: o && o.solved, drafts: o && o.drafts }
  if (st.kind === 'code') checkpoint()
}
// One promise per subtask, waiting only on its own dependencies. Measured (d12 c1913h, the
// wave loop): subtasks 9-12 waited for every subtask of the first wave, the budget ran out,
// and they never ran.
const startedRuns = new Map()
function start(st) {
  if (!startedRuns.has(st.n)) {
    startedRuns.set(st.n, (async () => {
      await Promise.all(st.deps.map(d => start(byN.get(d))))
      // Once the budget is spent nothing new starts here; the last-chance pass below takes it.
      if (late || hard) return
      record(st, await runSubtask(st, depCards(st)).catch(e => { log(`subtask ${st.n}: ${e}`); return null }))
    })())
  }
  return startedRuns.get(st.n)
}
const solving = (async () => {
  await Promise.all(plan.subtasks.map(start))
  // After the budget, every subtask with no result - of any kind - gets one last solver while
  // the graph's own clock allows. Measured (d12 c1913h): subtasks queued behind the budget never
  // ran, and "(no result)" went into the deliverable as their section.
  const bare = plan.subtasks.filter(st => !hasResult(st) && !(provisional[st.n] && provisional[st.n].result))
  if (bare.length && !hard) {
    log(`last chance: one solver each for subtask(s) ${bare.map(st => st.n).join(', ')}`)
    await Promise.all(bare.map(st => runSubtask(st, depCards(st), { last: true })
      .then(o => record(st, o, 'solved after the budget'), e => log(`subtask ${st.n}: ${e}`))))
  }
})()
await Promise.race([solving, hardStop])
sealed = true
for (const st of plan.subtasks) if (!hasResult(st) && provisional[st.n] && provisional[st.n].result) done[st.n] = provisional[st.n]

const resultOf = st => (done[st.n] && done[st.n].result) || ''
const RESULTS = plan.subtasks.map(st => card(`Subtask ${st.n}: ${st.title}`, resultOf(st), { status: done[st.n] && done[st.n].check })).join('\n\n---\n\n')
// Every key value, as one checklist the assembler fills and the final checkers tick off.
// Paths are not values; a code change is one item; a value no checker passed is marked, and
// loses to the README or a docstring. Measured (d9): an unchecked "10 / 2" from one solver
// failed both final checkers and sent a fixer to break a correct file.
const CHECKLIST = plan.subtasks.flatMap(st => {
  const trusted = /^PASS/i.test(String((done[st.n] && done[st.n].check) || ''))
  const tag = trusted ? '' : ' _(unchecked: where the README or a docstring disagrees, they win - report it, it is not a FAIL by itself)_'
  if (st.kind === 'code') {
    const f = functionOf(st)
    return [`- [ ] Subtask ${st.n} - **${st.title}**: the change is in ${f ? `\`${f.fn}\` in \`${f.file}\`` : 'the project'}${tag}`]
  }
  const keys = keyValues(resultOf(st)).filter(k => !/\/tmp\/|\.(py|patch)\b|^- \*\*[^*]*(path|file)[^*]*:\*\*/i.test(k))
  return keys.length ? keys.map(k => k.replace(/^- /, `- [ ] Subtask ${st.n} - `) + tag) : [`- [ ] Subtask ${st.n} - **${st.title}**: its answer is in the deliverable${tag}`]
}).join('\n')

// ---------------------------------------------------------------------------------
// 6. Assemble, then a final check by two checkers and a measured repair
// ---------------------------------------------------------------------------------

phase('Assemble')
if (cpRun) await Promise.race([cpRun, endStop])
// Every deliverable at its exact place in the project folder. Measured: an assembler wrote a
// correct answers.json into the scout's scratch folder, and the main loop - finding none in
// the project - computed its own, wrongly.
function inProject(d) {
  const p = String(d || '').trim().replace(/^\.\//, '')
  if (!p) return null
  if (p.startsWith(CWD + '/')) return p
  return `${CWD}/${p.startsWith('/') ? p.split('/').pop() : p}`
}
const deliverables = [...new Set(plan.deliverables.map(inProject).filter(Boolean))]
await putInPlace(true)
// The program still runs: every command the request names, and each changed script, run on the
// project and on the original. A command that ran cleanly on the original and now crashes is a
// regression; the change behind it is found by taking changes back one at a time - tables and
// audits first, reported symptoms and features last - and is left out. Measured (d15 c1913h): a
// table audit dropped a theme from DOMAIN; `--json` then died with KeyError, every check failed,
// and the import check passed - importing never ran the code.
const SMOKE_PY = String.raw`import json,os,subprocess,sys
cwd,pristine,cmds,cache=sys.argv[1],sys.argv[2],json.loads(sys.argv[3]),sys.argv[4]
def run(root,c):
    try:
        r=subprocess.run(['/bin/bash','-c',c],cwd=root,capture_output=True,text=True,timeout=90,stdin=subprocess.DEVNULL,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        bad=r.returncode!=0 or 'Traceback (most recent call last)' in r.stderr
        return bad,(r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or [''])[-1][:200]
    except subprocess.TimeoutExpired:
        return True,'timed out after 90 s'
try: base=json.load(open(cache))
except Exception: base={}
for c in cmds:
    if c not in base: base[c]=run(pristine,c)[0]
json.dump(base,open(cache,'w'))
new=[]
for c in cmds:
    bad,why=run(cwd,c)
    if bad and not base[c]: new.append(c); print('SMOKE CRASH',c,'-',why)
print('SMOKE OK' if not new else f'SMOKE {len(new)} new crash(es)')
`
const SMOKE_CMDS = (() => {
  if (!PRISTINE || !plan.subtasks.some(st => st.kind === 'code')) return []
  const asked = [...TASK.matchAll(/\`(python3?\s+[^\`\n]+)\`/g)].map(m => m[1].replace(/^python\s/, 'python3 ').trim())
  const scripts = FUNCTIONS.length ? [...new Set(FUNCTIONS.map(f => f.file))].filter(f => asked.every(c => !c.includes(f))).map(f => `python3 ${f}`) : []
  return [...new Set([...asked, ...scripts])].slice(0, 6)
})()
let takenBack = []
async function smokeCheck() {
  const out = await runExactly(`smoke-${takenBack.length}-${puts}`, `python3 - ${q(CWD)} ${q(PRISTINE)} ${q(JSON.stringify(SMOKE_CMDS))} ${q(`${SCRATCH}/smoke-baseline.json`)} <<'AGENTAUS_SMOKE_END'
${SMOKE_PY}AGENTAUS_SMOKE_END`, 'Assemble') || ''
  return { crash: /^SMOKE CRASH/m.test(out), ok: /^SMOKE OK/m.test(out), out }
}
if (SMOKE_CMDS.length && changeNs.length && !ended) {
  let s = await smokeCheck()
  const rank = n => { const st = byN.get(n); const t = (st && st.title) || ''
    return /^\W*Audit the module-level/i.test(t) ? 0 : /^\W*Audit\b/i.test(t) ? 1 : /^\W*Docs:/i.test(t) ? 2 : /^\W*Feature\b|--[a-z]/i.test(t) ? 4 : 3 }
  for (let round = 0; s.crash && round < 3 && !ended; round++) {
    const cands = changeNs.filter(n => !failedChanges.includes(n) && !takenBack.includes(n)).sort((a, b) => rank(a) - rank(b))
    let found = null
    for (const n of cands) {
      if (ended) break
      await putInPlace(true, new Set([...takenBack, n]))
      const t = await smokeCheck()
      if (!t.crash) { found = n; break }
    }
    if (found === null) break
    takenBack.push(found)
    log(`smoke: subtask ${found} taken back - ${(s.out.match(/^SMOKE CRASH .*$/m) || ['a command crashed with it'])[0].slice(0, 200)}`)
    s = { crash: false, out: '' }
  }
  // The project as decided, less what was taken back.
  await putInPlace(true, new Set(takenBack))
  if (takenBack.length) failedChanges = [...new Set([...failedChanges, ...takenBack])]
  if (!takenBack.length && s.crash) log('smoke: a command crashes and no single change is behind it')
}
const importFailed = applied.match(/^FAILED subtask \d+ - undone, it breaks an import.*$/gm) || []
if (importFailed.length) log(`assemble: ${importFailed.length} change(s) undone - they broke an import`)
if (changeNs.length) log(`assemble: ${changeNs.length - failedChanges.length} of ${changeNs.length} code change(s) put in place${restoreOk ? '' : ' - THE RESTORE FAILED'}`)
// Prose deliverables built from one section per subtask are written section by section:
// one agent per section, each running one exact command. A single assembler asked to write
// all nine answers claimed it had, and the file held two.
const SECTION_FILE = /\.(md|txt)$/i
const sectionMode = deliverables.length === 1 && SECTION_FILE.test(deliverables[0])
  && plan.subtasks.length > 1 && plan.subtasks.every(st => st.kind === 'text')
const sectionsWritten = []
const sectionPart = k => `${SCRATCH}/sections/${String(k + 1).padStart(2, '0')}.md`
async function writeSection(k, st) {
  const body = section((done[st.n] && done[st.n].result) || '', 'Answer') || (done[st.n] && done[st.n].result) || 'NOT ANSWERED YET - the graph ran out of time for this question.'
  // Exactly one heading per section: the subtask's own. Measured (d9): an answer starting
  // `#### Q8` was given a `## Q8` above it, and the doubled heading hid the section.
  const strip = b => b.trim().replace(/^(#{1,6}[^\n]*\n+)+/, '').replace(/^#{1,6}\s+(.*)$/gm, '**$1**')
  const text = plain(`## ${st.title}\n\n${strip(body)}`)
  // Each section is its own file, written with `>`: running the command twice cannot write
  // it twice. Measured: a section writer ran its `>>` append twice, and the duplicate failed
  // both final checkers.
  const part = sectionPart(k)
  return !!(await runExactly(`section-${k + 1}`, `mkdir -p ${q(`${SCRATCH}/sections`)} && cat > ${q(part)} <<'AGENTAUS_SECTION_END'\n${text}\n\nAGENTAUS_SECTION_END\nwc -l ${q(part)}`, 'Assemble'))
}
const joinSections = file => runExactly('join-sections', `cat ${q(`${SCRATCH}/sections`)}/*.md > ${q(file)} && grep -c '^## ' ${q(file)} && wc -l ${q(file)}`, 'Assemble')
if (sectionMode) {
  for (const [k, st] of plan.subtasks.entries()) sectionsWritten.push(await writeSection(k, st))
  await joinSections(deliverables[0])
}
const allCode = deliverables.every(d => /\.(py|js|ts|mjs|cjs|sh)$/.test(d)) && plan.subtasks.every(st => st.kind === 'code')
const codeOnly = changeNs.length > 0 && !failedChanges.length && restoreOk && allCode
// After the hard stop no assembler starts on a code-only plan: every change is in place by
// exact command, and the ones that are not are listed for the main loop. Measured (arithmetic):
// an assembler with the writer's 1620 s after a late hard stop ran past the 7200 s kill.
const skipWriter = hard && allCode && changeNs.length > 0
const assembled = sectionMode ? `## Written\n- \`${deliverables[0]}\` - ${sectionsWritten.filter(Boolean).length} of ${plan.subtasks.length} sections`
  : codeOnly || skipWriter ? `## Written\n${applied}${failedChanges.length ? `\n\n**Not in place - make these by hand:** ${failedChanges.map(n => `subtask ${n}`).join(', ')}` : ''}`
  : ended ? null
  : await ask(prompt('assemble', 'Build the deliverables', [
  '## The request', '```text', TASK, '```', '',
  '## Deliverables - write each at exactly this path, in the project folder',
  deliverables.length ? deliverables.map(d => `- \`${d}\``).join('\n') : `(as the request names them - in \`${CWD}\`, never in a scratch folder)`, '',
  '## Assembly', plan.assembly || 'As the request describes.', '',
  changeNs.length ? `## Code changes - already put in place by the runtime\n\`\`\`text\n${applied}\n\`\`\`\n${failedChanges.length ? `**Make each FAILED change by hand**, with small \`Edit\`s, from that subtask's Answer below - for a conflict, merge both changes into that region by hand: ${failedChanges.map(n => `subtask ${n}`).join(', ')}.` : 'Do not change the code again.'}` : null, '',
  takenBack.length ? `## Changes taken back - with each in place, a command the request names crashed\n${takenBack.map(n => `- subtask ${n}: ${(byN.get(n) || {}).title || ''}`).join('\n')}\nRedo each by hand only in a form that keeps every command running.` : null, '',
  importFailed.length ? `## Changes undone because they broke an import - make each by hand\n\`\`\`text\n${importFailed.join('\n')}\n\`\`\`\nImporting a module must run nothing: put command-line code (argument parsing, printing, exiting) under \`if __name__ == "__main__":\`, keeping what it does. Then import it again to check.` : null, '',
  '## Checklist - every item must end up in a deliverable, exactly as written', CHECKLIST, '',
  '## Merged results', RESULTS,
]), 'assemble', 'Assemble', null, { wait: true, deadline: hard ? Math.round(CLOSE_MS / 3) : undefined })

// Text deliverables in plain characters: hyphens, spaces and brackets as typed on a keyboard.
// Measured: mergers wrote `GST‑registered` and `355‑30` with non-breaking hyphens, and cited
// sources in 【lenticular brackets】 - text that no search for the plain words finds.
const NORMALISE = f => `python3 - ${q(f)} <<'PY'\nimport re,sys\np=sys.argv[1]; t=open(p,encoding='utf-8').read(); n=len(t)\nt=re.sub('[\\u2010\\u2011\\u2012\\u2212]','-',t)\nt=re.sub('[\\u00a0\\u2007\\u2009\\u202f]',' ',t)\nt=re.sub('\\u3010([^\\u3011\\n]{1,80})\\u3011',r'[\\1]',t)\nopen(p,'w',encoding='utf-8').write(t); print('normalised',p,n,len(t))\nPY`
const normalise = async () => {
  for (const d of deliverables.filter(d => SECTION_FILE.test(d))) await runExactly('normalise', NORMALISE(d), 'Assemble')
}
await normalise()

phase('Final check')
const codeMode = !!PRISTINE && plan.subtasks.some(st => st.kind === 'code')
// The final checkers check the deliverables against the merged results - present, complete,
// matching, in the right format - and do not recompute them. Measured: two final checkers
// recomputed four correct values, both fell into the same trap, and the fixer overwrote them.
const finalPrompt = n => prompt('check', `Check the finished work (checker ${n})`, [
  '## What was asked', '```text', TASK, '```', '',
  EXAMPLES && PRISTINE && FUNCTIONS.length ? `**First, run the docstring examples on each changed Python file and paste the last line:** \`python3 ${q(EXAMPLES)} ${q(PRISTINE)}/FILE ${q(CWD)}/FILE\` - a \`MISMATCH\` line is a FAIL.\n` : null,
  '## Your job here',
  '- Each deliverable exists at its path and has every part the request asks for, in the format it asks for.',
  '- Each value or section in it **matches the merged result below**. Do **not** recompute values. A deliverable that matches its merged result PASSES on that point.',
  '- Code still runs: **run** every command the request names, exactly as written, and the program with no arguments too.',
  '- Importing a changed module runs nothing: argument parsing, printing or exiting at module level, outside `if __name__ == "__main__":`, is a FAIL.',
  '- Output the request asks for as JSON or CSV: pipe the exact command into the real parser (`| python3 -c "import json,sys; json.load(sys.stdin)"`). Reading it by eye is not a check; a parse error is a FAIL.',
  '- Each Python file changed: `python3 -c "import ast,sys,collections as c; print([k for k,v in c.Counter(n.name for n in ast.parse(open(sys.argv[1]).read()).body if hasattr(n,\'name\')).items() if v>1])" FILE` must print `[]` - a name defined twice means code was pasted twice: FAIL.',
  EXAMPLES && PRISTINE && FUNCTIONS.length ? `- Each Python file with docstring examples: \`python3 ${q(EXAMPLES)} ${q(PRISTINE)}/FILE ${q(CWD)}/FILE\` - a \`MISMATCH\` line is a FAIL; quote it.` : null,
  codeMode ? '- **For each problem in code, give a repro** under `## Repro`: one ```bash block per problem, run from the project folder, that exits non-zero while the problem is there and zero once it is fixed - e.g. `python3 -c "from tools.x import f; assert f(3) == 6, f(3)"` or `python3 prog.py --json | python3 -c "import json,sys; json.load(sys.stdin)"`. Base it on the request, a docstring or the README - never on a docstring example (those already run), and never on what you think the code should do beyond them.' : null, '',
  '## Checklist - copy it under `## Checklist` in your reply, ticking `[x]` each item the deliverable holds and leaving `[ ]` with what you found for each it does not',
  CHECKLIST, '',
  '## Merged results', RESULTS, '',
  `Check every deliverable in \`${CWD}\` against every part of the request.`,
  deliverables.length ? `The deliverables: ${deliverables.map(d => `\`${d}\``).join(', ')}. A deliverable that is not at its path is a FAIL.` : null,
])
// After the hard stop each final checker gets one attempt, clipped.
const finalOpts = hard ? { once: true, deadline: Math.min(420000, Math.round(CLOSE_MS / 3)) } : {}
const finals = ended ? [] : (await parallel([1, 2].map(n => () => ask(finalPrompt(n), `final-check-${n}`, 'Final check', md => !!VERDICT(md), finalOpts)))).filter(Boolean)
const failing = finals.filter(md => /fail/i.test(VERDICT(md)))
let fix = null
let recheck = null
let restored = false
let repairNote = null
let reprosLeft = null
let repaired = []
const backupDir = `${SCRATCH}/before-fix`
const takeBackup = () => runExactly('backup', `rm -rf ${q(backupDir)} && mkdir -p ${q(backupDir)} && cp -R ${q(CWD)}/. ${q(backupDir)}/ && echo saved`, 'Final check')
const undo = async why => {
  restored = /undone/.test(await runExactly('undo', `rsync -a --delete --exclude .git ${q(backupDir)}/ ${q(CWD)}/ && echo undone`, 'Final check') || '')
  log(`final repair: ${why}${restored ? ' - undone' : ' - THE UNDO FAILED'}`)
}
// A sectioned document is repaired section by section: the failing subtasks are solved again
// and their sections rewritten. Measured (d9): a whole-file fixer spent 17 minutes editing
// text that did not match the file, and was undone.
if (sectionMode && finals.length >= 2 && failing.length === finals.length && !hard) {
  const named = new Set()
  for (const md of failing) {
    for (const m of section(md, 'Checklist').matchAll(/^\s*[-*]\s*\[ \]\s*Subtask\s+(\d+)/gim)) named.add(Number(m[1]))
    for (const m of section(md, 'Problem').matchAll(/\bQ(\d{1,2})\b/g)) {
      const st = plan.subtasks.find(o => new RegExp(`^\\W*Q${m[1]}\\b`, 'i').test(o.title))
      if (st) named.add(st.n)
    }
  }
  const targets = plan.subtasks.filter(st => named.has(st.n)).slice(0, 4)
  if (targets.length && await takeBackup()) {
    const problems = failing.map(md => section(md, 'Problem')).join('\n')
    // The repair's solvers run past the budget: within the graph's own clock, they are the point.
    const wasLate = late
    late = false
    for (const st of targets) {
      const again = await runSubtask({ ...st, check: false, task: `${st.task}\n\n**The final checkers found a problem with this part:**\n${problems}` }, [], { last: true })
      if (again && again.result && section(again.result, 'Answer')) { done[st.n] = again; repaired.push(st.title) }
    }
    late = wasLate
    if (repaired.length) {
      for (const [k, st] of plan.subtasks.entries()) if (repaired.includes(st.title)) await writeSection(k, st)
      await joinSections(deliverables[0])
      await normalise()
      recheck = ended ? null : await ask(finalPrompt(3), 'final-check-3', 'Final check', md => !!VERDICT(md), { once: true })
      if (recheck && /fail/i.test(VERDICT(recheck))) await undo('the rewritten sections failed the re-check')
    }
  }
}
// Code is repaired against measurements: each final checker's repro, the docstring examples,
// every solver's tests and the imports, before and after. A repair is kept only when fewer
// repros fail and nothing that passed before fails now - and a repro that runs a docstring
// example is dropped, since the examples are the spec. Measured (d10-d13): final checkers
// named real bugs (`len(same)`, "twelve", `--json`) and no fixer ran in any late run; (d9) two
// checkers fell into the same trap and an unmeasured fixer acted on it.
const exampleCalls = FUNCTIONS.flatMap(f => (String(f.doc || '').match(new RegExp(`\\b${f.fn}\\([^\\n]*\\)`, 'g')) || []).map(c => c.replace(/\s+/g, '')))
const reprosOf = md => [...section(md, 'Repro').matchAll(/```[a-z]*\n([\s\S]*?)```/g)].map(m => m[1].trim()).filter(Boolean)
if (codeMode && !sectionMode && failing.length && !ended) {
  const all = [...new Set(failing.flatMap(reprosOf))]
  const repros = all.filter(r => !exampleCalls.some(c => r.replace(/\s+/g, '').includes(c))).slice(0, 8)
  if (all.length > repros.length) log(`final repair: ${all.length - repros.length} repro(s) dropped - they run a docstring example`)
  const tests = plan.subtasks.filter(st => st.kind === 'code').flatMap(st => Array.from({ length: st.solvers + 2 }, (_, k) => `${solverDir(st, k + 1)}/tests.py`))
  const dirs = plan.subtasks.filter(st => st.kind === 'code').flatMap(st => Array.from({ length: st.solvers + 2 }, (_, k) => solverDir(st, k + 1)))
  const files = [...new Set(FUNCTIONS.map(f => f.file))]
  const manifest = JSON.stringify({ cwd: CWD, pristine: PRISTINE, examples: EXAMPLES || null, imports: IMPORTS, repros, tests, dirs, files, limit: 240 })
  const measure = (out, before) => runExactly(before ? 'measure-after' : 'measure-before',
    `cat > ${q(MEASURE)} <<'AGENTAUS_MEASURE_END' && cat > ${q(SCRATCH + '/measure.json')} <<'AGENTAUS_MANIFEST_END'\n${MEASURE_PY}AGENTAUS_MEASURE_END\n${manifest}\nAGENTAUS_MANIFEST_END\npython3 ${q(MEASURE)} ${q(SCRATCH + '/measure.json')} ${q(out)}${before ? ` ${q(before)}` : ''}`, 'Final check')
  // With no repro, both checkers must have failed, as before; a repro measures on its own.
  const enough = repros.length || (finals.length >= 2 && failing.length === finals.length)
  const m0 = enough ? await measure(`${SCRATCH}/measure-before.json`) : null
  const failingRepros = Number((String(m0 || '').match(/repros failing (\d+)\//) || [])[1] || 0)
  if (m0 && (failingRepros > 0 || !repros.length) && !ended && await takeBackup()) {
    const lines = String(m0).split('\n').filter(l => /^repro \d+: FAILS/.test(l))
    fix = await ask(prompt('fix', 'Fix the problem the checkers found', [
      '## What was asked', '```text', TASK, '```', '',
      '## What the checkers found', ...failing.map(md => section(md, 'Problem') || md), '',
      repros.length ? ['## Repros - each must exit 0 when you are done; run each before and after', ...repros.map((r, k) => `### Repro ${k + 1}\n\`\`\`bash\n${r}\n\`\`\``), '', '## What they print now', '```text', lines.join('\n') || '(see above)', '```'].join('\n') : null, '',
      '## Rules',
      '- Change only what a failing repro needs, with small `Edit`s, in the project folder.',
      '- The docstring examples are the spec: never change a function so that one of its examples breaks, and never edit a docstring. A repro that disagrees with a docstring example is wrong - leave it failing and say so.',
      '- Your change is measured: kept only if fewer repros fail and every docstring example, import and solver test that passed before still passes.', '',
      '## Merged results', RESULTS,
    ]), 'fix', 'Final check', null, { wait: true, deadline: hard ? Math.round(CLOSE_MS / 3) : undefined })
    if (!fix) await undo('the fixer returned nothing')
    else if (ended) await undo('the graph\'s time ran out before the fix could be measured')
    else {
      const m1 = await measure(`${SCRATCH}/measure-after.json`, `${SCRATCH}/measure-before.json`)
      const verdict = (String(m1 || '').match(/^VERDICT .*$/m) || ['VERDICT REJECT the measurement did not run'])[0]
      reprosLeft = Number((String(m1 || '').match(/repros failing (\d+)\//) || [])[1])
      // With no repro, the old rule: a fresh checker must not still fail it.
      if (/ACCEPT/.test(verdict) && !repros.length) recheck = await ask(finalPrompt(3), 'final-check-3', 'Final check', md => !!VERDICT(md), { once: true })
      if (!/ACCEPT/.test(verdict)) await undo(verdict.replace(/^VERDICT\s*/, ''))
      else if (recheck && /fail/i.test(VERDICT(recheck))) await undo('the re-check still failed it')
      else repairNote = `kept: ${String(m1).split('\n')[0]}`
    }
  }
}
// Prose and values: the fixer runs only when both checkers find a problem, and is undone if a
// fresh checker still finds one - or if it returned nothing.
if (!codeMode && !sectionMode && finals.length >= 2 && failing.length === finals.length && !hard && await takeBackup()) {
  fix = await ask(prompt('fix', 'Fix the problem the checkers found', [
    '## What was asked', '```text', TASK, '```', '',
    '## What both checkers found', ...failing.map(md => section(md, 'Problem') || md), '',
    '## Items the checkers found missing', ...[...new Set(failing.flatMap(md => section(md, 'Checklist').split('\n').filter(l => /^\s*[-*]\s*\[ \]/.test(l))))], '',
    '## Merged results - locked', 'A deliverable value that matches one of these is correct: **do not change it**, whatever a recomputation says. Fix only what is missing, misplaced or malformed.', '',
    RESULTS,
  ]), 'fix', 'Final check', null, { wait: true })
  if (fix) await normalise()
  recheck = fix && !ended ? await ask(finalPrompt(3), 'final-check-3', 'Final check', md => !!VERDICT(md), { once: true }) : null
  if (!fix) await undo('the fixer returned nothing')
  else if (recheck && /fail/i.test(VERDICT(recheck))) await undo('the re-check still failed it')
}

// Settled: a function whose subtask was measured - a PASS from a check of a real change, and two
// or more candidates behaving alike or a script pick that ran docstring examples - that only it
// changed, that no failing final checker names, and whose live source is still the frozen
// choice. Measured (d13 c1913h): the list named `_clean` and `parse`, the two wrong functions -
// one a single opinion PASS, the other "no change" - while a final checker named `_clean`; the
// main loop never touched it.
let settledLine = null
{
  const namedByFinal = fn => failing.some(md => new RegExp(`\\b${fn}\\b`).test(`${section(md, 'Problem')}\n${section(md, 'Repro')}`))
  const measured = d => /^PASS/i.test(String(d.check || '')) && !/no change/i.test(String(d.check || '')) && d.changed !== false
    && ((d.votes || 1) >= 2 || (d.picked && d.examples > 0 && d.mismatches === 0))
  const cands = plan.subtasks.filter(st => st.kind === 'code' && functionOf(st) && done[st.n] && done[st.n].change
    && measured(done[st.n]) && owners.get(functionOf(st).fn) === st.n && !namedByFinal(functionOf(st).fn))
  if (cands.length && !ended) {
    const cmp = cands.map(st => { const f = functionOf(st)
      return `python3 - ${q(`${CWD}/${f.file}`)} ${f.fn} ${q(done[st.n].change)} <<'PY'\nimport ast,sys\nt,fn,c=sys.argv[1:4]\ndef src(p):\n    s=open(p).read(); l=s.splitlines(True)\n    for n in ast.parse(s).body:\n        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn: return ''.join(l[n.body[1 if ast.get_docstring(n) is not None and len(n.body)>1 else 0].lineno-1:n.end_lineno]).strip()\ntry:\n    print('same' if src(t)==src(c) else 'differs', fn)\nexcept Exception as e:\n    print('differs', fn)\nPY` }).join('\n')
    const out = await runExactly('settled', cmp, 'Final check') || ''
    const same = [...out.matchAll(/^same (\w+)/gm)].map(m => `\`${m[1]}\``)
    if (same.length) settledLine = `**Settled - do not edit:** ${same.join(', ')}. Each was measured - its docstring examples or two agreeing candidates - passed its check, and is still the chosen version; a test you write that disagrees with one is more likely wrong than the function. Never edit a docstring.`
  }
}
for (const t of timers) clearTimeout(t)

const missing = plan.subtasks.filter(st => !hasResult(st))
const repairedOk = !!repairNote && reprosLeft === 0
const passed = restoreOk && !failedChanges.length && !missing.length && finals.length > 0
  && (failing.length < finals.length || !!(recheck && /pass/i.test(VERDICT(recheck))) || repairedOk)
// What each part really had: solvers that replied, drafts, and its check - never "several agents"
// where there was one.
const candidatesOf = st => { const d = done[st.n] || {}; return (d.solved || 0) + (d.drafts || 0) }
const thin = plan.subtasks.filter(st => candidatesOf(st) < 2 || !/^PASS/i.test(String((done[st.n] && done[st.n].check) || '')))
const lines = [
  `# Result: ${passed ? 'checked' : 'needs attention'}`, '',
  `Plan: ${plan.subtasks.length} subtask(s), ${total} solver(s).${late ? ' **The time budget ran out:** check the deliverables yourself.' : ''}`, '',
  '## Subtasks',
  ...plan.subtasks.map(st => { const d = done[st.n] || {}
    return `- ${st.n}. ${st.title} - ${st.difficulty}, ${d.solved || 0} solver answer(s) (${st.solvers} planned)${d.drafts ? `, ${d.drafts} draft(s)` : ''}${d.votes > 1 ? `, ${d.votes} candidates agree` : ''}, check: ${d.check || 'n/a'}` }),
  '', '## Assembly', assembled || '(the assembler returned nothing)',
  '', '## Final check', ...finals.map((md, k) => `- Checker ${k + 1}: ${VERDICT(md)} - ${section(md, 'Problem').slice(0, 1500)}`),
  !finals.length ? '- No final checker answered.' : null,
  repaired.length ? `- Sections solved again and rewritten: ${repaired.join(', ')}` : null,
  fix ? `- Fixer: ${section(fix, 'Fixed').slice(0, 300)}` : null,
  recheck ? `- Re-check: ${VERDICT(recheck)} - ${section(recheck, 'Problem').slice(0, 300)}` : null,
  repairNote ? `- The repair was measured and ${repairNote}` : null,
  restored ? '- The repair did not pass its measurement or re-check and was undone.' : null,
  missing.length ? `\n**No result - do these yourself:** ${missing.map(st => `${st.n}. ${st.title}`).join('; ')}` : null,
  failedChanges.length ? `**Changes not in place:** ${failedChanges.map(n => `subtask ${n}`).join(', ')} - see the apply output above.` : null,
  hard ? '**The graph ran out of time:** everything decided is in place; the rest is listed above.' : null,
  '', '## Next step',
  passed ? 'Open each deliverable to confirm it is there, then report.' : 'Open each deliverable, check it against the request with your own tools, fix what is wrong, then report.',
  `**Never rewrite a deliverable whole with \`Write\`** - fix only what a checker named, with small \`Edit\`s. ${thin.length ? `Parts with one candidate or no passing check: ${thin.map(st => st.n).join(', ')} - treat them as unverified.` : 'Each part above had two or more candidates and passed its check.'}`,
  settledLine,
  sectionMode ? 'Keep exactly one `## ` heading per section, plain `-` hyphens, and every citation.' : null,
  EXAMPLES && PRISTINE && FUNCTIONS.length ? `Check code against its docstring examples with \`python3 ${q(EXAMPLES)} ${q(PRISTINE)}/FILE ${q(CWD)}/FILE\`; never add a special case for an example's values.` : null,
  'When you edit, copy `old_string` from the file itself - without the line-number prefix the Read tool shows.',
].filter(l => l !== null)
return lines.join('\n')