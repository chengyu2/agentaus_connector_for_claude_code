// Dry run of agentaus_bridge/graphs/solve.js with a mock agent(): schema-shaped answers,
// a null every 17th call to exercise the backups. Used by tests/test_ultracode.py.
import fs from 'fs'
const src = fs.readFileSync(process.argv[2], 'utf8').replace('export const meta', 'const meta')
let n = 0, nulls = 0
const fake = (schema, label) => {
  if (!schema) return 'text'
  const o = {}
  for (const [k, v] of Object.entries(schema.properties || {})) {
    if (v.type === 'array') o[k] = k === 'files' ? ['a.csv', 'b.xlsx', 'docs/x.docx', 'tools/bank.py'] : k === 'patch' ? (label.startsWith('solve-2') ? [{ file: 'tools/bank.py', old: 'a', new: 'b' }] : []) : k === 'requirements' ? ['q1 value', 'q2 value', 'fix bug', 'write report'] : k === 'rules' ? ['zero counts'] : k === 'traps' ? ['a 0 amendment'] : k === 'unreported_bugs' ? (label === 'traps-spec' ? ['bank.py count_top uses < not <='] : []) : k === 'deliverables' ? ['answers.json'] : k === 'items' ? ['item A', 'item B'] : ['x']
    else if (v.type === 'boolean') o[k] = label.startsWith('verify-2') ? false : true
    else if (v.enum) o[k] = label === 'expand-3' ? 'code' : 'value'
    else if (k === 'patch_file') o[k] = /^(solve|judge)-3/.test(label) ? '/tmp/x/fix.patch' : ''
    else o[k] = k === 'answer' ? (label.endsWith('.3') ? '41' : '42') : k === 'evidence' ? 'ran python3 check.py, printed 42' : k === 'problem' ? (label.startsWith('verify-2') ? 'q2 is 41 in the file' : '') : 'ok'
  }
  return o
}
let inflight = 0, peak = 0
const agent = async (prompt, opts = {}) => {
  inflight++; peak = Math.max(peak, inflight); await new Promise(r => setTimeout(r, 5)); inflight--
  n++
  if (!prompt.startsWith('[agentaus-graph]')) throw new Error('prompt missing MARK: ' + opts.label)
  if (n % 17 === 0) { nulls++; return null }
  return fake(opts.schema, opts.label || '')
}
const parallel = async (thunks) => Promise.all(thunks.map(t => t().catch(e => { console.error('THUNK ERR', e); return null })))
const pipeline = async () => { throw new Error('unused') }
const phase = () => {}, log = (m) => console.error('log:', m)
const args = { task: 'ultracode: compute q1 and q2 from a.csv and b.xlsx, fix the bug in tools/bank.py, write answers.json', cwd: '/tmp/work' }
const f = new Function('agent', 'parallel', 'pipeline', 'phase', 'log', 'args', 'budget', `return (async () => { ${src} })()`)
const out = await f(agent, parallel, pipeline, phase, log, args, {})
console.log(JSON.stringify({ peak, agents: n, nulls, summary: out.summary, deliverables: out.deliverables, nresults: out.results.length, fixes: out.fixes.length, open: out.open_issues }, null, 1))
