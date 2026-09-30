# Posting the review to a GitHub pull request (optional)

Only do this when the user asked for the review to be posted. Posting sends a message
on the user's behalf, so show the comment text and get a clear yes first.

## Steps

1. Check the PR is still open: `gh pr view <n> --json state,isDraft`. If it closed
   while you were reviewing, tell the user and stop.
2. Get the full commit SHA of the PR head:
   `gh pr view <n> --json headRefOid --jq .headRefOid`. Copy the 40-character value
   into the links as literal text. A link containing `$(git rev-parse HEAD)` will not
   work, because GitHub renders the comment as Markdown and never runs commands.
3. Get the repository as `owner/repo`:
   `gh repo view --json nameWithOwner --jq .nameWithOwner`.
4. Write the comment to a temporary file using the template below. Include only
   Critical and Important findings. Keep it brief. No emojis.
5. Show the user the comment and ask to post it.
6. On a yes: `gh pr comment <n> --body-file <file>`.

## Link format

```text
https://github.com/<owner>/<repo>/blob/<full-40-char-sha>/<path>#L<start>-L<end>
```

- The SHA must be the full 40 characters.
- The repository must be the one being reviewed.
- Put `#` right after the file path.
- Use `L<start>-L<end>`, with at least one line of context before and after the line
  you are commenting on. For a finding on lines 5-6, link `#L4-L7`.

## Comment template

```markdown
### Code review

Found 3 issues:

1. <brief description> (CLAUDE.md says "<quoted rule>")

https://github.com/<owner>/<repo>/blob/<sha>/<path>#L<start>-L<end>

2. <brief description> (bug: <file and code snippet that causes it>)

https://github.com/<owner>/<repo>/blob/<sha>/<path>#L<start>-L<end>

3. ...
```

When nothing survived verification:

```markdown
### Code review

No issues found. Checked for: <the lenses that ran>.
```
