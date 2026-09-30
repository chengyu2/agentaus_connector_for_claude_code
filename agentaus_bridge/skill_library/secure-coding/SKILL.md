---
name: secure-coding
description: Write and check code against a catalogue of 25 known-dangerous patterns (shell command injection, eval and new Function, pickle, yaml.load and torch.load deserialization, innerHTML and other XSS sinks, disabled TLS verification, ECB and IV-less ciphers, XXE-prone XML parsers, GitHub Actions workflow injection, CDN scripts without SRI), each with the safe code to write instead, plus a pre-commit self-check for SQL injection, path traversal, SSRF, IDOR, auth bypass and hardcoded secrets. Use when asked for a security check or "is this secure", or when writing, editing or about to commit code that runs shell commands, evals strings, loads pickle, YAML or model files, sets innerHTML, parses XML, turns off TLS checks, uses paths or URLs from users, or edits GitHub workflows.
license: Apache-2.0
metadata:
  source: "anthropics/claude-plugins-official plugins/security-guidance/hooks/patterns.py; plugins/security-guidance/README.md; review criteria prose in plugins/security-guidance/hooks/llm.py (criteria only, no code)"
  adapted-for: agentaus
---

# Secure coding: dangerous patterns and what to write instead

## When to use

- You are writing or editing code that runs shell commands, turns strings into code,
  loads pickle / YAML / joblib / torch / numpy files, writes HTML into a page, parses
  XML, sets TLS or cipher options, builds file paths, URLs or SQL from input, or edits
  `.github/workflows/`.
- "Is this secure?", "security check", "check for vulnerabilities before I commit".
- For a full review of a change, use `thorough-code-review` and run this skill as well.

## Steps

1. Make a task list with `TodoWrite`: one item per step.
2. Fix the scope: the files you are writing now; otherwise `git diff --name-only`,
   `git diff --staged --name-only` and `git ls-files --others --exclude-standard`.
3. Run the pattern scanner with `Bash`. `scripts/` is next to this file.
   - Changed lines only: `git diff -U0 HEAD | python3 <skill-dir>/scripts/scan_patterns.py --diff`
   - Whole files or folders: `python3 <skill-dir>/scripts/scan_patterns.py <path> ...`
   - Output is `path:line: rule_name: matched text`.
   - The script cannot run → `Grep` with each rule's **Grep** line in `references/patterns.md`.
4. For each hit:
   1. `Read` the line with 20 lines of context.
   2. Find the rule in `references/patterns.md`.
   3. Trace the value back to where it comes from.
   4. It can come from outside (request, upload, download, bucket, user-written database
      row, webhook, LLM output, config in a cloned repo, GitHub event) or you cannot
      tell → replace the code with the rule's **Write instead**.
   5. It is fully trusted and the risky form is needed → keep it, and add a one-line
      comment above it saying why it is safe.
5. Go through the pre-commit self-check below for every changed function. These
   problems have no fixed pattern: answer each question by reading the code.
6. High-risk change → get an independent review. The change handles HTTP requests,
   login or permissions, uploads or downloads, fetches URLs built from input, or runs
   commands: launch 3 `Agent` calls in one turn, each `subagent_type: "general-purpose"`.
   Paste "Rules for every reviewer" from `references/vulnerability-classes.md` into each,
   plus one group per agent (A, B or C). Add the repository root, the changed-file list
   and the diff.
7. Verify every finding from steps 4-6: `Read` the cited line, and confirm there is a
   concrete path from untrusted input to the dangerous call. No path → drop it.
8. Fix or report. Code you are writing: fix it now, then re-run step 3. Someone else's
   change: report with the template below.

## Pre-commit self-check

Answer every question for the changed code. Any "yes" → fix it before committing.

1. **Command injection:** does input reach a shell (`shell=True`, `os.system`, JS
   `exec`, `sh -c`, or a helper that wraps one)? Can an argument that starts with `-`
   reach a command line without `--` before it?
2. **SQL injection:** is a value put into SQL with an f-string, `+` or `format` instead
   of a parameter (`%s`, `?`, `$1`)?
3. **Path traversal:** is a file path built from input without `realpath` and a check
   that the result is inside the allowed directory? `os.path.join` alone does not stop `../`.
4. **SSRF:** does a URL or host from input reach an outbound request without rejecting
   loopback, private and `169.254.x.x` addresses after DNS resolution, and on every redirect?
5. **IDOR:** for each endpoint that takes an ID, if user A sends user B's ID, does
   anything stop them? Do list queries filter by the current user?
6. **Auth bypass:** does an access decision trust a client-set value (`X-Forwarded-For`,
   `Host`, `Origin`, an `X-User-*` header, a body field like `is_admin`)?
7. **Hardcoded secrets:** is a password, API key, private key, token or framework
   secret (`SECRET_KEY`) written as a literal? Try
   `Grep` for `(?i)(password|passwd|secret|api_?key|token)\s*[:=]\s*["'][^"']{8,}`.
8. **Secrets in output:** are tokens, passwords or personal data logged, put in a URL
   query string, or returned in an error message (`str(exc)`, raw upstream body)?
9. **XSS:** is input written into HTML without escaping (autoescape off, `|safe`,
   `mark_safe`, `<%- %>`, HTML built with string formatting, an `href` that accepts `javascript:`)?
10. **Deserialization:** is untrusted data loaded with pickle, `yaml.load`,
    `torch.load` without `weights_only=True`, `joblib.load` or `ObjectInputStream`?
11. **Randomness:** are tokens, reset codes or session IDs made with `random`,
    `Math.random` or `math/rand` instead of `secrets`, `crypto.randomBytes` or `crypto/rand`?
12. **Passwords:** stored with MD5, SHA-1 or SHA-256 instead of bcrypt, scrypt, argon2
    or PBKDF2?
13. **Open redirect:** does a `next` or `redirect` parameter reach a redirect without
    checking it starts with `/` and not `//`?
14. **Credential files:** is a token or key written to disk without `0o600` permissions?
15. **CI:** in a workflow, does `${{ github.event.* }}` appear inside `run:`? Is a
    third-party action pinned by tag instead of a full commit SHA?

## Severity

- **Critical:** actively exploitable remote code execution, auth bypass or data breach.
- **High:** a significant vulnerability such as IDOR, SQL injection or XSS.
- **Medium:** a defence-in-depth gap such as disabled CSRF protection.
- **Low:** a best-practice improvement.

## If this → do that

- `yaml.load(f, Loader=yaml.SafeLoader)` → safe. No change.
- `np.load(p)` without `allow_pickle=True` → safe. No change.
- `model.eval()` or `redis.eval(...)` → a method, not the `eval` builtin. No change.
- Scanner hit on a workflow line inside an `env:` block → that is the safe form.
- `subprocess.run(["grep", pattern, path])` with user `pattern` → no shell, but flag
  injection is possible: `["grep", "-e", pattern, "--", path]`.
- A comment says "validated upstream" or "not user input" → find the validation. If you
  cannot find it, treat the input as untrusted.
- Hit in a test or fixture with constant data → no change.
- Self-signed certificate in development → add its CA to the trust store. Do not turn
  off verification.
- The fix needs a new dependency (`defusedxml`, `DOMPurify`) → check whether it is
  installed. If not, tell the user and add it to the dependency file only if they agree.
- The line was not changed by this diff → leave it, unless the new code sends untrusted
  data into it.

## Done when

- The scanner (or the `Grep` fallback) covered every changed file.
- Every hit is fixed, or has a comment saying why it is safe.
- All 15 self-check questions were answered for the changed code.
- For a high-risk change, all 3 reviewers returned, and each finding was verified.

## Report template

~~~markdown
## Security check: <scope>

Scanner: <n> hits. Self-check: 15/15 answered. Independent review: <ran / not needed, why>.

| Location | Class / rule | Severity | Decision |
| --- | --- | --- | --- |
| `path:line` | `unsafe_yaml_load` | High | Fixed: `yaml.safe_load` |
| `path:line` | IDOR | High | Needs fix: add ownership check before update |
| `path:line` | `eval_injection` | - | Safe: constant input, comment added |
~~~
