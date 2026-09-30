# Agent Skill format - fields, layout and pitfalls

The Agent Skills specification (agentskills.io/specification) as it applies to Claude Code
and to Agentaus through the bridge. `scripts/validate_skill.py` checks every rule here that
can be checked mechanically.

## Contents

1. Directory layout
2. Front matter fields
3. YAML pitfalls
4. Progressive disclosure and sizes
5. How Agentaus sees a skill
6. Checklist

## 1. Directory layout

```
skill-name/
├── SKILL.md          required: front matter + Markdown body
├── references/       optional: docs opened when the body says so
├── scripts/          optional: code that is run, not necessarily read
└── assets/           optional: templates, boilerplate, images used in output
```

- The directory name equals the `name` field.
- Bundled directories are one level deep: `references/api.md`, not `references/v2/api.md`.
- Refer to bundled files by relative path from the skill directory (`references/api.md`).
- When a skill covers several variants (clouds, frameworks, languages), put one reference per variant and say in the body which one to open:

```
cloud-deploy/
├── SKILL.md            workflow + how to pick a variant
└── references/
    ├── aws.md
    ├── gcp.md
    └── azure.md
```

## 2. Front matter fields

| Field | Required | Rule |
| --- | --- | --- |
| `name` | yes | 1-64 characters of `a-z`, `0-9`, `-`; no leading, trailing or double hyphen; equals the directory name |
| `description` | yes | 1-1024 characters; what it does, then when to use it |
| `license` | no | a licence name (`Apache-2.0`) or the name of a bundled licence file |
| `compatibility` | no | at most 500 characters; environment needs such as "Needs python3 and network access" |
| `metadata` | no | a map of string keys to string values (`author`, `version`, `source`) |
| `allowed-tools` | no | tools the skill may use without asking; support varies by client |

Other keys (`version`, `when_to_use`, `tools`) appear in older skills. They are outside the
spec; the validator warns about them. Put a version in `metadata` instead.

Worked example:

```yaml
---
name: release-notes
description: Draft release notes from merged pull requests and commit messages, grouped by feature, fix and breaking change. Use when asked to write release notes, a changelog entry or a "what's new" summary for a version or tag.
license: Apache-2.0
compatibility: Needs git; gh optional for pull request titles
metadata:
  author: platform-team
  version: "1.2"
---
```

## 3. YAML pitfalls

Front matter is YAML. A description that is fine as English can be invalid YAML:

- `: ` (colon followed by space) inside an unquoted value ends the value. `description: Do X. Note: slow` is broken. Rephrase, or wrap the whole value in double quotes.
- ` #` inside an unquoted value starts a comment; everything after it is lost.
- A value starting with `"`, `'`, `[`, `{`, `*`, `&`, `!`, `|`, `>`, `%`, `@` or a backtick is read as special syntax. Start with a word.
- `version: 1.2` is a number, not a string; quote it (`"1.2"`) inside `metadata`.
- Keep the description on one line. Folded blocks (`description: >` with indented lines) are valid YAML, but some tools only read the first line.
- The upstream validator rejects `<` and `>` in the description. Use `[placeholder]` in examples.

## 4. Progressive disclosure and sizes

| Level | Loaded | Size |
| --- | --- | --- |
| Metadata | always, for every skill | about 100 words |
| Body | when the skill is chosen | under 500 lines, about 5000 tokens |
| Bundled files | when the body points at them | any size; scripts need not be read at all |

- Keep only the procedure and the most common cases in the body. Move detail, long examples, API references and edge cases to `references/`.
- Tell the model when to open each reference ("open `references/aws.md` when deploying to AWS").
- Give a reference over 300 lines a table of contents; for a very large one, name the headings or `Grep` patterns to jump to.
- Information lives in one place: body or reference, not both.

## 5. How Agentaus sees a skill

- **Listing:** the skill appears as `- name: description` in the list of skills available to the `Skill` tool.
- **Routing line:** the bridge also writes `If <clause> → Skill with skill: "<name>"`. The clause comes from the description: the text after the first `Use when` (also matched: `Use this skill when`, `Use it when`, `Use if`, `Triggers on`, and a `;` or `—` followed by `when`), up to the first full stop followed by whitespace, cut at 230 characters with `…`. With no match, the line reads "the task needs: [first sentence]", which rarely fires.
- **Project skills** (`.claude/skills/`) may be injected whole into the system prompt when a gate matches; the injected body is cut at 9000 characters, and at most two skills are injected per turn.
- **Library skills** (`agentaus_bridge/skill_library/`) are listed by the bridge and served when `Skill` names them, with a 40000-character body cap. The served text starts with the skill's base directory, and bundled files are opened with `agentaus_zoom` on their absolute path.
- A running bridge caches skills by directory modification time: adding or renaming a skill is picked up; editing a body needs a bridge restart.

These values come from `agentaus_bridge/skills.py` and `agentaus_bridge/augment.py` and may change.

## 6. Checklist

- [ ] `SKILL.md` starts with `---`, has `name` and `description`, and ends the block with `---`.
- [ ] `name` matches the directory and the naming rule.
- [ ] Description: what it does, then one `Use when` sentence under 230 characters with real user phrases, file types and situations.
- [ ] No YAML pitfalls from section 3.
- [ ] Body: numbered imperative steps, stopping criteria, `If X → do Y` examples, a "Done when" list.
- [ ] Every bundled file mentioned in the body exists, and each says when to open it.
- [ ] Scripts run as documented and use only what `compatibility` declares.
- [ ] `python3 scripts/validate_skill.py <dir>` reports no `FAIL`.
