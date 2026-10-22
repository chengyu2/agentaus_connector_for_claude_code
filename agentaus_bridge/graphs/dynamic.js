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
const SCOUT_MS = 360000
const MAX_SUBTASKS = 24
// The plan chooses solver counts; the runtime keeps the total within what the upstream can
// serve - about thirty calls a minute across every session.
// Measured (d9): 40 solvers on 6 slots kept every slot busy for an hour, and most answers
// came too late to count. Four solvers per slot.
const MAX_SOLVERS_TOTAL = Math.min(40, WIDTH * 4)

let late = false
let live = 0
const waiting = []
// Once the budget is spent, no new solving, merging of values or checking starts. Code is
// still merged - by running the candidates - because without it the first solver wins blind.
const LATE_SKIPS = new Set(['Solve', 'Merge', 'Check'])
const timers = []
function later(ms, fn) {
  const t = setTimeout(fn, ms)
  timers.push(t)
  return t
}
function startBudget() {
  later(BUDGET_MS, () => {
    late = true
    for (const w of waiting.filter(w => LATE_SKIPS.has(w.ph))) w.resolve()
    waiting.splice(0, waiting.length, ...waiting.filter(w => !LATE_SKIPS.has(w.ph)))
  })
}

// ---------------------------------------------------------------------------------
// Runtime helpers
// ---------------------------------------------------------------------------------

function q(path) {
  return `'${String(path).replace(/'/g, "'\\''")}'`
}

async function slot(fn, ph, pri = 0) {
  while (live >= WIDTH) {
    await new Promise(resolve => waiting.push({ resolve, ph, pri }))
    if (late && LATE_SKIPS.has(ph)) return null
  }
  live += 1
  try { return await fn() } finally {
    live -= 1
    if (waiting.length) {
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
const REFUSAL = /^\W{0,3}(I[’']m sorry|I am sorry|sorry,|I can(?:no|[’'])t|I[’']m unable|I am unable)\b[\s\S]{0,600}?\b(too (large|complex|big)|single step|separate (actions|steps)|unable to (complete|do|help)|break (it|the task|this|the request) (in|down))/i
function usable(text, short) {
  const t = String(text || '').trim()
  return t.length >= (short ? 1 : 20) && !PLAN_SHAPED.test(t) && !REFUSAL.test(t)
}
// Plain keyboard characters in everything an agent hands on, so the text a checker or fixer
// quotes matches the file, which is normalised the same way.
const plain = s => String(s).replace(/[‐-‒−]/g, '-').replace(/[    ]/g, ' ').replace(/【([^】\n]{1,80})】/g, '[$1]')

// One agent with a backup: a dead, empty, plan-shaped or rejected reply is asked again,
// once. The deadline stops the waiting, not the agent, so its slot is held until it settles.
// opts.wait: a writer (assembler, fixer) is never raced against the deadline - a backup
// running beside a live first attempt wrote the same files at the same time.
// The deadline runs from when the agent gets its slot, not from when it was queued, and an
// attempt that runs past it stays in the race beside its backup: the first usable answer
// wins. Measured (d9): the deadline counted queue time, so with 40 solvers on 6 slots most
// "timed out" before they started, ran anyway, and 57 of 82 answers were thrown away - among
// them all three that fixed count_top.
function firstUsable(runs, ok) {
  return new Promise(resolve => {
    let left = runs.length
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
async function ask(text, label, ph, accept, opts = {}) {
  const ok = t => usable(t, opts.short) && (!accept || accept(t))
  const runs = []
  let answered = false
  const done = out => { if (out) answered = true; return out }
  for (const attempt of [1, 2]) {
    if (opts.once && attempt === 2) return done(runs.length ? await firstUsable(runs, ok) : null)
    if (late && LATE_SKIPS.has(ph)) return done(runs.length ? await firstUsable(runs, ok) : null)
    const body = attempt === 1 ? text
      : `${text}\n\n**Backup attempt:** the previous agent returned nothing usable or a plan instead of the result. Do the work now, then reply with the result.`
    let begin = () => {}
    const begun = new Promise(r => { begin = r })
    runs.push(slot(() => {
      if (answered) return null
      begin()
      return agent(body, { label: attempt === 1 ? label : `${label}:backup`, phase: ph })
    }, ph, (opts.pri || 0) + (attempt === 2 ? 10 : 0)))
    let timer = null
    const timeout = opts.wait ? new Promise(() => {}) : begun.then(() => new Promise(r => { timer = setTimeout(() => r(null), opts.deadline || DEADLINE_MS) }))
    const out = await Promise.race([firstUsable(runs, ok), timeout])
    if (timer !== null) clearTimeout(timer)
    if (out) return done(out)
  }
  return null
}

// The headings agents write back. A section runs to the next of these at its level or above,
// not to the next heading of any kind: an answer holding its own `### Fee` or `## Q3` lost
// everything after it.
const KEYS = 'Answer|Key values|Evidence|Change|Tests|Why|Problem|Problems|Written|Fixed|Checklist|Verdict|Deliverables|Assembly|Plan|Brief|Subtask\\s*\\d+.*'
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

const FILES_MISSING = /\b(files?|data|inputs?|xlsx|csv|docx|directory|project)\b[^.\n]{0,80}\b(missing|not present|absent|do(es)? not exist|not found)\b/i
// A result must show its work, and must not claim the inputs are missing - that was always
// written without looking.
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
    if (!md) continue
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
  const codeFiles = [...new Set(plan.subtasks.filter(st => st.kind === 'code').flatMap(st => `${st.title}\n${st.task}`.match(/[\w./-]+\.py\b/g) || [])
    .concat(TASK.match(/[\w./-]+\.py\b/g) || []).map(f => f.replace(CWD + '/', '').replace(/^\.\//, '')))]
  const inFixed = f => codeFiles.some(c => c === f.file || c.endsWith('/' + f.file) || f.file.endsWith('/' + c) || c.split('/').pop() === f.file.split('/').pop())
  const auditing = /\b(more|other|further|any|every|all|hidden|unreported)\b[^.\n]{0,40}\b(bugs?|problems?|issues?|errors?|defects?)\b|\baudit\b/i.test(TASK)
  if (auditing) {
    for (const f of FUNCTIONS.filter(f => !f.stub && inFixed(f) && !covered(f.fn) && !/^_*(main|cli)$/.test(f.fn))) {
      added.push({ n: next++, title: `Audit \`${f.fn}\` in \`${f.file}\``, difficulty: 'medium', solvers: 2, check: true, merge: 'pick-change',
        kind: 'code', deps: [], task: `Audit \`${f.fn}\` in \`${f.file}\` for bugs nobody reported, with the audit checklist in your skill. Fix what is wrong; change nothing that is right.` })
    }
    for (const c of CONSTANTS.filter(c => inFixed({ file: c.file }))) {
      added.push({ n: next++, title: `Audit the module-level tables in ${c.file}`, difficulty: 'medium', solvers: 2, check: true, merge: 'pick-change',
        kind: 'code', deps: [], task: `Audit every module-level table, mapping and threshold in \`${c.file}\` - ${c.names.map(n => `\`${n}\``).join(', ')} - against the README and any model or standard the request names. The project's own documents often print these groups (a chart or a table by group): find them in the plain-text copies (\`grep -n -i -A60 '<a group name>' <copy>\`), copy that listing with its \`file:line\` as the expected table, then print the code's groups and compare member by member. Memory of a model is not a source. Fix only entries that are wrong.` })
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
  const auditing = /\b(more|other|further|any|every|all|hidden|unreported)\b[^.\n]{0,40}\b(bugs?|problems?|issues?|errors?|defects?)\b|\baudit\b/i.test(TASK)
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

// One agent that runs one exact command. The marker lets the bridge run it without the model.
function runExactly(label, cmd, ph) {
  return ask(prompt('assemble', label, [
    'Run exactly this command - nothing else - then reply with what it printed:', '', '[exact-command]', '```bash', cmd, '```',
  ]), label, ph, null, { wait: true, short: true, pri: -1 })
}

// A function's candidates are picked by a script, not an agent: each solver's copy of the
// function goes into a fresh copy of the project, the docstring's examples run first, then
// every solver's tests, pooled. A solver whose reply never arrived still has its copy on disk.
// Measured (d9): every merge of a function was given up on or skipped, so the first solver
// won blind; and three solvers that fixed count_top in their copies were never heard from.
const PICK = `${SCRATCH}/pick.py`
const PICK_PY = String.raw`import ast,os,shutil,subprocess,sys,tempfile
root,rel,fn,ex=sys.argv[1:5]; rest=sys.argv[5:]; i=rest.index('--'); cands=rest[:i]; tests=[t for t in rest[i+1:] if os.path.exists(t)]
def fnsrc(path):
    code=open(path).read()
    for n in ast.parse(code).body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn:
            lines=code.splitlines(True); s=(n.decorator_list[0].lineno if n.decorator_list else n.lineno)-1
            return ''.join(lines[s:n.end_lineno])
def put(path,new):
    code=open(path).read(); lines=code.splitlines(True)
    ns=[n for n in ast.parse(code).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn]
    if ns:
        n=ns[0]; s=(n.decorator_list[0].lineno if n.decorator_list else n.lineno)-1; lines[s:n.end_lineno]=[new.rstrip(chr(10))+chr(10)]
    else:
        lines.append(chr(10)*2+new)
    open(path,'w').write(''.join(lines))
orig=fnsrc(os.path.join(root,rel)) or ''
rows=[]
for c in cands:
    k,d,replied=c.split('|')
    # The function file, unless it is untouched and the copy holds a change.
    path=None; new=None
    for p in (os.path.join(d,fn+'.py'),os.path.join(d,'repo',rel)):
        try:
            s=fnsrc(p) if os.path.exists(p) else None
        except Exception as e:
            print('candidate',k,'unreadable:',p,e); s=None
        if s and (new is None or (new.strip()==orig.strip() and s.strip()!=orig.strip())):
            path,new=p,s
    if not new:
        print('candidate',k,'has no',fn); continue
    w=tempfile.mkdtemp(); shutil.copytree(root,w,dirs_exist_ok=True); put(os.path.join(w,rel),new)
    bad=0; out=''
    if ex!='-':
        try:
            out=subprocess.run([sys.executable,ex,os.path.join(root,rel),os.path.join(w,rel),fn],capture_output=True,text=True,timeout=120).stdout
            bad=out.count('MISMATCH')
        except Exception:
            bad=99
    passed=0
    for t in tests:
        try:
            passed+=subprocess.run([sys.executable,t],cwd=w,capture_output=True,timeout=120).returncode==0
        except Exception:
            pass
    changed=new.strip()!=orig.strip()
    print(f'candidate {k}: {path} - docstring example mismatches {bad}, pooled tests passed {passed}/{len(tests)}, replied {replied}, changed {changed}')
    rows.append(((bad,-passed,-int(replied=='1')),k,path,out,changed))
rows.sort()
for l in (rows[0][3].splitlines() if rows else []):
    if 'MISMATCH' in l: print('EXAMPLE', l)
frozen=os.environ.get('PICK_OUT')
if rows and frozen:
    os.makedirs(os.path.dirname(frozen),exist_ok=True); open(frozen,'w').write(fnsrc(rows[0][2]))
print('CHOSEN', frozen if rows and frozen else rows[0][2] if rows else 'none', 'mismatches', rows[0][0][0] if rows else -1, 'from', rows[0][2] if rows else '-', 'changed', rows[0][4] if rows else False)
`
let pickReady = false
const solverDir = (st, k) => `${SCRATCH}/s${st.n}/${k}`
// The picker installs itself if it is not there yet: nothing waits on an install agent's word.
const installPick = `mkdir -p ${q(SCRATCH)} && { [ -s ${q(PICK)} ] || cat > ${q(PICK)} <<'AGENTAUS_PICK_END'\n${PICK_PY}AGENTAUS_PICK_END\n}`
async function pickFunction(st, f, ks, replies) {
  const cands = ks.map(k => `${k}|${solverDir(st, k)}|${replies[k] ? 1 : 0}`)
  const tests = ks.map(k => `${solverDir(st, k)}/tests.py`)
  // The chosen function is frozen at once: a solver still running cannot change it afterwards.
  const frozen = `${SCRATCH}/chosen/s${st.n}/${f.fn}.py`
  const out = await runExactly(`pick-${st.n}`, `${installPick}\nPICK_OUT=${q(frozen)} python3 ${q(PICK)} ${q(BASE)} ${q(f.file)} ${f.fn} ${EXAMPLES && PRISTINE ? q(EXAMPLES) : '-'} ${cands.map(q).join(' ')} -- ${tests.map(q).join(' ')}`, 'Pick')
  const m = (out || '').match(/CHOSEN (\S+) mismatches (-?\d+) from (\S+)(?: changed (\w+))?/)
  if (!m || m[1] === 'none') return null
  const k = Number((m[3].match(new RegExp(`/s${st.n}/(\\d+)/`)) || [])[1])
  const reply = replies[k] || `## Answer\nCandidate ${k}'s \`${f.fn}\` - chosen by the tests below.`
  return { result: `${reply}\n\n## Why\n${(out.match(/^candidate .*$/gm) || []).join('\n')}`, change: m[1], dir: solverDir(st, k), mismatches: Number(m[2]),
    changed: m[4] !== 'False', evidence: (out.match(/^EXAMPLE .*$/gm) || []).map(l => l.replace(/^EXAMPLE\s+/, '')).join('\n') }
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
    if (lost.length) {
      log(`merge-${st.n}: the merged answer dropped ${lost.join(', ')}, given by two or more solvers - kept the fullest solver answer`)
      return results.slice().sort((a, b) => agreed.filter(n => nums(b).has(n)).length - agreed.filter(n => nums(a).has(n)).length)[0]
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

// Where a code result's change lives: the solver copy it names, or the first that replied.
function dirOf(st, result, ks, replies) {
  const k = Number((String(result || '').match(new RegExp(`/s${st.n}/(\\d+)/`)) || [])[1])
  if (k) return solverDir(st, k)
  const first = ks.find(x => replies[x])
  return first ? solverDir(st, first) : null
}

// A code result is tested in a copy holding that change - never in the project, which still
// holds the old code. Measured (d9): a checker imported the project's module and failed a
// correct change it had never loaded.
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

async function runSubtask(st, deps) {
  const f = functionOf(st)
  const byScript = st.kind === 'code' && !!f && pickReady
  const ks = Array.from({ length: st.solvers }, (_, k) => k + 1)
  // A code solver needs no backup: its copy on disk is read whether or not it replies.
  const outs = await parallel(ks.map(k => () => ask(solvePrompt(st, k, deps), `solve-${st.n}.${k}`, 'Solve', shown, { pri: k - 1, once: st.kind === 'code' })))
  const replies = {}
  ks.forEach((k, i) => { if (outs[i]) replies[k] = outs[i] })
  const decide = async kset => {
    if (byScript) {
      const picked = await pickFunction(st, f, kset, replies)
      if (picked) return picked
    }
    const results = kset.map(k => replies[k]).filter(Boolean)
    if (!results.length) return null
    const result = await merge(st, results, st.merge)
    let dir = st.kind === 'code' ? dirOf(st, result, kset, replies) : null
    if (dir) {
      const frozen = `${SCRATCH}/chosen/s${st.n}`
      if (/frozen/.test(await runExactly(`freeze-${st.n}`, `rm -rf ${q(frozen)} && mkdir -p ${q(frozen)} && cp -R ${q(dir + '/repo')} ${q(frozen + '/repo')} && echo frozen`, 'Pick') || '')) dir = frozen
    }
    return { result, dir }
  }
  let d = await decide(ks)
  if (!d) return { st, result: '', why: 'no solver returned a result' }
  // Up to two rounds of readings while the best candidate breaks an example - and no checker
  // or redo then: they only restate a mismatch the script measured. Measured (d12 he10): two
  // checkers restated it, and the redo fitted the example by breaking the docstring's tie rule.
  let all = []
  for (const round of [1, 2]) {
    if (!(byScript && d.mismatches > 0)) break
    const rk = await readings(st, f, d.evidence, round)
    if (!rk.length) break
    all = all.concat(rk)
    const again = await pickFunction(st, f, [...ks, ...all], replies)
    d = { ...(again || d), readings: all }
    log(`subtask ${st.n}: readings round ${round}; the chosen candidate breaks ${d.mismatches} example(s)`)
  }
  if (byScript && d.mismatches > 0) return { st, ...d, check: `unsettled: the best candidate breaks ${d.mismatches} docstring example(s)` }
  // An audit that changed nothing needs no checker.
  if (byScript && d.changed === false && /^\W*Audit\b/i.test(st.title)) return { st, ...d, check: 'PASS (no change: the audit found nothing to fix)' }
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
      ready ? `## The change under check - already in \`${c}\`; the project still holds the OLD code\n\`\`\`diff\n${diff || '(no difference)'}\n\`\`\`\nRun everything from \`${c}\`. Judge only this change: a problem in code it does not touch belongs to another subtask - name it, it is not a FAIL here.` : testCopy(st, d), '',
      card('The result to check', d.result, { top: 2, evidence: true }),
    ]), `check-${st.n}${n > 1 ? `.${n}` : ''}`, 'Check', md => !!VERDICT(md), { pri: -1 })
    const tested = md => !ready || String(md).includes(c)
    let check = await checkOnce(1)
    if (check && /fail/i.test(VERDICT(check)) && !tested(check)) return { st, ...d, check: 'FAIL not tested in the copy - kept' }
    // A failure is acted on only when a second checker, who has not seen the first, finds it
    // too. Measured: one checker's mistaken FAIL brought in a redo that replaced three
    // solvers' correct median.
    const second = check && /fail/i.test(VERDICT(check)) ? await checkOnce(2) : null
    if (second && /fail/i.test(VERDICT(second))) {
      const k = st.solvers + 1
      const feedback = `What the checkers found:\n${section(check, 'Problem') || check}\n${section(second, 'Problem')}\n\nReply in your solve format - \`## Answer\`, \`## Key values\`, \`## Evidence\` - never a verdict.`
      const redo = await ask(solvePrompt(st, k, deps, feedback), `solve-${st.n}.redo`, 'Solve', shown, { pri: 5 })
      // The redo joins the original solvers rather than facing the merged result alone, so
      // one new answer cannot outvote three.
      if (redo) replies[k] = redo
      const again = redo || byScript ? await decide([...ks, k, ...(d.readings || [])]) : null
      if (again) d = again
      else if (!section(d.result, 'Answer')) d = { ...d, result: bestOf(Object.values(replies)) }
      return { st, ...d, check: redo ? 'FAIL (confirmed) then redone' : 'FAIL (confirmed); the redo returned nothing' }
    }
    if (check && /fail/i.test(VERDICT(check))) return { st, ...d, check: 'FAIL not confirmed by a second checker - kept' }
    return { st, ...d, check: check ? VERDICT(check) : 'not checked' }
  }
  return { st, ...d, check: st.check ? 'skipped: time budget' : 'not needed' }
}

phase('Solve')
startBudget()
pickReady = plan.subtasks.some(st => st.kind === 'code' && functionOf(st))
const done = {}
const pending = plan.subtasks.slice()
while (pending.length) {
  // Every subtask whose dependencies are done runs now; one whose dependencies can never be
  // met runs anyway rather than stalling the plan.
  let ready = pending.filter(st => st.deps.every(d => done[d] || !plan.subtasks.some(o => o.n === d)))
  if (!ready.length) ready = pending.slice()
  for (const st of ready) pending.splice(pending.indexOf(st), 1)
  const outs = await parallel(ready.map(st => () => runSubtask(st,
    st.deps.filter(d => done[d]).map(d => card(`Subtask ${d}: ${done[d].st.title}`, done[d].result)))))
  outs.forEach((o, k) => { done[ready[k].n] = o || { st: ready[k], result: '', why: 'failed' } })
  if (late) break
}

const resultOf = st => (done[st.n] && done[st.n].result) || ''
const RAW = plan.subtasks.map(resultOf).join('\n\n')
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
// 6. Assemble, then a final check by two checkers and one fix if they find a problem
// ---------------------------------------------------------------------------------

phase('Assemble')
// The project goes back to how it was when the work began, before anything is put in place:
// every edit made before assembly - by anyone - is undone, and only the chosen changes go in.
// Files created since are left; only changed files are put back.
const restored0 = PRISTINE
  ? await runExactly('restore', `rsync -ai --checksum --exclude .git --exclude __pycache__ --exclude node_modules --exclude .venv --exclude venv --exclude '*.pyc' ${q(PRISTINE)}/ ${q(CWD)}/ | grep -v '/$' | head -40; echo restored`, 'Assemble')
  : null
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
// Every code change goes in by exact command: a chosen function by name, any other change as
// a patch the runtime makes from the solver's copy. Measured (d9): solvers named patch files
// they never wrote or wrote by hand, malformed; an assembler then copied one solver's whole
// file over the project.
const status = (cmd, n) => cmd.includes("<<'PY'")
  ? cmd.replace("<<'PY'", `<<'PY' && echo "applied subtask ${n}" || echo "FAILED subtask ${n}"`)
  : `{ ${cmd}; } && echo "applied subtask ${n}" || echo "FAILED subtask ${n}"`
// A whole-copy change goes in region by region - each top-level statement it changed, never a
// function another subtask owns. Measured (d12 c1913): a table audit's patch also rewrote
// `short`; it half-applied, and the assembler stacked its `short` edit on a correct one.
const REGIONS = `${SCRATCH}/regions.py`
const REGIONS_PY = String.raw`import ast,os,sys
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
        out.setdefault(k,(n.lineno-1,n.end_lineno,''.join(t[n.lineno-1:n.end_lineno])))
    return out
done=[]; failed=[]
for dp,dn,fs in os.walk(cand):
    dn[:]=[x for x in dn if x not in ('.git','__pycache__')]
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
        todo=[k for k in C if k in B and C[k][2]!=B[k][2] and not (k.startswith('def:') and k[4:] in owned)]
        new=[k for k in C if k not in B and not (k.startswith('def:') and k[4:] in owned)]
        miss=[k for k in todo if k not in L]
        if miss:
            failed.append(rel+': '+', '.join(miss)); continue
        for k in sorted(todo,key=lambda k:-L[k][0]):
            s,e,_=L[k]; lines[s:e]=[C[k][2].rstrip(chr(10))+chr(10)]
        for k in [k for k in new if k.startswith('import:')]:
            last=max([v[1] for kk,v in L.items() if kk.startswith('import:')] or [0]); lines.insert(last,C[k][2].rstrip(chr(10))+chr(10))
        tail=[C[k][2] for k in new if not k.startswith('import:')]
        if tail:
            at=L['main'][0] if 'main' in L else len(lines)
            lines[at:at]=[chr(10).join(x.rstrip(chr(10)) for x in tail)+chr(10)*3]
        open(l,'w').write(''.join(lines)); done.append(rel+': '+', '.join(todo+new))
print('regions', '; '.join(done) or 'none')
if failed:
    print('could not place', '; '.join(failed)); sys.exit(1)
`
const owners = new Map()
for (const st of plan.subtasks) { const f = functionOf(st); if (f) owners.set(f.fn, st.n) }
const changeLines = plan.subtasks.filter(st => st.kind === 'code' && done[st.n] && done[st.n].result).map(st => {
  const f = functionOf(st)
  const d = done[st.n]
  const change = d.change || (f && (resultOf(st).match(new RegExp(`[^\\s\`'"]+\\/${f.fn}\\.py`)) || [])[0])
  // An unchanged function is never put back. Measured (d12 c1913): a subtask whose chosen copy
  // was the original's put the bug back over another subtask's fix.
  if (f && change) return d.changed === false ? `echo "no change in subtask ${st.n}"` : status(replaceCommand(`${CWD}/${f.file}`, f.fn, change), st.n)
  if (!d.dir) return null
  const others = [...owners].filter(([, n]) => n !== st.n).map(([fn]) => fn).join(',')
  return `python3 ${q(REGIONS)} ${q(BASE)} ${q(d.dir + '/repo')} ${q(CWD)} ${q(others)} && echo "applied subtask ${st.n}" || echo "FAILED subtask ${st.n}"`
}).filter(Boolean)
if (changeLines.length) changeLines.unshift(`mkdir -p ${q(SCRATCH)} && cat > ${q(REGIONS)} <<'AGENTAUS_REGIONS_END'\n${REGIONS_PY}AGENTAUS_REGIONS_END`)
const applied = changeLines.length ? (await runExactly('apply-changes', changeLines.join('\n') + `\nfind ${q(CWD)} \\( -name '*.orig' -o -name '*.rej' \\) -newer ${q(SCRATCH + '/pristine')} -delete 2>/dev/null; true`, 'Assemble') || '(the changes could not be applied)') : ''
// A change counts as applied only when its own line says so. Measured (d10): a reply that
// dropped every FAILED line was read as "0 failed", and no one made the changes by hand.
const changeNs = plan.subtasks.filter(st => st.kind === 'code' && done[st.n] && done[st.n].result).map(st => st.n)
const okNs = new Set([...applied.matchAll(/^(?:applied|no change in) subtask (\d+)\s*$/gm)].map(m => Number(m[1])))
const failedChanges = changeNs.filter(n => !okNs.has(n))
// Every changed module still imports cleanly, with a command line that is not its own.
// Measured (d13 c1913h): a feature's solver put `args = parser.parse_args()` at module level;
// importing the module then parsed the importer's arguments and exited, and nothing could load it.
const IMPORTS_PY = String.raw`import filecmp,importlib.util,io,os,sys,contextlib
base,live=sys.argv[1:3]
for dp,dn,fs in os.walk(live):
    dn[:]=[x for x in dn if x not in ('.git','__pycache__','node_modules','.venv','venv')]
    for f in fs:
        if not f.endswith('.py'): continue
        p=os.path.join(dp,f); rel=os.path.relpath(p,live); b=os.path.join(base,rel)
        if os.path.exists(b) and filecmp.cmp(b,p,shallow=False): continue
        sys.argv=['import-check','--agentaus-not-an-option']; out=io.StringIO(); here=os.getcwd()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                os.chdir(os.path.dirname(p)); s=importlib.util.spec_from_file_location('m_check',p); s.loader.exec_module(importlib.util.module_from_spec(s))
            print('IMPORT OK',rel)
        except BaseException as e:
            print('IMPORT FAILED',rel,type(e).__name__,(out.getvalue() or str(e))[-300:].replace(chr(10),' '))
        finally:
            os.chdir(here)
`
const importCheck = changeLines.length ? (await runExactly('import-check', `python3 - ${q(BASE)} ${q(CWD)} <<'AGENTAUS_IMPORTS_END'\n${IMPORTS_PY}AGENTAUS_IMPORTS_END`, 'Assemble') || '') : ''
const importFailed = importCheck.match(/^IMPORT FAILED .*$/gm) || []
if (importFailed.length) log(`assemble: ${importFailed.length} changed module(s) do not import cleanly`)
if (changeLines.length) log(`assemble: ${changeNs.length} code change(s) put in place, ${failedChanges.length} failed`)
const patches = []
const swaps = []
// Prose deliverables built from one section per subtask are written section by section:
// one agent per section, each running one exact command. A single assembler asked to write
// all nine answers claimed it had, and the file held two.
const SECTION_FILE = /\.(md|txt)$/i
const sectionMode = deliverables.length === 1 && SECTION_FILE.test(deliverables[0])
  && plan.subtasks.length > 1 && plan.subtasks.every(st => st.kind === 'text')
const sectionsWritten = []
const sectionPart = k => `${SCRATCH}/sections/${String(k + 1).padStart(2, '0')}.md`
async function writeSection(k, st) {
  const body = section((done[st.n] && done[st.n].result) || '', 'Answer') || (done[st.n] && done[st.n].result) || '(no result)'
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
const codeOnly = changeLines.length > 0 && !failedChanges.length && !importFailed.length && deliverables.every(d => /\.(py|js|ts|mjs|cjs|sh)$/.test(d))
  && plan.subtasks.every(st => st.kind === 'code')
const assembled = sectionMode ? `## Written\n- \`${deliverables[0]}\` - ${sectionsWritten.filter(Boolean).length} of ${plan.subtasks.length} sections`
  : codeOnly ? `## Written\n${applied}`
  : await ask(prompt('assemble', 'Build the deliverables', [
  '## The request', '```text', TASK, '```', '',
  '## Deliverables - write each at exactly this path, in the project folder',
  deliverables.length ? deliverables.map(d => `- \`${d}\``).join('\n') : `(as the request names them - in \`${CWD}\`, never in a scratch folder)`, '',
  '## Assembly', plan.assembly || 'As the request describes.', '',
  changeLines.length ? `## Code changes - already put in place by the runtime\n\`\`\`text\n${applied}\n\`\`\`\n${failedChanges.length ? `**Make each FAILED change by hand**, with small \`Edit\`s, from that subtask's Answer below: ${failedChanges.map(n => `subtask ${n}`).join(', ')}.` : 'Do not change the code again.'}` : null, '',
  importFailed.length ? `## A changed module does not import cleanly - fix this first\n\`\`\`text\n${importFailed.join('\n')}\n\`\`\`\nImporting a module must run nothing: move command-line code (argument parsing, printing, exiting) under \`if __name__ == "__main__":\`, keeping what it does. Then import it again to check.` : null, '',
  '## Checklist - every item must end up in a deliverable, exactly as written', CHECKLIST, '',
  '## Merged results', RESULTS,
]), 'assemble', 'Assemble', null, { wait: true })

// Text deliverables in plain characters: hyphens, spaces and brackets as typed on a keyboard.
// Measured: mergers wrote `GST‑registered` and `355‑30` with non-breaking hyphens, and cited
// sources in 【lenticular brackets】 - text that no search for the plain words finds.
const NORMALISE = f => `python3 - ${q(f)} <<'PY'\nimport re,sys\np=sys.argv[1]; t=open(p,encoding='utf-8').read(); n=len(t)\nt=re.sub('[\\u2010\\u2011\\u2012\\u2212]','-',t)\nt=re.sub('[\\u00a0\\u2007\\u2009\\u202f]',' ',t)\nt=re.sub('\\u3010([^\\u3011\\n]{1,80})\\u3011',r'[\\1]',t)\nopen(p,'w',encoding='utf-8').write(t); print('normalised',p,n,len(t))\nPY`
const normalise = async () => {
  for (const d of deliverables.filter(d => SECTION_FILE.test(d))) await runExactly('normalise', NORMALISE(d), 'Assemble')
}
await normalise()

phase('Final check')
// The final checkers check the deliverables against the merged results - present, complete,
// matching, in the right format - and do not recompute them. Every value was already
// settled by several solvers, a merger and its own check. Measured: two final checkers
// recomputed four correct values, both fell into the same trap, and the fixer overwrote them.
const finalPrompt = n => prompt('check', `Check the finished work (checker ${n})`, [
  '## What was asked', '```text', TASK, '```', '',
  EXAMPLES && PRISTINE && FUNCTIONS.length ? `**First, run the docstring examples on each changed Python file and paste the last line:** \`python3 ${q(EXAMPLES)} ${q(PRISTINE)}/FILE ${q(CWD)}/FILE\` - a \`MISMATCH\` line is a FAIL.\n` : null,
  '## Your job here',
  '- Each deliverable exists at its path and has every part the request asks for, in the format it asks for.',
  '- Each value or section in it **matches the merged result below**. Do **not** recompute values: they were settled by several solvers and checked. A deliverable that matches its merged result PASSES on that point.',
  '- Code still runs: **run** every command the request names, exactly as written, and the program with no arguments too.',
  '- Importing a changed module runs nothing: argument parsing, printing or exiting at module level, outside `if __name__ == "__main__":`, is a FAIL.',
  '- Output the request asks for as JSON or CSV: pipe the exact command into the real parser (`| python3 -c "import json,sys; json.load(sys.stdin)"`). Reading it by eye is not a check; a parse error is a FAIL.',
  '- Each Python file changed: `python3 -c "import ast,sys,collections as c; print([k for k,v in c.Counter(n.name for n in ast.parse(open(sys.argv[1]).read()).body if hasattr(n,\'name\')).items() if v>1])" FILE` must print `[]` - a name defined twice means code was pasted twice: FAIL.',
  EXAMPLES && PRISTINE && FUNCTIONS.length ? `- Each Python file with docstring examples: \`python3 ${q(EXAMPLES)} ${q(PRISTINE)}/FILE ${q(CWD)}/FILE\` - a \`MISMATCH\` line is a FAIL; quote it.` : null, '',
  '## Checklist - copy it under `## Checklist` in your reply, ticking `[x]` each item the deliverable holds and leaving `[ ]` with what you found for each it does not',
  CHECKLIST, '',
  '## Merged results', RESULTS, '',
  `Check every deliverable in \`${CWD}\` against every part of the request.`,
  deliverables.length ? `The deliverables: ${deliverables.map(d => `\`${d}\``).join(', ')}. A deliverable that is not at its path is a FAIL.` : null,
])
const finals = (await parallel([1, 2].map(n => () => ask(finalPrompt(n), `final-check-${n}`, 'Final check', md => !!VERDICT(md))))).filter(Boolean)
const failing = finals.filter(md => /fail/i.test(VERDICT(md)))
let fix = null
let recheck = null
let restored = false
// The fixer runs only when both checkers find a problem, starts from a backup, and is
// undone if a fresh checker still finds one. Measured: a fixer acting on one checker's word
// recomputed four correct values the wrong way and overwrote them.
let repaired = []
// A sectioned document is repaired section by section: the failing subtasks are solved again
// and their sections rewritten. Measured (d9): a whole-file fixer spent 17 minutes editing
// text that did not match the file, and was undone.
if (sectionMode && finals.length >= 2 && failing.length === finals.length && !late) {
  const named = new Set()
  for (const md of failing) {
    for (const m of section(md, 'Checklist').matchAll(/^\s*[-*]\s*\[ \]\s*Subtask\s+(\d+)/gim)) named.add(Number(m[1]))
    for (const m of section(md, 'Problem').matchAll(/\bQ(\d{1,2})\b/g)) {
      const st = plan.subtasks.find(o => new RegExp(`^\\W*Q${m[1]}\\b`, 'i').test(o.title))
      if (st) named.add(st.n)
    }
  }
  const backup = `${SCRATCH}/before-fix`
  const targets = plan.subtasks.filter(st => named.has(st.n)).slice(0, 4)
  if (targets.length && await runExactly('backup', `rm -rf ${q(backup)} && mkdir -p ${q(backup)} && cp -R ${q(CWD)}/. ${q(backup)}/ && echo saved`, 'Final check')) {
    const problems = failing.map(md => section(md, 'Problem')).join('\n')
    for (const st of targets) {
      const again = await runSubtask({ ...st, task: `${st.task}\n\n**The final checkers found a problem with this part:**\n${problems}` }, [])
      if (again && again.result && section(again.result, 'Answer')) { done[st.n] = again; repaired.push(st.title) }
    }
    if (repaired.length) {
      for (const [k, st] of plan.subtasks.entries()) if (repaired.includes(st.title)) await writeSection(k, st)
      await joinSections(deliverables[0])
      await normalise()
      recheck = await ask(finalPrompt(3), 'final-check-3', 'Final check', md => !!VERDICT(md))
      if (recheck && /fail/i.test(VERDICT(recheck))) {
        restored = !!(await runExactly('undo', `cp -R ${q(backup)}/. ${q(CWD)}/ && echo undone`, 'Final check'))
      }
    }
  }
}
if (!sectionMode && finals.length >= 2 && failing.length === finals.length && !late) {
  const backup = `${SCRATCH}/before-fix`
  const saved = await runExactly('backup', `rm -rf ${q(backup)} && mkdir -p ${q(backup)} && cp -R ${q(CWD)}/. ${q(backup)}/ && echo saved`, 'Final check')
  if (saved) {
    fix = await ask(prompt('fix', 'Fix the problem the checkers found', [
      '## What was asked', '```text', TASK, '```', '',
      '## What both checkers found', ...failing.map(md => section(md, 'Problem') || md), '',
      '## Items the checkers found missing', ...[...new Set(failing.flatMap(md => section(md, 'Checklist').split('\n').filter(l => /^\s*[-*]\s*\[ \]/.test(l))))], '',
      '## Merged results - locked', 'Each was reached by several solvers and a merger. A deliverable value that matches one of these is correct: **do not change it**, whatever a recomputation says. Fix only what is missing, misplaced or malformed.', '',
      RESULTS,
    ]), 'fix', 'Final check', null, { wait: true })
    if (fix) await normalise()
    recheck = fix ? await ask(finalPrompt(3), 'final-check-3', 'Final check', md => !!VERDICT(md)) : null
    if (fix && recheck && /fail/i.test(VERDICT(recheck))) {
      restored = !!(await runExactly('undo', `cp -R ${q(backup)}/. ${q(CWD)}/ && echo undone`, 'Final check'))
    }
  }
}
for (const t of timers) clearTimeout(t)

// Settled: a function whose subtask passed, that only it changed, and whose live source is still
// the frozen choice - checked in the file, not taken from a verdict. Measured (d12 c1913): the
// list named `short`, which the assembler had changed after its check.
let settledLine = null
{
  const cands = plan.subtasks.filter(st => st.kind === 'code' && functionOf(st) && done[st.n] && done[st.n].change
    && /^PASS/i.test(String(done[st.n].check || '')) && owners.get(functionOf(st).fn) === st.n)
  if (cands.length) {
    const cmp = cands.map(st => { const f = functionOf(st)
      return `python3 - ${q(`${CWD}/${f.file}`)} ${f.fn} ${q(done[st.n].change)} <<'PY'\nimport ast,sys\nt,fn,c=sys.argv[1:4]\ndef src(p):\n    s=open(p).read(); l=s.splitlines(True)\n    for n in ast.parse(s).body:\n        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn: return ''.join(l[n.body[1 if ast.get_docstring(n) is not None and len(n.body)>1 else 0].lineno-1:n.end_lineno]).strip()\ntry:\n    print('same' if src(t)==src(c) else 'differs', fn)\nexcept Exception as e:\n    print('differs', fn)\nPY` }).join('\n')
    const out = await runExactly('settled', cmp, 'Final check') || ''
    const same = [...out.matchAll(/^same (\w+)/gm)].map(m => `\`${m[1]}\``)
    if (same.length) settledLine = `**Settled - do not edit:** ${same.join(', ')}. Each passed its checks and is still the chosen version; a test you write that disagrees with one is more likely wrong than the function. Never edit a docstring.`
  }
}

const passed = finals.length > 0 && (failing.length < finals.length || !!(recheck && /pass/i.test(VERDICT(recheck))))
const lines = [
  `# Result: ${passed ? 'checked' : 'needs attention'}`, '',
  `Plan: ${plan.subtasks.length} subtask(s), ${total} solver(s).${late ? ' **The time budget ran out:** check the deliverables yourself.' : ''}`, '',
  '## Subtasks',
  ...plan.subtasks.map(st => `- ${st.n}. ${st.title} - ${st.difficulty}, ${st.solvers} solver(s), check: ${(done[st.n] && done[st.n].check) || 'n/a'}`),
  '', '## Assembly', assembled || '(the assembler returned nothing)',
  '', '## Final check', ...finals.map((md, k) => `- Checker ${k + 1}: ${VERDICT(md)} - ${section(md, 'Problem').slice(0, 1500)}`),
  repaired.length ? `- Sections solved again and rewritten: ${repaired.join(', ')}` : null,
  fix ? `- Fixer: ${section(fix, 'Fixed').slice(0, 300)}` : null,
  recheck ? `- Re-check: ${VERDICT(recheck)} - ${section(recheck, 'Problem').slice(0, 300)}` : null,
  restored ? '- The fix did not pass the re-check and was undone.' : null,
  '', '## Next step',
  passed ? 'Open each deliverable to confirm it is there, then report.' : 'Open each deliverable, check it against the request with your own tools, fix what is wrong, then report.',
  '**Never rewrite a deliverable whole with `Write`** - fix only what a checker named, with small `Edit`s. Each part above was solved and checked by several agents.',
  settledLine,
  sectionMode ? 'Keep exactly one `## ` heading per section, plain `-` hyphens, and every citation.' : null,
  EXAMPLES && PRISTINE && FUNCTIONS.length ? `Check code against its docstring examples with \`python3 ${q(EXAMPLES)} ${q(PRISTINE)}/FILE ${q(CWD)}/FILE\`; never add a special case for an example's values.` : null,
  'When you edit, copy `old_string` from the file itself - without the line-number prefix the Read tool shows.',
].filter(l => l !== null)
return lines.join('\n')
