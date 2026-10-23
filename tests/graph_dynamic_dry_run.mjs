// Dry run of agentaus_bridge/graphs/dynamic.js with a mock agent() that answers in the
// Markdown each skill describes, drops every 13th call, and fails one check - so the
// backups, the redo and the merge path all run. Used by tests/test_ultracode.py.
import fs from 'fs'
const src = fs.readFileSync(process.argv[2], 'utf8').replace('export const meta', 'const meta')
const skills = Object.fromEntries(fs.readdirSync(process.argv[3]).map(f => [f.replace(/\.md$/, ''), fs.readFileSync(`${process.argv[3]}/${f}`, 'utf8')]))
let n = 0, nulls = 0, inflight = 0, peak = 0, missingSkill = 0
const labels = []
const PLAN = `# Plan

## Deliverables
- \`answers.json\` - q1 and q2

## Subtask 1: q1 total
- Difficulty: hard
- Solvers: 3
- Check: yes
- Merge: vote
- Kind: value
- Depends on: none

### Task
Compute q1 from a.csv.

## Subtask 2: q2 count
- Difficulty: easy
- Solvers: 1
- Check: no
- Merge: vote
- Kind: value
- Depends on: 1

### Task
Count rows in b.xlsx.

## Subtask 3: q3 share
- Difficulty: easy
- Solvers: 1
- Check: no
- Merge: vote
- Kind: value
- Depends on: 4

### Task
The share of q4 in c.csv.

## Subtask 4: q4 rows
- Difficulty: easy
- Solvers: 1
- Check: no
- Merge: vote
- Kind: value
- Depends on: none

### Task
Count rows in c.csv.

## Assembly
Write answers.json with keys q1 and q2.
`
const answer = v => `## Answer\n${v}\n\n## Evidence\nran python3 -c ... printed ${v}\n\n## Change\nnone`
async function agent(prompt, opts = {}) {
  n++; inflight++; peak = Math.max(peak, inflight)
  await new Promise(r => setTimeout(r, 5))
  inflight--
  const label = opts.label || ''
  labels.push(label)
  if (!prompt.startsWith('[agentaus-graph]')) throw new Error('prompt missing MARK: ' + label)
  if (!/## Your skill: \w/.test(prompt) || /## Your skill: [\w-]+\n\n---/.test(prompt)) missingSkill++
  if (n % 13 === 0) { nulls++; return null }
  if (label.startsWith('scout')) return '# Brief\n\n## Files\n- `a.csv` - data'
  if (label.startsWith('plan') || label.startsWith('review')) return PLAN
  // The bridge's give-up reply to an agent far past its limit is no answer: a backup follows.
  if (label === 'solve-4.1') return '(out of time)'
  if (label.startsWith('solve')) return answer(label.endsWith('.3') ? '41' : '42')
  if (label.startsWith('merge')) return answer('42')
  if (label.startsWith('check-1')) return '**Verdict:** FAIL\n\n## Problem\nq1 ignores a zero row\n\n## Evidence\nran it, printed 41'
  if (label.startsWith('check') || label.startsWith('final')) return '**Verdict:** PASS\n\n## Problem\nnone\n\n## Evidence\nran it, all keys present'
  if (label.startsWith('assemble')) return '## Written\n- `answers.json` - q1, q2\n\n## Problems\nnone'
  return '## Fixed\nnothing\n\n## Evidence\nran it'
}
const parallel = async thunks => Promise.all(thunks.map(t => t().catch(e => { console.error('THUNK', e); return null })))
const args = { task: 'ultracode: compute q1 and q2 from a.csv and b.xlsx, write answers.json', cwd: '/tmp/work', skills }
const f = new Function('agent', 'parallel', 'pipeline', 'phase', 'log', 'args', 'budget', `return (async () => { ${src} })()`)
const out = await f(agent, parallel, async () => {}, () => {}, m => console.error('log:', m), args, {})
// Each subtask waits only on its own dependencies: subtask 3 (after 4) starts while subtask 1
// is still being checked and redone - never behind the slowest of an earlier wave.
const problems = []
if (!labels.includes('solve-4.1:backup')) problems.push('"(out of time)" was taken as an answer')
if (!(labels.indexOf('solve-3.1') >= 0 && labels.indexOf('solve-3.1') < labels.indexOf('solve-1.redo'))) problems.push('subtask 3 waited for subtask 1')
if (/\(out of time\)/.test(out)) problems.push('"(out of time)" reached the result')
if (problems.length) { console.error(problems.join('\n')); process.exitCode = 1 }
console.log(JSON.stringify({ agents: n, nulls, peak, missingSkill, labels, problems, result: out }, null, 1))
