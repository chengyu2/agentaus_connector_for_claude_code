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
const CONSTANTS = (args && args.constants) || []
const EXAMPLES = String((args && args.examples) || '').trim()

const WIDTH = Math.max(1, Number((args && args.width) || 8))
const DEADLINE_MS = Math.max(60000, Number((args && args.deadline_ms) || 600000))
const BUDGET_MS = Math.max(1000, Number((args && args.budget_ms) || 4200000))
const SCOUT_MS = 360000
const MAX_SUBTASKS = 24
// The plan chooses solver counts; the runtime keeps the total within what the upstream can
// serve - about thirty calls a minute across every session.
const MAX_SOLVERS_TOTAL = 40

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
later(BUDGET_MS, () => {
  late = true
  for (const w of waiting.filter(w => LATE_SKIPS.has(w.ph))) w.resolve()
  waiting.splice(0, waiting.length, ...waiting.filter(w => !LATE_SKIPS.has(w.ph)))
})

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
function usable(text) {
  const t = String(text || '').trim()
  return t.length >= 20 && !PLAN_SHAPED.test(t)
}

// One agent with a backup: a dead, empty, plan-shaped or rejected reply is asked again,
// once. The deadline stops the waiting, not the agent, so its slot is held until it settles.
// opts.wait: a writer (assembler, fixer) is never raced against the deadline - a backup
// running beside a live first attempt wrote the same files at the same time.
async function ask(text, label, ph, accept, opts = {}) {
  for (const attempt of [1, 2]) {
    if (late && LATE_SKIPS.has(ph)) return null
    const body = attempt === 1 ? text
      : `${text}\n\n**Backup attempt:** the previous agent returned nothing usable or a plan instead of the result. Do the work now, then reply with the result.`
    let out = null
    try {
      let timer = null
      const run = slot(() => agent(body, { label: attempt === 1 ? label : `${label}:backup`, phase: ph }), ph, opts.pri || 0)
      out = opts.wait ? await run : await Promise.race([
        run, new Promise(resolve => { timer = setTimeout(() => resolve(null), opts.deadline || DEADLINE_MS) }),
      ])
      if (timer !== null) clearTimeout(timer)
    } catch (e) {
      out = null
    }
    const text2 = typeof out === 'string' ? out : out ? JSON.stringify(out) : ''
    if (usable(text2) && (!accept || accept(text2))) return text2
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
const STEP = /^(load|read|parse|normali[sz]e|prepare|gather (data|inputs|sources)|collect|derive|build|assemble|write|compile|produce|create)\b|cross[- ]?check|verify all|summary of findings|^(bug[- ‐-―]?fix(es)?\s+)?summary\b|^verif(y|ication)\b/i
{
  const dropped = new Set()
  const kept = plan.subtasks.filter(st => {
    const step = STEP.test(st.title) && !OUTPUT_MARK.test(st.title.replace(/^(write|produce|create)\s+`[^`]+`$/i, ''))
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
  return `python3 - ${q(target)} ${fn} ${q(src)} <<'PY'\nimport ast,sys\nt,fn,src=sys.argv[1:4]; code=open(t).read(); lines=code.splitlines(keepends=True); new=open(src).read().rstrip(chr(10))+chr(10)\nfound=[n for n in ast.parse(code).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn]\nif found:\n    n=found[0]; s=(n.decorator_list[0].lineno if n.decorator_list else n.lineno)-1\n    try:\n        c=[x for x in ast.parse(new).body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name==fn][0]; d=ast.get_docstring(c) is not None; body=c.body[1:] if d else c.body\n        if ast.get_docstring(n) is not None and body and body[0].lineno>(c.body[0].end_lineno if d else c.lineno):\n            new=''.join(lines[s:n.body[0].end_lineno])+''.join(new.splitlines(keepends=True)[body[0].lineno-1:c.end_lineno])\n    except Exception:\n        pass\n    lines[s:n.end_lineno]=[new]\nelse:\n    lines.append(chr(10)+chr(10)+new)\nopen(t,'w').write(''.join(lines)); print('replaced' if found else 'appended', fn)\nPY`
}
function functionOf(st) {
  if (st.kind !== 'code') return null
  const text = `${st.title}\n${st.task}`
  // A constant (`SPELLING`) is not a function, and a name the code does not define is not one.
  const names = [...new Set((st.title.match(/`([A-Za-z_]\w*)`/g) || []).map(n => n.replace(/`/g, '')))]
    .filter(n => !/^[A-Z][A-Z0-9_]*$/.test(n))
  const known = FUNCTIONS.length ? names.filter(n => FUNCTIONS.some(f => f.fn === n)) : names
  const fn = known.length === 1 ? known[0] : null
  if (!fn) return null
  const inv = FUNCTIONS.filter(f => f.fn === fn)
  if (inv.length === 1) return { fn, file: inv[0].file }
  const pys = plan.deliverables.filter(d => /\.py$/.test(d)).map(d => d.replace(CWD + '/', ''))
  const named = (text.match(/[\w./-]+\.py\b/g) || []).map(f => f.replace(CWD + '/', '').replace(/^\.\//, ''))
  const file = named.find(f => pys.includes(f)) || (pys.length === 1 ? pys[0] : named.find(f => !/test/i.test(f)))
  return file ? { fn, file } : null
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
        kind: 'code', deps: [], task: `Audit every module-level table, mapping and threshold in \`${c.file}\` - ${c.names.map(n => `\`${n}\``).join(', ')} - against the README and any model or standard the request names. Print each group's members and sizes and compare. Fix only entries that are wrong.` })
    }
  }
  if (added.length) {
    plan.subtasks = plan.subtasks.concat(added).slice(0, MAX_SUBTASKS)
    log(`plan: added ${added.length} subtask(s) so every function is covered`)
  }
}

// A text subtask answering three or more questions is one subtask per question. Measured: one
// subtask for nine questions gave each solver nine answers to write, and facts in the README
// never reached Q3; with one subtask per question, they did.
{
  const qs = st => {
    const text = `${st.title}\n${st.task}`
    const range = text.match(/\bQ(\d{1,2})\s*(?:-|‐|‑|‒|–|—|to|through)\s*Q?(\d{1,2})\b/i)
    const set = new Set((text.match(/\bQ(\d{1,2})\b/gi) || []).map(x => Number(x.slice(1))))
    if (range && Number(range[2]) > Number(range[1]) && Number(range[2]) - Number(range[1]) < 15) {
      for (let k = Number(range[1]); k <= Number(range[2]); k++) set.add(k)
    }
    return [...set].sort((a, b) => a - b)
  }
  let next = Math.max(0, ...plan.subtasks.map(o => o.n)) + 1
  const out = []
  for (const st of plan.subtasks) {
    const list = st.kind === 'text' ? qs(st) : []
    if (list.length < 3) { out.push(st); continue }
    const parts = list.map(k => ({ n: next++, title: `Q${k}`, difficulty: st.difficulty === 'easy' ? 'medium' : st.difficulty,
      solvers: Math.max(2, Math.min(3, st.solvers)), check: true, merge: 'combine', kind: 'text', deps: [],
      task: `Answer **Q${k}** of the request. Quote the question **word for word** from the file that asks it, then answer exactly that question - every part of it.\n\nThe planner's notes for the group it was in (context only; where they disagree with the question or the documents, the question and the documents win):\n\n${nest(st.task, 4)}` }))
    for (const o of plan.subtasks) o.deps = o.deps.flatMap(d => d === st.n ? parts.map(p => p.n) : [d])
    out.push(...parts)
    log(`plan: split subtask ${st.n} into one per question (${list.map(k => `Q${k}`).join(', ')})`)
  }
  plan.subtasks = out.slice(0, MAX_SUBTASKS)
}

// Keep the total, now the plan is final.
total = plan.subtasks.reduce((n, s) => n + s.solvers, 0)
while (total > MAX_SOLVERS_TOTAL) {
  const most = plan.subtasks.slice().sort((a, b) => b.solvers - a.solvers)[0]
  if (most.solvers <= 1) break
  most.solvers -= 1
  total -= 1
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
  const copy = `- Copy: \`rm -rf ${q(dir)} && mkdir -p ${q(dir)} && cp -R ${q(BASE)} ${q(dir + '/repo')} && chmod -R u+w ${q(dir)}\` - work **only** in \`${dir}/repo\`.`
  const examples = f && EXAMPLES && PRISTINE
    ? `- **The docstring's examples are the spec.** Run them before and after your change:\n\`\`\`bash\npython3 ${q(EXAMPLES)} ${q(`${PRISTINE}/${f.file}`)} ${q(`${dir}/repo/${f.file}`)} ${f.fn}\n\`\`\`\n  Every line must read \`ok\`. Paste its last line under \`## Evidence\`.`
    : null
  const tests = `- Save your tests as \`${dir}/tests.py\`: a plain script that imports what it tests from the project's own module and \`assert\`s, run as \`cd ${q(dir + '/repo')} && python3 ${q(dir + '/tests.py')}\`.`
  if (f) {
    return [`\n## Your copy - this subtask is the function \`${f.fn}\` in \`${f.file}\``, copy, tests, examples,
      `- Change only the body of \`${f.fn}\` - never its name, arguments, defaults or docstring. Put any \`import\` it needs inside the function.`,
      `- Save the function, exactly:\n\`\`\`bash\n${extractCommand(`${dir}/repo/${f.file}`, f.fn, `${dir}/${f.fn}.py`)}\n\`\`\``,
      `- Under \`## Change\`, give \`${dir}/${f.fn}.py\`; under \`## Tests\`, \`${dir}/tests.py\`.`].join('\n')
  }
  return ['\n## Your copy', copy, tests,
    `- Save the change: \`cd ${q(dir)} && diff -ruN -x __pycache__ -x .git ${q(BASE)} repo | sed -e "s#^--- ${BASE}#--- a#" -e "s#^+++ repo#+++ b#" > fix.patch\``,
    `- Under \`## Change\`, give \`${dir}/fix.patch\`; under \`## Tests\`, \`${dir}/tests.py\`.`].join('\n')
}

function solvePrompt(st, k, deps, feedback) {
  const dir = `${SCRATCH}/s${st.n}/${k}`
  return prompt('solve', `Solve subtask ${st.n}: ${st.title} (solver ${k})`, [
    `**Kind:** ${st.kind}. **Difficulty:** ${st.difficulty}.`, '',
    '## The request - the whole job, of which your subtask is one part', '```text', TASK, '```', '',
    '## Your subtask', `**${st.title}**`, '', nest(st.task, 3), '',
    specOf(st), '',
    deps.length ? '## Results it depends on' : null, ...deps,
    '', '## The brief from the scout', brief, '',
    `## Your method\n${METHOD[(k - 1) % METHOD.length]}`,
    st.kind === 'code' ? codeSteps(st, dir) : null,
    feedback ? `\n## A checker found a problem with the first answer\n${feedback}\nSolve it again with this in mind.` : null,
  ])
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
      (f ? `its function, with:\n\`\`\`bash\n${replaceCommand(`${SCRATCH}/m${st.n}/cN/repo/${f.file}`, f.fn, 'CANDIDATE_FUNCTION_FILE')}\n\`\`\`` : '`cd` into the copy and `patch -p1 < CANDIDATE_PATCH`.'),
    f && EXAMPLES && PRISTINE ? `3. **Docstring examples first:** \`python3 ${q(EXAMPLES)} ${q(`${PRISTINE}/${f.file}`)} ${q(`${SCRATCH}/m${st.n}/cN/repo/${f.file}`)} ${f.fn}\`. A candidate that fails any example loses to one that passes them all, whatever the pooled tests say.` : null,
    `${f && EXAMPLES && PRISTINE ? 4 : 3}. Run every pooled tests file from \`${SCRATCH}/m${st.n}/cN/repo\`, count the passes, and keep the candidate that passes most.`,
  ].join('\n') : null
  const merged = await ask(prompt('merge', `Merge results for subtask ${st.n}: ${st.title}`, [
    `**Strategy:** \`${code ? 'pick-change' : strategy}\``, '', '## The subtask', `**${st.title}**`, '', nest(st.task, 3), '',
    specOf(st), '', runIt, '',
    ...results.map((r, k) => card(`Result ${k + 1}`, r, { top: 2, evidence: true })),
  ].filter(l => l !== null), !code), `merge-${st.n}`, code ? 'Pick' : 'Merge')
  if (!merged) return results[0]
  // A merger that names no saved change keeps the first solver's, so the assembler still has
  // one to put in place.
  const paths = results.flatMap(r => section(r, 'Change').match(/[^\s`'"]+\.(py|patch)\b/g) || [])
  if (paths.length && !paths.some(p => merged.includes(p))) {
    return `${merged}\n\n## Change\n${section(results.find(r => /\.(py|patch)\b/.test(section(r, 'Change'))), 'Change')}`
  }
  return merged
}

const VERDICT = md => (String(md || '').match(/\*\*Verdict:\*\*\s*(PASS|FAIL)/i) || [])[1]

async function runSubtask(st, deps) {
  const results = (await parallel(Array.from({ length: st.solvers }, (_, k) => () =>
    ask(solvePrompt(st, k + 1, deps), `solve-${st.n}.${k + 1}`, 'Solve', shown, { pri: k })))).filter(Boolean)
  if (!results.length) return { st, result: '', why: 'no solver returned a result' }
  let result = await merge(st, results, st.merge)
  if (st.check && !late) {
    const checkOnce = n => ask(prompt('check', `Check subtask ${st.n}: ${st.title} (checker ${n})`, [
      '## The request', '```text', TASK, '```', '',
      '## What was asked of this part', `**${st.title}**`, '', nest(st.task, 3), '', specOf(st), '',
      card('The result to check', result, { top: 2, evidence: true }),
    ]), `check-${st.n}${n > 1 ? `.${n}` : ''}`, 'Check', md => !!VERDICT(md))
    const check = await checkOnce(1)
    // A failure is acted on only when a second checker, who has not seen the first, finds it
    // too. Measured: one checker's mistaken FAIL brought in a redo that replaced three
    // solvers' correct median.
    const second = check && /fail/i.test(VERDICT(check)) ? await checkOnce(2) : null
    if (second && /fail/i.test(VERDICT(second))) {
      const redo = await ask(solvePrompt(st, results.length + 1, deps,
        `${section(check, 'Problem') || check}\n${section(second, 'Problem')}`), `solve-${st.n}.redo`, 'Solve', shown, { pri: 5 })
      // The redo joins the original solvers rather than facing the merged result alone, so
      // one new answer cannot outvote three.
      if (redo) result = await merge(st, results.concat([redo]), st.merge)
      return { st, result, check: 'FAIL (confirmed) then redone' }
    }
    if (check && /fail/i.test(VERDICT(check))) return { st, result, check: 'FAIL not confirmed by a second checker - kept' }
    return { st, result, check: check ? VERDICT(check) : 'not checked' }
  }
  return { st, result, check: st.check ? 'skipped: time budget' : 'not needed' }
}

phase('Solve')
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
const CHECKLIST = plan.subtasks.flatMap(st => {
  const keys = keyValues(resultOf(st))
  return keys.length ? keys.map(k => k.replace(/^- /, `- [ ] Subtask ${st.n} - `)) : [`- [ ] Subtask ${st.n} - **${st.title}**: its answer is in the deliverable`]
}).join('\n')

// ---------------------------------------------------------------------------------
// 6. Assemble, then a final check by two checkers and one fix if they find a problem
// ---------------------------------------------------------------------------------

phase('Assemble')
// One agent that runs one exact command.
const runExactly = (label, cmd, ph) => ask(prompt('assemble', label, [
  'Run exactly this command - nothing else - then reply with what it printed:', '', '```bash', cmd, '```',
]), label, ph, null, { wait: true })
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
const patches = [...RAW.matchAll(/[^\s`'"]+\/fix\.patch/g)].map(m => m[0])
const swaps = plan.subtasks.map(st => ({ st, f: functionOf(st), r: (done[st.n] && done[st.n].result) || '' }))
  .filter(x => x.f).map(x => {
    const path = (x.r.match(new RegExp(`[^\\s\`'"]+\\/${x.f.fn}\\.py`)) || [])[0]
    return path ? replaceCommand(`${CWD}/${x.f.file}`, x.f.fn, path) : null
  }).filter(Boolean)
// Prose deliverables built from one section per subtask are written section by section:
// one agent per section, each running one exact command. A single assembler asked to write
// all nine answers claimed it had, and the file held two.
const SECTION_FILE = /\.(md|txt)$/i
const sectionMode = deliverables.length === 1 && SECTION_FILE.test(deliverables[0])
  && plan.subtasks.length > 1 && plan.subtasks.every(st => st.kind === 'text')
const sectionsWritten = []
if (sectionMode) {
  const file = deliverables[0]
  for (const [k, st] of plan.subtasks.entries()) {
    const body = section((done[st.n] && done[st.n].result) || '', 'Answer') || (done[st.n] && done[st.n].result) || '(no result)'
    const text = /^#{1,3}\s/.test(body.trim()) ? body.trim() : `## ${st.title}\n\n${body.trim()}`
    // Each section is its own file, written with `>`: running the command twice cannot
    // write it twice. Measured: a section writer ran its `>>` append twice, and the
    // duplicate failed both final checkers.
    const part = `${SCRATCH}/sections/${String(k + 1).padStart(2, '0')}.md`
    const ok = await ask(prompt('assemble', `Write section ${k + 1} of ${plan.subtasks.length} of ${file.split('/').pop()}`, [
      'Run exactly this command - nothing else - then report the line count it prints:', '',
      '```bash', `mkdir -p ${q(`${SCRATCH}/sections`)} && cat > ${q(part)} <<'AGENTAUS_SECTION_END'\n${text}\n\nAGENTAUS_SECTION_END\nwc -l ${q(part)}`, '```',
    ]), `section-${k + 1}`, 'Assemble', null, { wait: true })
    sectionsWritten.push(!!ok)
  }
  await runExactly('join-sections', `cat ${q(`${SCRATCH}/sections`)}/*.md > ${q(file)} && grep -c '^## ' ${q(file)} && wc -l ${q(file)}`, 'Assemble')
}
const assembled = sectionMode ? `## Written\n- \`${deliverables[0]}\` - ${sectionsWritten.filter(Boolean).length} of ${plan.subtasks.length} sections` : await ask(prompt('assemble', 'Build the deliverables', [
  '## The request', '```text', TASK, '```', '',
  '## Deliverables - write each at exactly this path, in the project folder',
  deliverables.length ? deliverables.map(d => `- \`${d}\``).join('\n') : `(as the request names them - in \`${CWD}\`, never in a scratch folder)`, '',
  '## Assembly', plan.assembly || 'As the request describes.', '',
  swaps.length ? `## Put the chosen functions in place - run each command, in order\n${swaps.map(c => `\`\`\`bash\n${c}\n\`\`\``).join('\n')}` : null, '',
  patches.length ? `## Patches to apply, in order\n${[...new Set(patches)].map(p => `- \`cd ${q(CWD)} && patch -p1 --forward -F 3 < ${q(p)}\``).join('\n')}` : null, '',
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
  '## Your job here',
  '- Each deliverable exists at its path and has every part the request asks for, in the format it asks for.',
  '- Each value or section in it **matches the merged result below**. Do **not** recompute values: they were settled by several solvers and checked. A deliverable that matches its merged result PASSES on that point.',
  '- Code still runs: **run** every command the request names, exactly as written, and the program with no arguments too.',
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
if (finals.length >= 2 && failing.length === finals.length && !late) {
  const backup = `${SCRATCH}/before-fix`
  const saved = await ask(prompt('assemble', 'Back up the project', [
    `Run exactly: \`rm -rf ${q(backup)} && mkdir -p ${q(backup)} && cp -R ${q(CWD)}/. ${q(backup)}/ && ls ${q(backup)} | head\``,
  ]), 'backup', 'Final check')
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

const passed = finals.length > 0 && (failing.length < finals.length || !!(recheck && /pass/i.test(VERDICT(recheck))))
const lines = [
  `# Result: ${passed ? 'checked' : 'needs attention'}`, '',
  `Plan: ${plan.subtasks.length} subtask(s), ${total} solver(s).${late ? ' **The time budget ran out:** check the deliverables yourself.' : ''}`, '',
  '## Subtasks',
  ...plan.subtasks.map(st => `- ${st.n}. ${st.title} - ${st.difficulty}, ${st.solvers} solver(s), check: ${(done[st.n] && done[st.n].check) || 'n/a'}`),
  '', '## Assembly', assembled || '(the assembler returned nothing)',
  '', '## Final check', ...finals.map((md, k) => `- Checker ${k + 1}: ${VERDICT(md)} - ${section(md, 'Problem').slice(0, 300)}`),
  fix ? `- Fixer: ${section(fix, 'Fixed').slice(0, 300)}` : null,
  recheck ? `- Re-check: ${VERDICT(recheck)} - ${section(recheck, 'Problem').slice(0, 300)}` : null,
  restored ? '- The fix did not pass the re-check and was undone.' : null,
  '', '## Next step',
  passed ? 'Open each deliverable to confirm it is there, then report.' : 'Open each deliverable, check it against the request with your own tools, fix what is wrong, then report.',
  'When you edit, copy `old_string` from the file itself - without the line-number prefix the Read tool shows.',
].filter(l => l !== null)
return lines.join('\n')
