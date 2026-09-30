# Dangerous-pattern catalogue

25 rules, adapted from `security-guidance/hooks/patterns.py` in
anthropics/claude-plugins-official (Apache-2.0). `scripts/scan_patterns.py` checks all of
them. The **Grep** line under each rule is a simpler pattern for the `Grep` tool. It can
over-match, so read every hit.

How to use an entry:

1. Find the rule name the scanner printed.
2. Read **Why dangerous** and decide whether the input can come from outside.
3. If it can, or you cannot tell, replace the code with **Write instead**.
4. If the input is fully trusted and the risky form is really needed, keep it and put a
   one-line comment above it saying why it is safe.

Untrusted input means: HTTP requests, uploaded or downloaded files, files from buckets,
database rows users can write, messages and webhooks, LLM output, config files inside a
cloned repository, and every GitHub event field.

## Contents

- A. Running commands: rules 1-5
- B. Evaluating strings as code: rules 6-7
- C. Writing HTML into a page: rules 8-13
- D. Deserializing data: rules 14-21
- E. Encryption and TLS: rules 22-24
- F. XML parsing: rule 25

## A. Running commands

### 1. `python_subprocess_shell`: Python
- **Matches:** `subprocess.run` / `call` / `Popen` / `check_output` / `check_call` with `shell=True` on the same line.
- **Why dangerous:** the command string goes through a shell, so `;`, `|`, `$(...)` and backticks in any interpolated value run as commands.
- **Write instead:**
  ```python
  # unsafe
  subprocess.run(f"ls {user_input}", shell=True)
  # safe: argument list, no shell
  subprocess.run(["ls", user_input])
  ```
- **Grep:** `subprocess\.\w+\(.*shell\s*=\s*True`

### 2. `os_system_injection`: Python
- **Matches:** `os.system(` or `from os import system`.
- **Why dangerous:** `os.system` always runs a shell. It is a command-injection sink.
- **Write instead:** `subprocess.run(["cmd", arg1, arg2], check=True)`.
- **Grep:** `os\.system\s*\(|from os import system`

### 3. `child_process_exec`: JavaScript / TypeScript
- **Matches (JS/TS files only):** `child_process.exec`, `execSync(`, or a bare `exec(`. `regex.exec(` is not matched.
- **Why dangerous:** `exec` runs the string through a shell, so interpolated input can inject commands.
- **Write instead:**
  ```js
  // unsafe
  exec(`command ${userInput}`)
  // safe: no shell, arguments passed as an array
  import { execFile } from 'node:child_process'
  execFile('command', [userInput], callback)
  ```
  Use `exec` only if you need shell features and the input is fixed.
- **Grep:** `child_process\.exec|execSync\(|\bexec\(` with glob `*.{js,jsx,ts,tsx,mjs,cjs}`

### 4. `go_exec_shell_injection`: Go
- **Matches:** `exec.Command("sh"`, `"bash"`, `"/bin/sh"` or `"/bin/bash"`.
- **Why dangerous:** `sh -c` turns the joined string into shell code.
- **Write instead:**
  ```go
  // unsafe
  exec.Command("sh", "-c", "ping -c 1 " + host)
  // safe: each argument separate, no shell
  exec.Command("ping", "-c", "1", host)
  ```
  Also validate: hostnames and IPs with `net.ParseIP()` or a hostname regex; paths with
  `filepath.Clean()` plus a check that the result is inside the allowed directory;
  numbers by parsing them to int or float first.
- **Grep:** `exec\.Command\(\s*"(sh|bash|/bin/sh|/bin/bash)"`

### 5. `github_actions_workflow`: GitHub Actions YAML
- **Matches:** files under `.github/workflows/`. The scanner reports each line containing `${{ github.event.` or `${{ github.head_ref`.
- **Why dangerous:** `${{ }}` is pasted into the script before the shell runs. An attacker who controls an issue title, PR title or commit message can inject commands.
- **Write instead:** pass the value through `env:` and quote the shell variable.
  ```yaml
  # unsafe
  run: echo "${{ github.event.issue.title }}"
  # safe
  env:
    TITLE: ${{ github.event.issue.title }}
  run: echo "$TITLE"
  ```
  A hit on a line inside an `env:` block is already the safe form.
- **Attacker-controlled fields:** `github.event.issue.title`, `.issue.body`,
  `.pull_request.title`, `.pull_request.body`, `.comment.body`, `.review.body`,
  `.review_comment.body`, `.pages.*.page_name`, `.commits.*.message`,
  `.head_commit.message`, `.head_commit.author.email`, `.head_commit.author.name`,
  `.commits.*.author.email`, `.commits.*.author.name`, `.pull_request.head.ref`,
  `.pull_request.head.label`, `.pull_request.head.repo.default_branch`,
  `.client_payload.*` (repository_dispatch: every field is attacker-set), and `github.head_ref`.
- **Ref injection:** never put untrusted input in the `ref:` of `actions/checkout`. Check
  that `client_payload.pr_number` matches `^[0-9]+$` before using it in
  `ref: refs/pull/${{ ... }}/head`.
- **Grep:** `\$\{\{\s*github\.(event\.|head_ref)` with glob `.github/workflows/*.y*ml`

## B. Evaluating strings as code

### 6. `eval_injection`: any language (not docs)
- **Matches:** `eval(` that is not a method call. `model.eval()` and `redis.eval()` are not matched. `.md`, `.txt`, `.json` and `.yaml` files are skipped.
- **Why dangerous:** `eval` runs arbitrary code.
- **Write instead:** `JSON.parse()` / `json.loads()` for data, `ast.literal_eval()` for Python literals, or a safe expression parser.
- **Grep:** `\beval\(` (also hits `.eval(`; skip those)

### 7. `new_function_injection`: JavaScript / TypeScript
- **Matches:** `new Function` in JS/TS files.
- **Why dangerous:** any variable concatenated or interpolated into the function body lets whoever controls it run code.
- **Write instead:** property access with `obj[key]` or `path.reduce((o, k) => o[k], root)`; a lookup table of functions; or a safe expression parser. Never interpolate strings into a `new Function()` body.
- **Grep:** `new Function`

## C. Writing HTML into a page

### 8. `innerHTML_xss`: JavaScript / TypeScript
- **Matches:** `.innerHTML =` in JS/TS files.
- **Why dangerous:** assigned text is parsed as HTML; `<img onerror=...>` runs script.
- **Write instead:** `el.textContent = s` for text; `createElement` and `appendChild` for structure; `el.innerHTML = DOMPurify.sanitize(s)` if HTML is really needed.
- **Grep:** `\.innerHTML\s*=`

### 9. `outerHTML_xss`: JavaScript / TypeScript
- **Matches:** `.outerHTML =`.
- **Why dangerous:** the same XSS sink as `innerHTML`.
- **Write instead:** `textContent`, DOM methods, or `DOMPurify.sanitize`.
- **Grep:** `\.outerHTML\s*=`

### 10. `insertAdjacentHTML_xss`: JavaScript / TypeScript
- **Matches:** `.insertAdjacentHTML(`.
- **Why dangerous:** its argument is parsed as HTML.
- **Write instead:** `insertAdjacentText()`, or sanitize with DOMPurify.
- **Grep:** `\.insertAdjacentHTML\(`

### 11. `document_write_xss`: JavaScript / TypeScript
- **Matches:** `document.write`.
- **Why dangerous:** writes raw HTML into the page (XSS), and it hurts performance.
- **Write instead:** `document.createElement()` and `appendChild()`.
- **Grep:** `document\.write`

### 12. `react_dangerously_set_html`: React
- **Matches:** `dangerouslySetInnerHTML`.
- **Why dangerous:** bypasses React's escaping.
- **Write instead:** render text as `{value}`. If HTML is really needed: `dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(html) }}`.
- **Grep:** `dangerouslySetInnerHTML`

### 13. `script_src_without_sri`: HTML in any file
- **Matches:** `<script ... src="https://..." >` or `src="//..."` with no `integrity=` attribute.
- **Why dangerous:** if the CDN is compromised, its script runs with your page's rights.
- **Write instead:**
  ```html
  <script src="https://cdn.example.com/lib.js"
          integrity="sha384-<hash>" crossorigin="anonymous"></script>
  ```
- **Grep:** `<script[^>]+src\s*=\s*["'](https?:)?//` then check for `integrity`

## D. Deserializing data

All rules in this section share one reason: these loaders can construct arbitrary objects,
so a crafted file runs code when loaded. Shared fix: for simple data, use JSON or msgspec.
For typed objects, use a schema-validated deserializer that builds only declared types
(msgspec.Struct, pydantic, marshmallow). If the file is always your own and never
crosses a trust boundary, add a comment saying so.

### 14. `pickle_deserialization`: Python
- **Matches (Python files):** `pickle.load(`, `pickle.loads(`, `pickle.Unpickler`, `pkl_load(`. `pickle.dump` is fine.
- **Write instead:** `json.load(f)`, or `msgspec.json.decode(data, type=MyStruct)`.
- **Grep:** `\bpickle\.(loads?|Unpickler)\b|\bpkl_load\(`

### 15. `pickle_variants_load`: Python
- **Matches:** `cPickle.load(s)`, `cloudpickle.load(s)`, `dill.load(s)`.
- **Write instead:** as rule 14.
- **Grep:** `\b(cPickle|cloudpickle|dill)\.loads?\s*\(`

### 16. `pickle_wrapper_load`: Python
- **Matches:** `joblib.load(`, `pd.read_pickle(` / `pandas.read_pickle(`, `.cloudpickle_load(`, and `np.load(` / `numpy.load(` **only with** `allow_pickle=True` (the default has been `False` since numpy 1.16.3).
- **Write instead:** `np.load(p)` without `allow_pickle`; `pd.read_parquet` / `pd.read_csv`; for models, a format such as safetensors or ONNX.
- **Grep:** `joblib\.load\s*\(|read_pickle\s*\(|allow_pickle\s*=\s*True`

### 17. `marshal_loads`: Python
- **Matches:** `marshal.load(` / `marshal.loads(`.
- **Write instead:** `json.loads`. `marshal` is for Python internals, not data exchange.
- **Grep:** `\bmarshal\.loads?\s*\(`

### 18. `shelve_open`: Python
- **Matches:** `shelve.open(`.
- **Why dangerous:** `shelve` is pickle on disk.
- **Write instead:** `sqlite3`, or JSON files.
- **Grep:** `\bshelve\.open\s*\(`

### 19. `unsafe_yaml_load`: Python (PyYAML)
- **Matches:** `yaml.load(` without `Safe` within 80 characters on the same line. `yaml.load(f, Loader=yaml.SafeLoader)` is fine.
- **Why dangerous:** `yaml.load` builds arbitrary Python objects from `!!python/object` tags.
- **Write instead:** `yaml.safe_load(f)`. For typed data, `safe_load` then validate with pydantic, msgspec or marshmallow. Never write a custom Loader that constructs arbitrary types.
- **Grep:** `yaml\.load\s*\(` then check for `SafeLoader`. The check is per line, so a multi-line call can be a false hit.

### 20. `yaml_unsafe_load_variants`: Python
- **Matches:** `yaml.unsafe_load(` and wrapper methods named `.yaml_unsafe_load(`.
- **Write instead:** `yaml.safe_load(f)`.
- **Grep:** `yaml\.unsafe_load\s*\(|\.yaml_unsafe_load\s*\(`

### 21. `torch_unsafe_load`: Python (PyTorch)
- **Matches:** `torch.load(` or `.torch_load(` without `weights_only=True` within 200 characters on the same line. `weights_only=False` still matches.
- **Why dangerous:** in PyTorch versions where `weights_only` defaults to `False`, `torch.load` unpickles arbitrary objects.
- **Write instead:** `torch.load(path, weights_only=True)`, or set `TORCH_FORCE_WEIGHTS_ONLY_LOAD=1`. For sharing weights, prefer safetensors.
- **Grep:** `torch\.load\s*\(|\.torch_load\s*\(` then check for `weights_only=True`

## E. Encryption and TLS

### 22. `node_createcipher_no_iv`: Node.js
- **Matches:** `crypto.createCipher` / `crypto.createDecipher`.
- **Why dangerous:** no IV, and it derives the key with an MD5-based function. Removed in Node 22.
- **Write instead:**
  ```js
  const iv = crypto.randomBytes(12)
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv)
  ```
- **Grep:** `crypto\.(createCipher|createDecipher)\b`

### 23. `aes_ecb_mode`: any language
- **Matches:** `AES.MODE_ECB`, `modes.ECB(`, or a string such as `'aes-128-ecb'`.
- **Why dangerous:** ECB encrypts identical blocks to identical ciphertext, so patterns in the data show through.
- **Write instead:** AES-GCM (authenticated). If not available, AES-CBC with a random IV plus an HMAC.
- **Grep:** `MODE_ECB|modes\.ECB|aes-\d+-ecb`

### 24. `tls_verification_disabled`: any language
- **Matches:** `verify=False`, `rejectUnauthorized: false`, `InsecureSkipVerify: true`, `NODE_TLS_REJECT_UNAUTHORIZED=0`, `ssl._create_unverified_context`, `check_hostname=False`.
- **Why dangerous:** anyone on the network path can impersonate the server (man-in-the-middle).
- **Write instead:** keep verification on. For a self-signed development certificate, add its CA to the trust store (`verify="/path/to/ca.pem"`, `ca:` option in Node, a custom `RootCAs` pool in Go), or use a properly issued certificate.
- **Grep:** `verify\s*=\s*False|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true|NODE_TLS_REJECT_UNAUTHORIZED|_create_unverified_context|check_hostname\s*=\s*False`

## F. XML parsing

### 25. `xml_unsafe_parse`: Python
- **Matches:** `ElementTree` / `ET` `.parse(`, `.fromstring(`, `.XML(`; `minidom.parse(` / `parseString(`; `xml.sax.parse` / `make_parser`.
- **Why dangerous:** the standard-library parsers are open to XXE (external entities that read local files) and "billion laughs" entity-expansion attacks.
- **Write instead:**
  ```python
  import defusedxml.ElementTree as ET
  tree = ET.parse(path)
  ```
- **Grep:** `(ElementTree|ET)\.(parse|fromstring|XML)\s*\(|minidom\.parse|xml\.sax\.(parse|make_parser)`
