# Documentation

The [README](../README.md) covers setup and day-to-day use. These pages hold the detail.

## Operating the bridge

| Page | What is in it |
| --- | --- |
| [Configuration reference](operating/configuration.md) | Every documented setting, including PDF and OCR tuning, search limits, retries and timeouts, and the ultracode agent graph |
| [Behaviours worth knowing about](operating/behaviours.md) | Re-asking turns that stop early, distillation, where output goes, truncated output read back from disk, the MCP schema budget, what typing "ultracode" does |
| [Troubleshooting and the log](operating/troubleshooting.md) | `diagnose.py` and how to read it, where the log is, every log line, and the full symptom table |

## Design notes

Why the bridge works the way it does, with the measurements behind each decision.

| Page | What is in it |
| --- | --- |
| [Working with Agentaus](design/working-with-agentaus.md) | What the live model taught us, why the guidance describes only what is on the wire, pattern matching versus judgement |
| [Search and reading](design/search-and-reading.md) | Aiming before reading, the three extractors, Tree-sitter for code, Zoom |
| [Reading documents](design/documents.md) | Office documents through LibreOffice, why not strip the XML, the PDF extractor ladder |
| [Grounding](design/grounding.md) | When a claim outruns the evidence: the tool ledger and the grounding pass |
| [Skills](design/skills.md) | Why the bridge injects skills itself, routing, the bundled skill library |
| [Ultracode](design/ultracode.md) | Multi-agent workflows on Agentaus: the faults found, the fixed and dynamic agent graphs, and their results |

## Benchmarks

[Benchmarks](benchmarks/README.md): the Claude Code A/B against Opus, and what each suite in
`benchmarks/` is really testing.
