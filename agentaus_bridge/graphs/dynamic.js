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
// args = { task, cwd, width, skills: { name: markdown } }

const MARK = '[agentaus-graph]'
const THINK = '[think-only]'
const TASK = String((args && args.task) || '').trim()
const CWD = String((args && args.cwd) || '').trim()
const SKILLS = (args && args.skills) || {}
const SCRATCH = `/tmp/agentaus-graph/${(CWD.replace(/^\/+/, '') || 'task').replace(/[^\w.-]+/g, '_')}-dyn`

const WIDTH = Math.max(1, Number((args && args.width) || 8))
const DEADLINE_MS = Math.max(60000, Number((args && args.deadline_ms) || 600000))
const BUDGET_MS = Math.max(1000, Number((args && args.budget_ms) || 3000000))
const MAX_SUBTASKS = 16
// The plan chooses solver counts; the runtime keeps the total within what the upstream can
// serve - about thirty calls a minute across every session.
const MAX_SOLVERS_TOTAL = 36

let late = false
let live = 0
const waiting = []
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

async function slot(fn, ph) {
  while (live >= WIDTH) {
    await new Promise(resolve => waiting.push({ resolve, ph }))
    if (late && LATE_SKIPS.has(ph)) return null
  }
  live += 1
  try { return await fn() } finally {
    live -= 1
    if (waiting.length) {
      let k = 0
      if (late) {
        const closing = waiting.findIndex(w => !LATE_SKIPS.has(w.ph))
        if (closing >= 0) k = closing
      }
      waiting.splice(k, 1)[0].resolve()
    }
  }
}

// An agent's prompt: the role's skill, then the job. Think-only agents reason over the
// Markdown they are handed and get no tools - one call, not a tool loop.
function prompt(skill, title, lines, think) {
  return [
    MARK, think ? THINK : null, `# ${title}`, '',
    think ? 'Everything you need is below. Reply with Markdown only.' : `**Project folder:** \`${CWD}\` - every input file is there.`,
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
async function ask(text, label, ph, accept) {
  for (const attempt of [1, 2]) {
    if (late && LATE_SKIPS.has(ph)) return null
    const body = attempt === 1 ? text
      : `${text}\n\n**Backup attempt:** the previous agent returned nothing usable or a plan instead of the result. Do the work now, then reply with the result.`
    let out = null
    try {
      let timer = null
      out = await Promise.race([
        slot(() => agent(body, { label: attempt === 1 ? label : `${label}:backup`, phase: ph }), ph),
        new Promise(resolve => { timer = setTimeout(() => resolve(null), DEADLINE_MS) }),
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

function section(md, heading) {
  const m = String(md || '').match(new RegExp(`^#{2,3}\\s*${heading}\\s*$([\\s\\S]*?)(?=^#{1,3}\\s|$(?![\\s\\S]))`, 'im'))
  return m ? m[1].trim() : ''
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
  const deliverables = [...section(text, 'Deliverables').matchAll(/`([^`]+)`/g)].map(m => m[1].replace(/^\.\//, ''))
  const subtasks = []
  const re = /^##\s*Subtask\s*(\d+)\s*:?\s*(.*)$/gim
  const heads = [...text.matchAll(re)]
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
const brief = await ask(prompt('scout', 'Survey the project', [
  `**Scratch folder for plain-text copies:** \`${SCRATCH}/inputs\` (make it first).`, '',
  '## The request', '```text', TASK, '```',
]), 'scout', 'Scout') || '# Brief\n\n(The scout returned nothing. Read the files yourself.)'

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

const METHOD = [
  'Use the most direct method.',
  'Use a DIFFERENT method from the obvious one: another library, another reading of the data, or first principles.',
  'Be a sceptic: look for the trap first (zero values, blanks, duplicates, edge cases), then solve.',
  'Work from the raw files directly, without any library you would normally reach for.',
  'Solve it, then try hard to break your own answer, and fix it if it breaks.',
]

function solvePrompt(st, k, deps, feedback) {
  const dir = `${SCRATCH}/s${st.n}/${k}`
  return prompt('solve', `Solve subtask ${st.n}: ${st.title} (solver ${k})`, [
    `**Kind:** ${st.kind}. **Difficulty:** ${st.difficulty}.`, '',
    '## The subtask', st.task, '',
    deps.length ? '## Results it depends on' : null, ...deps,
    '', '## The brief from the scout', brief, '',
    `## Your method\n${METHOD[(k - 1) % METHOD.length]}`,
    st.kind === 'code' ? `\n## Your copy\n- Copy: \`rm -rf ${q(dir)} && mkdir -p ${q(dir)} && cp -R ${q(CWD)} ${q(dir + '/repo')}\`\n- Save the change: \`cd ${q(dir)} && diff -ruN -x __pycache__ -x .git ${q(CWD)} repo | sed -e "s#^--- ${CWD}#--- a#" -e "s#^+++ repo#+++ b#" > fix.patch\`\n- Under \`## Change\`, give \`${dir}/fix.patch\`.` : null,
    feedback ? `\n## A checker found a problem with the first answer\n${feedback}\nSolve it again with this in mind.` : null,
  ])
}

async function merge(st, results, strategy) {
  if (results.length === 1) return results[0]
  return await ask(prompt('merge', `Merge results for subtask ${st.n}: ${st.title}`, [
    `**Strategy:** \`${strategy}\``, '', '## The subtask', st.task, '',
    ...results.map((r, k) => `## Result ${k + 1}\n${r}`),
  ], true), `merge-${st.n}`, 'Merge') || results[0]
}

const VERDICT = md => (String(md || '').match(/\*\*Verdict:\*\*\s*(PASS|FAIL)/i) || [])[1]

async function runSubtask(st, deps) {
  const results = (await parallel(Array.from({ length: st.solvers }, (_, k) => () =>
    ask(solvePrompt(st, k + 1, deps), `solve-${st.n}.${k + 1}`, 'Solve', shown)))).filter(Boolean)
  if (!results.length) return { st, result: '', why: 'no solver returned a result' }
  let result = await merge(st, results, st.merge)
  if (st.check && !late) {
    const check = await ask(prompt('check', `Check subtask ${st.n}: ${st.title}`, [
      '## What was asked', st.task, '', '## The result to check', result,
    ]), `check-${st.n}`, 'Check', md => !!VERDICT(md))
    if (check && /fail/i.test(VERDICT(check))) {
      // One more solver with the checker's finding, then the merger decides between them.
      const redo = await ask(solvePrompt(st, results.length + 1, deps, section(check, 'Problem') || check),
        `solve-${st.n}.redo`, 'Solve', shown)
      if (redo) result = await merge(st, [result, redo], 'best-evidence')
      return { st, result, check: 'FAIL then redone' }
    }
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
    st.deps.filter(d => done[d]).map(d => `### Subtask ${d}\n${done[d].result}`))))
  outs.forEach((o, k) => { done[ready[k].n] = o || { st: ready[k], result: '', why: 'failed' } })
  if (late) break
}

const RESULTS = plan.subtasks.map(st => `## Subtask ${st.n}: ${st.title}\n\n${(done[st.n] && done[st.n].result) || '(no result - not settled in time)'}`).join('\n\n')

// ---------------------------------------------------------------------------------
// 6. Assemble, then a final check by two checkers and one fix if they find a problem
// ---------------------------------------------------------------------------------

phase('Assemble')
const deliverables = plan.deliverables.length ? plan.deliverables : []
const patches = [...RESULTS.matchAll(/[^\s`'"]+\/fix\.patch/g)].map(m => m[0])
const assembled = await ask(prompt('assemble', 'Build the deliverables', [
  '## The request', '```text', TASK, '```', '',
  '## Deliverables', deliverables.length ? deliverables.map(d => `- \`${d}\``).join('\n') : '(as the request names them)', '',
  '## Assembly', plan.assembly || 'As the request describes.', '',
  patches.length ? `## Patches to apply, in order\n${[...new Set(patches)].map(p => `- \`cd ${q(CWD)} && patch -p1 --forward -F 3 < ${q(p)}\``).join('\n')}` : null, '',
  '## Merged results', RESULTS,
]), 'assemble', 'Assemble')

phase('Final check')
const finalPrompt = n => prompt('check', `Check the finished work (checker ${n})`, [
  '## What was asked', '```text', TASK, '```', '',
  `Check every deliverable in \`${CWD}\` against every part of the request.`,
])
const finals = (await parallel([1, 2].map(n => () => ask(finalPrompt(n), `final-check-${n}`, 'Final check', md => !!VERDICT(md))))).filter(Boolean)
const failing = finals.filter(md => /fail/i.test(VERDICT(md)))
let fix = null
let recheck = null
if (failing.length && !late) {
  fix = await ask(prompt('fix', 'Fix the problem the checkers found', [
    '## What was asked', '```text', TASK, '```', '',
    '## What the checkers found', ...failing.map(md => section(md, 'Problem') || md), '',
    '## Merged results', RESULTS,
  ]), 'fix', 'Final check')
  recheck = fix ? await ask(finalPrompt(3), 'final-check-3', 'Final check', md => !!VERDICT(md)) : null
}
for (const t of timers) clearTimeout(t)

const passed = finals.length > 0 && (!failing.length || (recheck && /pass/i.test(VERDICT(recheck))))
const lines = [
  `# Result: ${passed ? 'checked' : 'needs attention'}`, '',
  `Plan: ${plan.subtasks.length} subtask(s), ${total} solver(s).${late ? ' **The time budget ran out:** check the deliverables yourself.' : ''}`, '',
  '## Subtasks',
  ...plan.subtasks.map(st => `- ${st.n}. ${st.title} - ${st.difficulty}, ${st.solvers} solver(s), check: ${(done[st.n] && done[st.n].check) || 'n/a'}`),
  '', '## Assembly', assembled || '(the assembler returned nothing)',
  '', '## Final check', ...finals.map((md, k) => `- Checker ${k + 1}: ${VERDICT(md)} - ${section(md, 'Problem').slice(0, 300)}`),
  fix ? `- Fixer: ${section(fix, 'Fixed').slice(0, 300)}` : null,
  recheck ? `- Re-check: ${VERDICT(recheck)} - ${section(recheck, 'Problem').slice(0, 300)}` : null,
  '', '## Next step',
  passed ? 'Open each deliverable to confirm it is there, then report.' : 'Open each deliverable, check it against the request with your own tools, fix what is wrong, then report.',
].filter(l => l !== null)
return lines.join('\n')
