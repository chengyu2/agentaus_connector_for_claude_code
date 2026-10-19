export const meta = {
  name: 'agentaus-solve',
  description: 'Survey, break down, solve each item three ways, reconcile, assemble, verify and fix',
  phases: [
    { title: 'Survey' }, { title: 'Read' }, { title: 'Requirements' }, { title: 'Expand' },
    { title: 'Plan' }, { title: 'Solve' }, { title: 'Reconcile' }, { title: 'Assemble' },
    { title: 'Verify' }, { title: 'Fix' },
  ],
}

// The agent graph the bridge runs for an Agentaus ultracode turn. Four principles, from
// what Agentaus measurably does well and badly: many agents; one small, simple prompt per
// agent; many steps; and redundancy - every step that can fail has a backup, and every
// answer is reached three independent ways before it is trusted.
//
// The bridge substitutes this script for whatever the model wrote, with
// args = { task, cwd }. Every prompt starts with MARK so the bridge knows the subagent's
// context is chosen here and does not append the whole request again.

const MARK = '[agentaus-graph]'
const TASK = String((args && args.task) || '').trim()
const CWD = String((args && args.cwd) || '').trim()
// Keyed on the whole working-directory path: two projects with the same folder name must
// not read each other's extracted documents.
const SCRATCH = `/tmp/agentaus-graph/${(CWD.replace(/^\/+/, '') || 'task').replace(/[^\w.-]+/g, '_')}`

// Agentaus starts answering 524s past about eight sessions at once, so every agent()
// call waits for one of WIDTH slots, however much the graph fans out.
const WIDTH = Math.max(2, Number((args && args.width) || 8))
// An agent still going after this long is a straggler: its backup starts instead. Measured,
// a handful took 13-27 minutes while their stage's other agents took seconds, and every
// barrier waited for them.
const DEADLINE_MS = Math.max(60000, Number((args && args.deadline_ms) || 600000))
// The whole graph's time. Past it, items still unsettled fall back to the draft and the
// optional checking is skipped, so a slow upstream costs quality, never the deliverable.
const BUDGET_MS = Math.max(1000, Number((args && args.budget_ms) || 3000000))
let late = false
let live = 0
const waiting = []
// When the budget runs out, every agent still queued for a slot is released: a solver or
// judge then skips itself (see ask), and the closing agents - the writer, the draft - run at
// once. The first version left them queued behind the solvers still running, and a graph
// sat 40 minutes past its budget with its writer waiting for a slot.
const budgetTimer = setTimeout(() => {
  late = true
  // Queued work that only improves on the draft is released to give up its place.
  for (const w of waiting.filter(w => SKIP_WHEN_LATE.has(w.ph))) w.resolve()
  waiting.splice(0, waiting.length, ...waiting.filter(w => !SKIP_WHEN_LATE.has(w.ph)))
}, BUDGET_MS)
let lateTimer = null
const lateSignal = new Promise(resolve => { lateTimer = setTimeout(() => resolve('late'), BUDGET_MS) })
const MAX_FILES = 14
const MAX_REQUIREMENTS = 12
const MAX_ITEMS = 24
const MAX_PER_REQUIREMENT = 12
const CODE_EXT = 'py|js|mjs|cjs|jsx|ts|tsx|r|sh|go|java|kt|rb|rs|c|h|cc|cpp|cs|swift|php|sql|html|css|ipynb'
const CODE_FILE = new RegExp(`\\.(${CODE_EXT})$`, 'i')
const SOLVERS = 3

const METHODS = [
  'Use the most direct method. Run code to get the answer; do not estimate.',
  'Use a DIFFERENT method from the obvious one (another library, another reading of the data, or working it out from first principles). Run code; do not estimate.',
  'Be a sceptic: look for the trap first (zero values, missing values, duplicates, a different spelling, an edge case the wording hides), then solve. Run code; do not estimate.',
]

// ---------------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------------

const STR = { type: 'string' }
const LIST = { type: 'array', items: STR }

// Every prompt ends the same way: a weak model that prints a 13,000-row table into its own
// context runs out of window, and its backup does the same.
const FOOTER = '\n---\n**Print only what you need** - counts, `head`, a few rows, the lines that matter. Never print a whole file or table.'
// Every prompt opens the same way too: agents that lost track of the project folder looked
// in the scratch copies, found two files, and reported the inputs "missing".
const WHERE = `**Project folder:** \`${CWD}\` - every input file is there. Start with \`cd ${CWD} && ls\`.`
function prompt(title, lines) {
  return [MARK, `# ${title}`, '', WHERE, '', ...lines.filter(l => l !== null && l !== undefined), FOOTER].join('\n')
}

// A think-only agent reasons over text this graph hands it, and the bridge gives it no
// tools but StructuredOutput: one upstream call instead of a tool loop. Agentaus
// throughput is the limit on a graph this size - about 30 calls a minute - so an agent
// that needs no tool must not spend calls looking around.
const THINK = '[think-only]'
function thinkPrompt(title, lines) {
  return [MARK, THINK, `# ${title}`, '', 'Everything you need is below. Reply only through `StructuredOutput`.', '',
    ...lines.filter(l => l !== null && l !== undefined)].join('\n')
}

// Single-quoted for the shell: a project folder with a space or a quote in its name must not
// split a command in two.
function q(path) {
  return `'${String(path).replace(/'/g, "'\\''")}'`
}

function short(value, n) {
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > n ? text.slice(0, n) + ' ...[cut]' : text
}

function norm(text) {
  return String(text || '').toLowerCase().replace(/[\s,_*`"'.:;$]+/g, ' ').trim()
}

// A slot is held until its agent settles - even one its caller stopped waiting for - so the
// number of agents upstream never passes WIDTH. Once the budget is spent, a queued agent
// whose phase only improves on the draft gives up its place, and the closing agents (the
// writer, the draft fallback) go to the front of the queue.
async function slot(fn, ph) {
  while (live >= WIDTH) {
    await new Promise(resolve => waiting.push({ resolve, ph }))
    if (late && SKIP_WHEN_LATE.has(ph)) return null
  }
  live += 1
  try { return await fn() } finally {
    live -= 1
    wake()
  }
}
function wake() {
  if (!waiting.length) return
  let k = 0
  if (late) {
    const closing = waiting.findIndex(w => !SKIP_WHEN_LATE.has(w.ph))
    if (closing >= 0) k = closing
  }
  waiting.splice(k, 1)[0].resolve()
}

// A result worth keeping: present, not empty, and not a plan in place of the work - the
// measured way Agentaus fails is "We will perform the edits." with nothing done.
const PLAN_SHAPED = /^\s*(i|we)('ll| will| am going to| are going to)\b|^\s*(let me|next,? i|first,? i)\b/i
function usable(out) {
  if (!out) return false
  if (typeof out !== 'object') return String(out).trim().length > 0
  if (!Object.keys(out).length) return false
  if (typeof out.answer === 'string' && (!out.answer.trim() || PLAN_SHAPED.test(out.answer))) return false
  return true
}

// One agent, with a backup: a dead, empty or plan-shaped result is asked again, once,
// with a note saying why. A throw is a dead agent, never the end of the graph.
// Work that only improves on the draft stops once the budget is spent.
const SKIP_WHEN_LATE = new Set(['Plan', 'Solve', 'Reconcile', 'Verify', 'Fix'])

async function ask(text, schema, label, ph, accept) {
  for (const attempt of [1, 2]) {
    if (late && SKIP_WHEN_LATE.has(ph)) return null
    const body = attempt === 1 ? text : text + '\n\n**Backup attempt:** the previous agent returned nothing usable, or a plan instead of the result. Do the work now with your tools, then return the result.'
    let out = null
    try {
      let timer = null
      // The deadline stops the waiting, not the agent: its slot stays held until it settles.
      out = await Promise.race([
        slot(() => agent(body, { label: attempt === 1 ? label : `${label}:backup`, phase: ph, schema }), ph),
        new Promise(resolve => { timer = setTimeout(() => resolve(null), DEADLINE_MS) }),
      ])
      // A pending timer would hold the workflow open for the full deadline after its agent
      // finished.
      if (timer !== null && typeof clearTimeout === 'function') clearTimeout(timer)
    } catch (e) {
      out = null
    }
    if (usable(out) && (!accept || accept(out))) return out
  }
  return null
}

// A value that says it could not be computed is not a value. Measured: three solvers
// said the input files were missing while they sat in the working directory.
const GAVE_UP = /\b(cannot|can't|unable to|could not|not present|not found|does not exist|no such file|missing)\b/i
// Only prose can give up: a JSON value or a number that happens to contain "missing" is data.
const isValue = out => {
  const a = String(out.answer || '').trim()
  return /^[\[{"\-\d$]/.test(a) || !GAVE_UP.test(a.slice(0, 300))
}
// Any answer that says the inputs are missing, while they sit in the project folder, was
// written without looking: a judge did exactly that with no tool call at all.
const FILES_MISSING = /\b(files?|data|inputs?|xlsx|csv|docx|directory|repository|project)\b[^.]{0,80}\b(missing|not present|absent|do(es)? not exist|not found|only contains|contains only)\b|\bonly (the file )?`?[\w.-]+`? (is |was )?(present|available|exists)\b/i
// Work is shown, not asserted: an answer must carry the output of something it ran.
const SHOWN = out => String(out.evidence || '').trim().length >= 20
const grounded = out => SHOWN(out) && !FILES_MISSING.test(String(out.answer || '').slice(0, 600))
const groundedValue = out => grounded(out) && isValue(out)

// The answer most of the agreeing results gave, or null when there is no majority.
function majority(values) {
  const counts = {}
  for (const v of values) {
    const k = norm(v)
    if (!k) continue
    counts[k] = counts[k] || { n: 0, v }
    counts[k].n += 1
  }
  const best = Object.values(counts).sort((a, b) => b.n - a.n)[0]
  return best && best.n >= 2 ? best.v : null
}

// ---------------------------------------------------------------------------------
// 1. Survey - two listers, merged
// ---------------------------------------------------------------------------------

phase('Survey')
const FILES_SCHEMA = { type: 'object', properties: { files: LIST }, required: ['files'] }
const listings = await parallel([0, 1].map(i => () => ask(prompt('List the files', [
  `List every file under \`${CWD}\`, recursively.`,
  '',
  '- Skip `.git`, `__pycache__`, `node_modules`, virtual environments and hidden files.',
  '- Give each as a path relative to that directory.',
  i === 0 ? '- Use `find` in Bash.' : '- Use `ls -R` in Bash.',
]), FILES_SCHEMA, `list-${i + 1}`, 'Survey')))
const allFiles = [...new Set(listings.filter(Boolean).flatMap(l => l.files || []))]
  .map(f => f.replace(/^\.\//, '')).filter(f => f && !f.endsWith('/'))
log(`${allFiles.length} files`)

// Files the request names come first; then documents, data and code; at most two from any
// one folder of look-alike files (a folder of forty PDFs is read as a sample).
const lowerTask = TASK.toLowerCase()
function rank(f) {
  const base = f.split('/').pop().toLowerCase()
  if (lowerTask.includes(base) || lowerTask.includes(f.toLowerCase())) return 0
  if (/\.(md|txt|docx?|pdf|csv|xlsx?|json|ya?ml)$/i.test(f)) return 1
  if (/\.(py|js|ts|r|sql|sh)$/i.test(f)) return 2
  return 3
}
const perFolder = {}
const toRead = allFiles.slice().sort((a, b) => rank(a) - rank(b)).filter(f => {
  const dir = f.includes('/') ? f.slice(0, f.lastIndexOf('/')) : '.'
  const ext = f.split('.').pop()
  const key = `${dir}|${ext}`
  perFolder[key] = (perFolder[key] || 0) + 1
  return rank(f) === 0 || perFolder[key] <= 2
}).slice(0, MAX_FILES)

// ---------------------------------------------------------------------------------
// 2. Read - one reader per file
// ---------------------------------------------------------------------------------

phase('Read')
const NOTE_SCHEMA = { type: 'object', properties: {
  what: STR, structure: STR, facts: LIST }, required: ['what', 'structure', 'facts'] }
const notes = (await parallel(toRead.map(f => () => ask(prompt(`Read one file: ${f}`, [
  `Open \`${CWD}/${f}\` and report what is in it.`,
  '',
  '- `.docx`: `python3 -c "import docx,sys; print(chr(10).join(p.text for p in docx.Document(sys.argv[1]).paragraphs))" FILE` (or `pandoc -t plain FILE`). Also print its tables.',
  '- `.xlsx` / `.csv`: pandas with `dtype=str, keep_default_na=False` (so 0 stays 0 and a blank stays blank); give sheet names, columns, row counts, first rows, and how many cells are blank or 0 in each column.',
  '- `.pdf`: `pypdf`. Code: the functions and what each docstring promises.',
  '',
  `- For a \`.docx\`, \`.pdf\`, \`.pptx\` or \`.xlsx\`: also save the full extracted text to \`${SCRATCH}/inputs/${'${NAME}'}.txt\` (make the folder first) so others can read it.`,
  '',
  '## Return',
  '- `what`: one line - what this file is.',
  '- `structure`: its sections, columns or functions.',
  '- `facts`: up to 25 facts, each quoted or copied exactly (numbers, names, dates, rules).',
].map(l => typeof l === 'string' ? l.split('${NAME}').join(f.split('/').pop()) : l)), NOTE_SCHEMA, `read:${f.split('/').pop()}`, 'Read'))))
const fileNotes = toRead.map((f, i) => notes[i] && { file: f, ...notes[i] }).filter(Boolean)
const EXTRACTED = toRead.filter(f => /\.(docx?|pdf|pptx|xlsx?)$/i.test(f))
  .map(f => `- \`${f}\` -> plain text in \`${SCRATCH}/inputs/${f.split('/').pop()}.txt\``)
const NOTES = (EXTRACTED.length ? `Extra plain-text copies of the documents (the originals stay in the project folder):\n${EXTRACTED.join('\n')}\n\n` : '') + short(fileNotes.map(n => `### ${n.file}\n${n.what}\n${n.structure}\n- ${(n.facts || []).join('\n- ')}`).join('\n\n'), 24000)

// ---------------------------------------------------------------------------------
// 3. Requirements - three extractors, one merger (with a backup)
// ---------------------------------------------------------------------------------

// ---------------------------------------------------------------------------------
// Draft - one agent does the whole task quickly, in a copy, as the backup deliverable
// ---------------------------------------------------------------------------------

const DRAFT = `${SCRATCH}/draft`
const DONE_SCHEMA = { type: 'object', properties: { done: LIST, problems: LIST }, required: ['done', 'problems'] }
const drafting = ask(prompt('Draft the whole task quickly', [
  `1. Copy the project: \`rm -rf ${DRAFT} && mkdir -p ${DRAFT} && cp -R ${CWD} ${DRAFT}/repo\``,
  `2. Do the whole request below **in the copy** (\`${DRAFT}/repo\`): write every file it asks for there, make every change there. Never touch \`${CWD}\`.`,
  '3. Be quick and complete rather than perfect: this is the backup if the careful work runs out of time.', '',
  '## The request', '```text', TASK, '```', '',
  '## The files (notes)', NOTES, '',
  '## Return', '- `done`: each file you wrote or changed in the copy, as a path relative to the copy.', '- `problems`: anything left undone.',
]), DONE_SCHEMA, 'draft', 'Read')

phase('Requirements')
const REQ_SCHEMA = { type: 'object', properties: {
  deliverables: LIST, requirements: LIST, rules: LIST }, required: ['deliverables', 'requirements', 'rules'] }
const extracting = parallel([0, 1, 2].map(i => () => ask(thinkPrompt('List what the request asks for', [
  'The request:', '', '```text', TASK, '```', '',
  '## Return three lists',
  '- `deliverables`: every file the request asks to be written or changed, as a path relative to the working directory.',
  '- `requirements`: every separate **output** the work must produce - each question to answer, each value to compute, each bug to fix, each feature to add, each section to write. One short line each, with the request\'s exact wording, keys, numbers and names. Not the steps to get there.',
  '- `rules`: every definition, constraint or format the request states that applies across the work (how to identify things, what counts, what to cite, what never to do). Exact wording.',
  i === 1 ? '- Go through the request line by line so nothing is missed.' : null,
  i === 2 ? '- If the request says there may be more problems than it lists, add one requirement: "find and fix any other problem in <file or module>".' : null,
]), REQ_SCHEMA, `requirements-${i + 1}`, 'Requirements')))
// Two trap hunters, each with one lens: what in the data, and what in the code or brief,
// would make a careful person get this wrong. Their findings go into every later prompt.
const TRAP_SCHEMA = { type: 'object', properties: { traps: LIST, unreported_bugs: LIST }, required: ['traps', 'unreported_bugs'] }
const hunting = parallel([
  ['data', 'Look at the DATA. Count, with code: values that are exactly 0 versus blank or missing (a 0 is a real value), rows with blank identifiers, the same identifier written in different formats, duplicates, one file overriding another. Load tables with pandas `dtype=str, keep_default_na=False`.'],
  ['spec', 'Look at the CODE and the BRIEF. Run the code and its tests if there are any. Compare every function with its docstring and the README, and list every place they disagree - especially problems nobody reported. List any question the files cannot answer, and any easy-to-miss format the request demands.'],
].map(([lens, job]) => () => ask(prompt(`Find the traps (${lens})`, [
  `**Working directory:** \`${CWD}\` - read only.`, '',
  '## The request', '```text', TASK, '```', '',
  '## The files (notes)', NOTES, '',
  '## Your job', job, '',
  '## Return', '- `traps`: each trap in one line, with the evidence (a count, a line number, a quote).', '- `unreported_bugs`: each problem in the code that the request did not mention, with where it is (file, function) and the evidence. [] if none or no code.',
]), TRAP_SCHEMA, `traps-${lens}`, 'Requirements')))
// Extractors and hunters run side by side: neither needs the other.
const extracted = (await extracting).filter(Boolean)
const hunters = (await hunting).filter(Boolean)
const TRAPS = [...new Set(hunters.flatMap(h => h.traps || []))].slice(0, 20)
const UNREPORTED = [...new Set(hunters.flatMap(h => h.unreported_bugs || []))].slice(0, 8)
const merged = await ask(thinkPrompt('Merge three lists', [
  'Three people listed what one request asks for. Merge each of their three lists into one.',
  '',
  '- Keep every distinct entry; drop duplicates.',
  '- Keep the exact wording, keys, numbers and names.',
  `- At most ${MAX_REQUIREMENTS} requirements: merge only near-identical ones to fit.`,
  '', '```json', JSON.stringify(extracted, null, 1), '```',
]), REQ_SCHEMA, 'merge-lists', 'Requirements')
const pick = key => (merged && merged[key] && merged[key].length ? merged[key]
  : [...new Set(extracted.flatMap(e => e[key] || []))])
const deliverables = [...new Set(pick('deliverables').map(d => d.replace(/^\.\//, '')))]
const requirements = (pick('requirements').length ? pick('requirements') : [TASK]).slice(0, MAX_REQUIREMENTS)
// A backstop no agent can talk its way past: every bullet, numbered or "Qn" line of the
// request that no requirement covers is added back, so a merger cannot drop a question.
const words = t => new Set(norm(t).split(' ').filter(w => w.length > 2))
for (const line of TASK.split('\n').map(l => l.trim())) {
  // Only a line that asks for an output: a key to fill, a question, a "Qn". A bullet that
  // describes an input file is not a requirement - the first backstop added those, and
  // their "answers" were written into the deliverable.
  if (!/^([-*]|\d+[.)]|q\d+\b)/i.test(line) || line.length < 12) continue
  if (!/\b[a-z]\w*_\w+\s*:|\?|^q\d+\b|^[-*]\s*q\d+\b/i.test(line)) continue
  const w = words(line)
  const covered = requirements.concat(pick('rules')).some(r => {
    const rw = words(r)
    let hit = 0
    for (const x of w) if (rw.has(x)) hit += 1
    return hit >= Math.max(2, Math.ceil(w.size * 0.5))
  })
  if (!covered && requirements.length < MAX_REQUIREMENTS * 2) requirements.push(line.replace(/^([-*]|\d+[.)])\s*/, ''))
}
// Near-copies of one requirement are one requirement: six extractors' versions of
// "implement every function" were expanded and solved six times over. But two requirements
// that differ by a year, a key, a "Qn" or a file name are different requirements.
const tokens = t => new Set(String(t || '').toLowerCase().match(/[a-z0-9_.]+/g) || [])
function nearCopy(a, b) {
  const ta = tokens(a), tb = tokens(b)
  const diff = [...ta].filter(x => !tb.has(x)).concat([...tb].filter(x => !ta.has(x)))
  if (diff.some(x => /[\d_.]/.test(x))) return false
  const both = [...ta].filter(x => tb.has(x)).length
  return both / Math.max(1, new Set([...ta, ...tb]).size) >= 0.8
}
{
  const kept = []
  for (const r of requirements) if (!kept.some(k => nearCopy(k, r))) kept.push(r)
  requirements.length = 0
  requirements.push(...kept)
}
const rules = pick('rules')
// Unreported bugs the spec hunter found become requirements of their own.
// Unreported bugs become work only when the request is about fixing something. On a task
// to implement stubs, the hunter's "the docstring disagrees with its example" became
// requirements to rewrite docstrings; there they are traps to know about instead.
const ABOUT_FIXING = /\b(bugs?|fix|fixes|broken|regress\w*|wrong|errors?|fails?|failing)\b/i.test(TASK)
if (ABOUT_FIXING) {
  for (const u of UNREPORTED) if (requirements.length < MAX_REQUIREMENTS * 2) requirements.push(`Fix this problem, which was not reported: ${u}`)
} else {
  TRAPS.push(...UNREPORTED.slice(0, 6))
}
const RULES = (rules.length ? rules.map(r => `- ${r}`).join('\n') : '- (none stated)')
  + (TRAPS.length ? '\n\n**Known traps:**\n' + TRAPS.map(t => `- ${t}`).join('\n') : '')
log(`${requirements.length} requirements, ${rules.length} rules, deliverables: ${deliverables.join(', ') || '(none named)'}`)

// ---------------------------------------------------------------------------------
// 4. Expand - one agent per requirement; splits it only into separate outputs
// ---------------------------------------------------------------------------------

phase('Expand')
const ITEMS_SCHEMA = { type: 'object', properties: {
  kind: { type: 'string', enum: ['value', 'code', 'text'] }, items: LIST }, required: ['kind', 'items'] }
const expandPrompt = (r, n) => thinkPrompt(`Split one requirement into separate outputs (${n})`, [
  `**Requirement:** ${r}`, '',
  '## Rules that apply to everything', RULES, '',
  '## The files (notes)', NOTES, '',
  '## Return',
  '- `kind`: `value` (a number or fact to compute), `code` (a change to code), or `text` (prose to write).',
  `- \`items\`: the separate **outputs** this requirement asks for. A value to compute is **one** item, however many steps it takes. Split only when it asks for several separate outputs: several questions, several bugs, several functions, several sections. To look for bugs you were not told about, give one item per function or section to audit. At most ${MAX_PER_REQUIREMENT}.`,
  '- Each item names the file, function, key or question it is about, so it makes sense on its own. **Never** a step such as "load the file".',
  '- **Do not solve anything.** Describe each output; others will produce it.',
])
// Two expanders per requirement, and the finer split wins: a weak model's usual failure
// here is one item listing all ten functions instead of ten items.
const expanded = await parallel(requirements.map((r, i) => async () => {
  const two = (await parallel([1, 2].map(n => () => ask(expandPrompt(r, n), ITEMS_SCHEMA, `expand-${i + 1}.${n}`, 'Expand')))).filter(Boolean)
  return two.sort((a, b) => (b.items || []).length - (a.items || []).length)[0] || null
}))
// An item that still names several functions or files is split, one item per name.
function splitNamed(item) {
  // "Answer Q1-Q9" is nine outputs: the questions sit in a file the extractors never saw.
  const range = item.match(/\bQ(\d{1,2})\s*(?:-|–|—|to|through)\s*Q?(\d{1,2})\b/i)
  if (range && Number(range[2]) > Number(range[1]) && Number(range[2]) - Number(range[1]) < MAX_PER_REQUIREMENT) {
    const out = []
    const rest = item.replace(range[0], 'the questions').replace(/\s+/g, ' ').slice(0, 160)
    for (let n = Number(range[1]); n <= Number(range[2]); n++) out.push(`Q${n} (one of: ${rest})`)
    return out
  }
  // A run of three or more comma-separated identifiers - "is_bored, intersection, ..." -
  // is a list of separate outputs.
  const run = item.match(/`?[A-Za-z_]\w*`?(?:\(\))?(?:\s*(?:,|,?\s+and)\s*`?[A-Za-z_]\w*`?(?:\(\))?){2,}/)
  if (!run) return [item]
  const names = [...new Set(run[0].split(/\s*(?:,|\band\b)\s*/).map(n => n.replace(/[`()]/g, '').trim()).filter(Boolean))]
  // Code names, not prose: "legal entity name, trading name, ABN and GST status" is one
  // question. Most of the run must look like identifiers.
  const codeLike = names.filter(n => /_|[a-z][A-Z]/.test(n) || run[0].includes('`' + n) || run[0].includes(n + '('))
  if (names.length < 3 || codeLike.length * 2 < names.length) return [item]
  const file = (item.match(/[\w./-]+\.\w{1,4}\b/) || [])[0]
  return names.map(n => `\`${n}\`${file ? ` in ${file}` : ''} - one of several: ${item.replace(run[0], 'several names').replace(/\s+/g, ' ').slice(0, 140)}`)
}

// Every requirement gets its first item before any gets a second, so the budget can never
// drop a requirement - the first version silently lost four of eight questions to it.
const perReq = requirements.map((r, i) => {
  const e = expanded[i] || { kind: 'value', items: [r] }
  // An item that shares no word with its requirement is an answer, not a description of
  // one - the expanders were seen "splitting" a question into its own computed value.
  const rw = words(r)
  const described = (e.items || []).filter(it => /[\w-]+\.\w+|\w\(|_/.test(it) || [...words(it)].some(w => rw.has(w)))
  const list = (described.length ? described : [r]).flatMap(splitNamed).slice(0, MAX_PER_REQUIREMENT)
  return list.map(it => ({ requirement: r, kind: kindOf(it, e.kind), item: it }))
})
// Implementing or fixing something in a code file is code work, whatever the expander
// called it: classified "text", ten function implementations produced no patches.
function kindOf(item, said) {
  const mentionsCode = new RegExp(`\\b[\\w/.-]+\\.(${CODE_EXT})\\b`, 'i').test(item)
    || deliverables.some(d => CODE_FILE.test(d) && item.includes(d.split('/').pop()))
  const changes = /\b(implement|fix|fixes|patch|refactor|rewrite|repair|add (a|an|the)? ?(flag|feature|option|function|method)|correct the|bug)\b/i.test(item)
  if (mentionsCode && changes) return 'code'
  if (changes && /`[A-Za-z_]\w*`/.test(item) && deliverables.some(d => CODE_FILE.test(d))) return 'code'
  return said || 'value'
}
// The same output named under several requirements is one item. Twelve requirements each
// began with `is_bored`, the budget filled with copies, and five functions were never solved.
// What makes two items the same output: the question number; for a value, the key it fills;
// for code, the function and its file. Nothing else - two questions about one column are
// two questions.
function itemKey(item, kind) {
  const qn = item.match(/^\s*`?Q(\d{1,2})\b/i)
  if (qn) return `q:${qn[1]}`
  if (kind === 'code') {
    const fn = functionName(item)
    return fn ? `fn:${(item.match(/[\w./-]+\.\w{1,4}\b/) || [''])[0]}:${fn}` : null
  }
  const key = item.match(/^\s*`?([a-z]\w*_\w+)`?\s*[:–—-]/i)
  return key ? `key:${key[1].toLowerCase()}` : null
}
// The one function an item is about, with its case kept - the swap looks it up by name.
function functionName(item) {
  const names = item.match(/`[A-Za-z_]\w*`|\b[A-Za-z_]+_[A-Za-z0-9_]+\b|\b[A-Za-z_]\w*(?=\()/g) || []
  const code = [...new Set(names.map(n => n.replace(/`/g, '')))].filter(n => !/\.(py|js|ts|json|md|csv|xlsx?)$/i.test(n))
  return code.length === 1 ? code[0] : null
}
let items = []
const seenKeys = new Set()
const seenWords = []
function isDuplicate(it) {
  const key = itemKey(it.item, it.kind)
  if (key) { if (seenKeys.has(key)) return true; seenKeys.add(key); return false }
  const w = words(it.item)
  if (seenWords.some(o => { let b = 0; for (const x of w) if (o.has(x)) b += 1; return b / Math.max(1, Math.min(w.size, o.size)) >= 0.85 })) return true
  seenWords.push(w)
  return false
}
for (let round = 0; round < MAX_PER_REQUIREMENT && items.length < MAX_ITEMS; round++) {
  for (const list of perReq) if (list[round] && items.length < MAX_ITEMS && !isDuplicate(list[round])) items.push(list[round])
}
const dropped = perReq.reduce((n, l) => n + l.length, 0) - items.length
log(`${items.length} items${dropped ? `, ${dropped} over the budget not solved` : ''}`)

// ---------------------------------------------------------------------------------
// 5-7. Plan, Solve, Reconcile - a pipeline per item, so a slow item delays only itself
// ---------------------------------------------------------------------------------

const PLAN_SCHEMA = { type: 'object', properties: {
  inputs: LIST, method: STR, traps: LIST, check: STR }, required: ['inputs', 'method', 'traps', 'check'] }
const ANSWER_SCHEMA = { type: 'object', properties: {
  answer: STR, evidence: STR, patch_file: STR }, required: ['answer', 'evidence', 'patch_file'] }

function copyDir(i, n) { return `${SCRATCH}/i${i + 1}/s${n}` }

// An item that is one named Python function gets a function, not a diff. Ten diffs against
// stubs in one shared file collided - their fuzzed hunks landed in the wrong places - and six
// of ten functions came out wrong although every patch "applied". Swapping whole function
// definitions by name, with Python's own parser, cannot collide.
function functionTarget(it) {
  if (it.kind !== 'code') return null
  const fn = functionName(it.item)
  if (!fn || !/^[A-Za-z_]\w*$/.test(fn)) return null
  // The file being changed, not whichever .py the item mentions first (it may be the tests).
  const pys = deliverables.filter(d => /\.py$/.test(d))
  const named = (it.item.match(/[\w./-]+\.py\b/g) || []).map(f => f.replace(/^\.\//, ''))
  const file = named.find(f => pys.includes(f)) || (pys.length === 1 ? pys[0] : named.find(f => !/test/i.test(f)) || null)
  return file ? { fn, file } : null
}
const AST_FIND = "n=[n for n in ast.parse(code).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn][0]; s=(n.decorator_list[0].lineno if n.decorator_list else n.lineno)-1"
function extractCommand(repoFile, fn, out) {
  return `python3 - ${q(repoFile)} ${fn} ${q(out)} <<'PY'\nimport ast,sys\nf,fn,out=sys.argv[1:4]; code=open(f).read(); lines=code.splitlines(keepends=True)\n${AST_FIND}\nopen(out,'w').write(''.join(lines[s:n.end_lineno])); print(open(out).read()[:3000])\nPY`
}
function replaceCommand(target, fn, src) {
  // A function the file does not have yet is appended rather than failing the swap.
  return `python3 - ${q(target)} ${fn} ${q(src)} <<'PY'\nimport ast,sys\nt,fn,src=sys.argv[1:4]; code=open(t).read(); lines=code.splitlines(keepends=True); new=open(src).read().rstrip(chr(10))+chr(10)\nfound=[n for n in ast.parse(code).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn]\nif found:\n    n=found[0]; s=(n.decorator_list[0].lineno if n.decorator_list else n.lineno)-1; lines[s:n.end_lineno]=[new]\nelse:\n    lines.append(chr(10)+chr(10)+new)\nopen(t,'w').write(''.join(lines)); print('replaced' if found else 'appended', fn)\nPY`
}

function solvePrompt(it, plan, method, n, i) {
  const dir = copyDir(i, n)
  const code = it.kind === 'code'
  const target = functionTarget(it)
  return prompt(`Solve one item (solver ${n})`, [
    `**Item:** ${it.item}`, `**Part of:** ${it.requirement}`, '',
    `**Working directory:** \`${CWD}\` - **read only.** Do not change anything in it.`, '',
    '## Rules that apply to everything', RULES, '',
    plan ? '## A plan (check it - it may be wrong)' : null,
    plan ? `- Inputs: ${(plan.inputs || []).join('; ')}\n- Method: ${plan.method}\n- Traps: ${(plan.traps || []).join('; ')}\n- Check: ${plan.check}` : null,
    '', '## Your method', method, '',
    code ? '## Steps' : null,
    code ? [
      `1. Copy the project: \`rm -rf ${dir} && mkdir -p ${dir} && cp -R ${CWD} ${dir}/repo\``,
      `2. Show the problem in the copy: run the code in \`${dir}/repo\` so you see it go wrong.`,
      `3. Fix it **in the copy** with small \`Edit\` calls - never rewrite a whole file.`,
      `4. Run the code again in the copy to show it now works.`,
      target
        ? `5. Put any \`import\` your function needs **inside** the function. Then save your function - run exactly:\n\`\`\`bash\n${extractCommand(`${dir}/repo/${target.file}`, target.fn, `${dir}/${target.fn}.py`)}\n\`\`\``
        : `5. Save your change as a patch - run exactly: \`cd ${dir} && diff -ruN -x __pycache__ -x .git ${CWD} repo | sed -e "s#^--- ${CWD}#--- a#" -e "s#^+++ repo#+++ b#" > fix.patch; wc -l fix.patch\``,
    ].join('\n') : null,
    '', '## Return',
    code
      ? `- \`answer\`: what was wrong (or "no bug") and why, in one or two lines.\n- \`patch_file\`: \`${target ? `${dir}/${target.fn}.py` : `${dir}/fix.patch`}\`, or "" if no change is needed.\n- \`evidence\`: the output that shows it failing before and working after.`
      : it.kind === 'text'
        ? '- `answer`: the finished text for this item, citing its source file in square brackets after each fact. Where the files do not hold the evidence, say so plainly - never invent.\n- `patch_file`: "".\n- `evidence`: the exact passages you used.'
        : '- `answer`: the value itself, exactly as it should appear (a number with no units or commas unless asked; JSON for a list or object).\n- `patch_file`: "".\n- `evidence`: the code you ran and its printed output.',
  ])
}

const VERDICT_SCHEMA = { type: 'object', properties: {
  answer: STR, why: STR, evidence: STR, patch_file: STR }, required: ['answer', 'why', 'evidence', 'patch_file'] }
function planPrompt(it) {
  return thinkPrompt('Plan one item', [
    `**Item:** ${it.item}`, `**Part of:** ${it.requirement}`, '',
    '## Rules that apply to everything', RULES, '',
    '## The files (notes)', NOTES, '',
    '## Return',
    '- `inputs`: the exact files, sheets, columns, functions or passages needed.',
    '- `method`: the steps, in two to five short lines.',
    '- `traps`: what could make a careful person get this wrong - a value of 0, a missing value, a duplicate, a different spelling or format, an edge case, a rule above.',
    '- `check`: how to tell the answer is right.',
  ])
}

function judgePrompt(it, i, candidates, traps, n) {
  return prompt(`Judge one item (judge ${n})`, [
    `**Item:** ${it.item}`, `**Part of:** ${it.requirement}`, '',
    '## Rules that apply to everything', RULES, '',
    `## ${candidates.length} independent answers`,
    ...candidates.map((c, k) => `### Answer ${k + 1}\n${short(c.answer, 3000)}\n\n**Evidence:** ${short(c.evidence, 1500)}${c.patch_file ? `\n\n**Patch file:** \`${c.patch_file}\`` : ''}`),
    '',
    '## Task',
    '1. **Even if they agree, test each trap below with your own code** - answers from the same kind of agent can share one mistake.',
    ...traps.map(t => `   - [ ] ${t}`),
    '2. Where they disagree, find out which is right by running the check yourself (read only - do not change the working directory).',
    it.kind === 'code' ? '3. For patches: `cat` each patch file; prefer the smallest one that fixes the item and changes nothing else.' : null,
    it.kind === 'value' ? `3. If no answer above is an actual value - one says a file is missing, say - compute it yourself. The files are in \`${CWD}\`.` : null,
    '',
    '## Return', it.kind === 'value' ? '- `answer`: **the value itself** - not a sentence about it.' : '- `answer`: the correct answer, in the same form as the answers above.', '- `why`: one line.', '- `evidence`: the command you ran to check it, and its output.',
    `- \`patch_file\`: ${it.kind === 'code' ? 'the path of the best patch file above, or "" if no change is needed' : '""'}.`,
  ])
}

async function reconcile(it, i, plan, solved) {
  let candidates = solved.filter(Boolean)
  let agreed = it.kind === 'value' ? majority(candidates.map(c => c.answer)) : null
  if (!agreed && it.kind === 'value') {
    // No majority: a fourth solver, the sceptic, before anyone judges - and the majority it
    // may create counts.
    const extra = await ask(solvePrompt(it, plan, METHODS[2], 4, i), ANSWER_SCHEMA, `solve-${i + 1}.4`, 'Reconcile', groundedValue)
    if (extra) candidates = candidates.concat([extra])
    agreed = majority(candidates.map(c => c.answer))
  }
  if (!candidates.length) return { ...it, answer: '', why: 'no solver returned', patch_file: '' }
  const traps = plan && plan.traps && plan.traps.length ? plan.traps : ['zero is a value', 'a missing value is not zero', 'an unreported case still counts']
  const accept = it.kind === 'value' ? groundedValue : grounded
  const verdict = await ask(judgePrompt(it, i, candidates, traps, 1), VERDICT_SCHEMA, `judge-${i + 1}`, 'Reconcile', accept)
  if (!verdict) {
    const best = candidates[0]
    return { ...it, answer: agreed || best.answer, why: 'judge unavailable; majority or first answer', patch_file: best.patch_file || '' }
  }
  // One judge against a majority of solvers is not enough: a second judge, who has not seen
  // the first, must reach the same answer before it overrides them. Measured, a lone judge
  // replaced three solvers' correct median with a wrong one.
  // A judge against a majority of solvers is escalated, not believed and not ignored: a
  // second judge, then a third if the two split. A lone judge once overrode a right
  // majority; another time a second judge's veto kept a wrong one.
  if (agreed && norm(verdict.answer) !== norm(agreed)) {
    const keepMajority = why => {
      const best = candidates.find(c => norm(c.answer) === norm(agreed)) || candidates[0]
      return { ...it, answer: agreed, why, patch_file: best.patch_file || '' }
    }
    const second = await ask(judgePrompt(it, i, candidates, traps, 2), VERDICT_SCHEMA, `judge-${i + 1}b`, 'Reconcile', accept)
    if (second && norm(second.answer) === norm(verdict.answer)) return { ...it, ...verdict, why: `two judges agree: ${verdict.why}` }
    const third = await ask(judgePrompt(it, i, candidates, traps, 3), VERDICT_SCHEMA, `judge-${i + 1}c`, 'Reconcile', accept)
    if (third && norm(third.answer) === norm(verdict.answer)) return { ...it, ...verdict, why: `judges 1 and 3 agree: ${verdict.why}` }
    return keepMajority('solver majority kept: the judges did not agree on another answer')
  }
  return { ...it, ...verdict }
}

phase('Solve')
const done = new Array(items.length).fill(null)
const solving = parallel(items.map((it, i) => async () => {
  const plan = await ask(planPrompt(it), PLAN_SCHEMA, `plan-${i + 1}`, 'Plan')
  const solved = await parallel(METHODS.slice(0, SOLVERS).map((m, k) => () =>
    ask(solvePrompt(it, plan, m, k + 1, i), ANSWER_SCHEMA, `solve-${i + 1}.${k + 1}`, 'Solve',
      it.kind === 'value' ? groundedValue : grounded)))
  done[i] = await reconcile(it, i, plan, solved)
  return done[i]
}))
await Promise.race([solving, lateSignal])
const unsettled = items.filter((_, i) => !done[i]).map(it => it.item)
if (unsettled.length) log(`time budget reached: ${unsettled.length} item(s) unsettled, the draft covers them`)
const settled = items.map((it, i) => done[i] || { ...it, answer: '(not settled in time - use the draft)', why: 'time budget', patch_file: '' })
let draftTimer = null
const draft = await Promise.race([drafting, new Promise(resolve => { draftTimer = setTimeout(() => resolve(null), late ? 1000 : DEADLINE_MS) })])
if (typeof clearTimeout === 'function') clearTimeout(draftTimer)
// Labelled by requirement, which carries the key or question the deliverable needs.
// Each answer is capped, not the whole: a long early answer must not push later ones out.
const PER_ITEM = Math.max(400, Math.floor(30000 / Math.max(1, settled.length)))
const RESULTS = settled.map((s, i) => `### ${i + 1}. ${s.requirement}\n(item: ${s.item})\n\n${short(s.answer, PER_ITEM)}`).join('\n\n')

// ---------------------------------------------------------------------------------
// 8. Assemble - patches applied by one agent; each deliverable written by one, with a backup
// ---------------------------------------------------------------------------------

phase('Assemble')
const swaps = settled.map(s => ({ s, t: functionTarget(s) }))
  .filter(x => x.t && x.s.patch_file && /\.py$/.test(x.s.patch_file))
  .map(x => ({ ...x.t, src: x.s.patch_file }))
const patches = settled.filter(s => s.patch_file && /\.patch$/.test(s.patch_file)).map(s => ({ file: s.patch_file, why: s.item }))
let applied = null
let swapped = null
if (swaps.length) {
  swapped = await ask(prompt('Put the chosen functions in place', [
    `Run each command below, in order. Each replaces one function in \`${CWD}\` with the version the judges chose.`, '',
    ...swaps.map((w, k) => `${k + 1}. \`${w.fn}\` in \`${w.file}\`:\n\`\`\`bash\n${replaceCommand(`${CWD}/${w.file}`, w.fn, w.src)}\n\`\`\``), '',
    `Then run \`python3 -c "import ast; ast.parse(open('${CWD}/${swaps[0].file}').read()); print('ok')"\` to check the file still parses.`, '',
    '## Return', '- `done`: each function replaced.', '- `problems`: any command that failed, with its error.',
  ]), DONE_SCHEMA, 'swap-functions', 'Assemble')
}
if (patches.length) {
  applied = await ask(prompt('Apply these patches', [
    `Apply each patch below to \`${CWD}\`, in order.`, '',
    ...patches.map((p, k) => `${k + 1}. \`cd ${CWD} && patch -p1 --forward -F 3 < ${p.file}\`  (${p.why})`), '',
    '- If a patch is rejected, `cat` it and make the same change by hand with small `Edit` calls.',
    '- If two patches change the same lines, combine them so both fixes hold.',
    '- Then run the code (or its tests) once to check nothing is broken.', '',
    '## Return', '- `done`: the patches applied.', '- `problems`: anything that could not be applied, or that broke when run.',
  ]), DONE_SCHEMA, 'apply-patches', 'Assemble')
}
// Nothing settled in time for a code task: the draft's changed files are the deliverable.
// Code changes only through settled patches. A code deliverable no patch touched is left
// as it is - or, when the draft changed it, takes the draft's version. Measured, a writer
// told to "write tools/bank.py from the results" rewrote it wholesale and broke it.
const codeDeliverables = deliverables.filter(d => CODE_FILE.test(d))
// Per file: the draft's version of any code deliverable no settled change touched. A diff
// patch can touch any file, so with diff patches the draft is not used at all.
const swappedFiles = new Set(swaps.map(w => w.file))
const fromDraft = draft && !patches.length
  ? (draft.done || []).map(f => f.replace(/^\.\//, '')).filter(f => CODE_FILE.test(f)
      && (codeDeliverables.length === 0 || codeDeliverables.includes(f)) && !swappedFiles.has(f))
  : []
if (fromDraft.length) {
  applied = await ask(prompt('Use the draft', [
    `No settled patch changed the code. Copy the draft's changed files into \`${CWD}\`:`, '',
    ...fromDraft.map(f => `- \`cp ${q(`${DRAFT}/repo/${f}`)} ${q(`${CWD}/${f}`)}\``), '',
    'Then run the code once to check it works.', '', '## Return', '- `done`: files copied.', '- `problems`: any error.',
  ]), DONE_SCHEMA, 'use-draft', 'Assemble')
}
const toWrite = deliverables.filter(d => !CODE_FILE.test(d))
const written = await parallel(toWrite.map(d => () => ask(prompt(`Write ${d}`, [
  `Write \`${CWD}/${d}\` from the settled results below. Use only these results and the request.`, '',
  '## The request', '```text', TASK, '```', '',
  '## Rules that apply to everything', RULES, '',
  '## Settled results', RESULTS, '',
  draft ? `## A quick draft (a backup - the settled results win wherever they exist)\nThe draft's copy of this file, if it wrote one: \`${DRAFT}/repo/${d}\`. Use it only for parts the settled results do not cover.` : null, '',
  '## Steps', '1. Write the file with the `Write` tool, exactly in the format the request asks for.', '2. Read it back and check every required part is there.',
  '', '## Return', '- `done`: what the file contains.', '- `problems`: anything missing.',
]), DONE_SCHEMA, `write:${d.split('/').pop()}`, 'Assemble')))

// ---------------------------------------------------------------------------------
// 9. Verify - one verifier per requirement; a fixer for each failure, then re-verify
// ---------------------------------------------------------------------------------

const CHECK_SCHEMA = { type: 'object', properties: {
  pass: { type: 'boolean' }, problem: STR, evidence: STR }, required: ['pass', 'problem', 'evidence'] }
const VERIFIERS = 3
function verifyPrompt(r, n) {
  return prompt(`Check one requirement (checker ${n})`, [
    `**Requirement:** ${r}`, '',
    `Check the work in \`${CWD}\` meets it. Open the deliverables (${deliverables.join(', ') || 'the changed files'}) and **run code** to check - do not judge by eye. Do not change anything.`, '',
    '## Rules that apply to everything', RULES, '',
    '## Return', '- `pass`: true unless you **showed** it is not met.', '- `problem`: what is wrong, exactly, or "".', '- `evidence`: the command you ran and its output.',
  ])
}
// A failure counts only when most checkers find it and show it: one weak checker's
// mistaken "fail" sent fixers to overwrite eight correct answers with wrong ones.
async function verifyAll(target, k, ph) {
  const votes = (await parallel(Array.from({ length: VERIFIERS }, (_, n) => () =>
    ask(verifyPrompt(target, n + 1), CHECK_SCHEMA, `${ph === 'Verify' ? 'verify' : 'reverify'}-${k + 1}.${n + 1}`, ph)))).filter(Boolean)
  const fails = votes.filter(v => !v.pass && String(v.evidence || '').length >= 15 && String(v.problem || '').trim())
  return { verified: votes.length > 0, pass: !(fails.length >= 2 || (votes.length === 1 && fails.length === 1)),
           problem: fails.map(f => f.problem).join(' | '), votes: votes.length }
}
phase('Verify')
// Rules are checked as well as requirements: "cite a source after each fact" or "a $0
// amendment still counts" is where a correct-looking deliverable goes wrong.
const targets = requirements.concat(rules).slice(0, MAX_REQUIREMENTS * 2)
const checks = late
  ? targets.map(() => ({ verified: false, pass: true, problem: 'not checked: time budget reached', votes: 0 }))
  : await parallel(targets.map((r, i) => () => verifyAll(r, i, 'Verify')))
const failing = targets.map((r, i) => ({ r, c: checks[i] })).filter(x => x.c.verified && !x.c.pass)

phase('Fix')
const fixes = []
// Fixes run one at a time, so they are the slowest stage: past MAX_FIXES the rest go back to
// the main loop as open issues rather than holding the whole result.
const MAX_FIXES = 4
for (const [k, f] of failing.slice(0, late ? 0 : MAX_FIXES).entries()) {
  // One at a time: fixers edit the same deliverables. Each fix has its own backup, taken by
  // its own agent before the fixer starts, and is undone from that backup only - never from
  // another fix's - if the checkers still find the problem.
  const backup = `${SCRATCH}/before-fix-${k + 1}`
  const saved = await ask(prompt('Back up the project', [
    `Run exactly: \`rm -rf ${q(backup)} && mkdir -p ${q(backup)} && cp -R ${q(CWD)}/. ${q(backup)}/ && ls ${q(backup)} | head\``, '',
    '## Return', '- `done`: the files listed.', '- `problems`: any error.',
  ]), DONE_SCHEMA, `backup-${k + 1}`, 'Fix')
  if (!saved) {
    fixes.push({ requirement: f.r, problem: f.c.problem, fixed: null, now_passes: false, restored: false, still: 'not fixed: no backup could be taken' })
    continue
  }
  const fixed = await ask(prompt('Fix one problem', [
    `**Requirement:** ${f.r}`, `**Problem the checkers found:** ${f.c.problem}`, '',
    '1. **Show the problem with your own code.** If you cannot reproduce it, change nothing.',
    '2. The settled results below were each reached by three solvers and a judge. **Change one only if your own code proves it wrong** - and say so in `done`.',
    `3. Fix it in \`${CWD}\` with small edits; keep everything else as it is.`, '',
    '## Settled results', RESULTS, '',
    '## Return', '- `done`: what you changed, or "nothing - could not reproduce".', '- `problems`: anything you could not fix.',
  ]), DONE_SCHEMA, `fix-${k + 1}`, 'Fix')
  // A re-check nobody completed is not a pass, and a fixer that never ran fixed nothing.
  const again = fixed ? await verifyAll(f.r, k, 'Fix') : { verified: false, pass: false, problem: 'the fixer did not run' }
  const nowPasses = !!(fixed && again.verified && again.pass)
  let restored = false
  if (fixed && again.verified && !again.pass) {
    const undo = await ask(prompt('Undo a fix that did not work', [
      `Run exactly: \`cp -R ${q(backup)}/. ${q(CWD)}/\``, '', '## Return', '- `done`: ["restored"].', '- `problems`: any error.',
    ]), DONE_SCHEMA, `undo-${k + 1}`, 'Fix')
    restored = !!undo
  }
  fixes.push({ requirement: f.r, problem: f.c.problem, fixed, now_passes: nowPasses, restored,
               still: nowPasses ? '' : (again.verified ? again.problem : `${again.problem || 'not re-checked'}`) })
}

const openIssues = fixes.filter(f => !f.now_passes).map(f => `${f.requirement}: ${f.still || f.problem}`)
  .concat(failing.slice(MAX_FIXES).map(f => `${f.r}: ${f.c.problem}`))
  .concat(targets.filter((r, i) => !checks[i].verified).map(r => `${r}: not verified`))
// Pending timers would hold the workflow open until the budget ran out.
if (typeof clearTimeout === 'function') { clearTimeout(budgetTimer); clearTimeout(lateTimer) }
const allPass = !late && checks.every(c => c.verified) && (failing.length === 0 || fixes.every(f => f.now_passes))
return {
  status: allPass && !openIssues.length ? 'verified' : 'needs-attention',
  next_step: allPass && !openIssues.length
    ? 'Open each deliverable to confirm it is there, then report the results.'
    : late
      ? 'The graph ran out of time. Open each deliverable, check it against the request with your own tools, fix what is wrong, then report.'
      : 'Fix each entry in open_issues with your own tools, check the deliverables, then report.',
  summary: late
    ? `Time budget reached: ${items.length - unsettled.length}/${items.length} items settled (${SOLVERS} ways each); the draft covers the rest; nothing was checked - check the deliverables yourself.`
    : `${items.length} items solved ${SOLVERS} ways and reconciled; ${checks.filter(c => c.verified && c.pass).length}/${targets.length} requirements and rules passed first check${checks.some(c => !c.verified) ? ` (${checks.filter(c => !c.verified).length} could not be checked)` : ''}; ${fixes.filter(f => f.now_passes).length} fixed after.`,
  deliverables,
  results: settled.map(s => ({ item: s.item, answer: short(s.answer, 1500), why: s.why })),
  applied,
  written: written.filter(Boolean),
  checks: targets.map((r, i) => ({ requirement: r, pass: checks[i].pass, problem: checks[i].problem })),
  fixes,
  open_issues: openIssues,
}
