---
name: git-commit-and-pr
description: Commit changes with git in the repository's own message style, optionally push and open a pull request with the GitHub CLI, and delete local branches whose remote branch is gone, under fixed safety rules (no force-push to main, no skipped hooks, no committed secrets). Use when asked to commit, "commit this", "commit and push", write a commit message, push a branch, open or create a PR or pull request, or clean up stale, merged or [gone] local branches.
license: Apache-2.0
metadata:
  source: claude-plugins-official/plugins/commit-commands
  adapted-for: agentaus
---

# Git commit, push and pull request

Three tasks share one set of safety rules. Pick the task, then follow its steps in order.

## When to use

| The user asked to... | Do |
| --- | --- |
| commit, "commit this", write a commit message | Task A |
| commit and push, push, open or create a PR | Task A (if anything is uncommitted), then Task B |
| clean up branches, delete merged, stale or `[gone]` branches | Task C |

Do not push or open a PR unless the user asked for it.

## Safety rules (every task)

1. **Never force-push to the default branch** (`main`, `master`, or whatever `origin/HEAD` points to): no `--force`, `-f`, `--force-with-lease`, no `+` refspec. Force-push another branch only if the user asked for that by name.
2. **Never skip hooks**: no `--no-verify`, no `-n` on `git commit`, no `-c core.hooksPath=...`, no `HUSKY=0`. If a hook fails, fix the cause.
3. **Never commit secrets.** Run the check in Task A step 4 before staging.
4. **Stage files by name**: `git add path/one path/two`. Use `git add -A` only after `git status` shows nothing but intended files.
5. **Never amend** unless the user asked. Never amend a commit that has been pushed.
6. **No destructive commands** unless the user asked for that exact thing: `git reset --hard`, `git checkout -- .`, `git restore .`, `git clean -f`, `git push --delete`, `git branch -D` (Task C excepted).
7. Do not change `git config`. Do not use interactive flags (`git rebase -i`, `git add -p`, `git commit` without `-m`/`-F`): they hang.
8. No empty commits. If there is nothing to commit, say so and stop.

## Task A - Commit

1. In one turn, run these as separate `Bash` calls:
   - `git status`
   - `git diff HEAD` (in a repository with no commits yet: `git diff --staged` and `git diff`)
   - `git log -10 --format='%h %s'`
   - `git log -3 --format='%B%n-----'` (whole messages: bodies and trailers)
2. If `git status` shows a merge, rebase or cherry-pick in progress, or unresolved conflicts → stop and report. Do not commit.
3. If there is nothing to commit → say so and stop.
4. **Secret and junk check.** Do not stage a file, and tell the user, if:
   - its name is `.env` or `.env.*` (except `.env.example`), `*.pem`, `*.key`, `*.p12`, `*.keystore`, `id_rsa*`, `credentials.json`, `secrets.*`, or a `.npmrc`/`.pypirc` holding a token;
   - its diff or content matches `BEGIN .*PRIVATE KEY`, `AKIA[0-9A-Z]{16}`, `ghp_`, `github_pat_`, `sk-[A-Za-z0-9]{20,}`, `xox[bap]-`, or `password\s*=\s*['"][^'"]+`.
   - Also leave out build output and clutter: `node_modules/`, `dist/`, `build/`, `__pycache__/`, `.DS_Store`, `*.log`.
5. `Read` each untracked file you will commit (`head -50` for long ones). `git diff` does not show them.
6. Choose the files. If the user named files or a scope → only those. Otherwise → every change except those excluded in step 4.
7. **Copy the message style from the log:**

   | The log shows | Write |
   | --- | --- |
   | `feat: ...`, `fix(api): ...` | Conventional Commits: `type(scope): subject` |
   | `ABC-123 Add ...` | the same ticket prefix if you know the ticket; otherwise leave it out and say so |
   | Capitalised imperative subjects, no prefix | `Add retry to the upload client` |
   | Bodies that explain why | a body too |
   | Trailers (`Signed-off-by:`, `Co-Authored-By:`) | only the trailers your own instructions or the user require |

8. **Draft the message.**
   - Subject: imperative mood, no full stop, about 50-72 characters (match the log). Say what changed and why it matters, not which files.
   - Body, when the reason is not obvious: a blank line after the subject, lines wrapped at 72, explaining why.
   - Unrelated changes → still one commit unless the user asked for several; mention it.
9. **Stage and commit in one `Bash` call.** The quoted heredoc (`<<'EOF'`) stops the shell expanding `$`, backticks or quotes in the message:

   ```bash
   git add src/upload.py tests/test_upload.py && git commit -F - <<'EOF'
   Retry uploads with backoff on 503

   A single 503 from the storage API failed the whole upload.
   Retry up to three times with exponential backoff.
   EOF
   ```

10. Run `git status` and `git log -1 --stat`.
11. Done when `git log -1` shows your message and `git status` no longer lists the committed files.

## Task B - Push and open a pull request

1. Run `git branch --show-current`, `git remote -v` and `gh auth status`. Find the default branch: `gh repo view --json defaultBranchRef -q .defaultBranchRef.name` (fallback: `git symbolic-ref --short refs/remotes/origin/HEAD`).
2. If you are on the default branch or a detached HEAD → create a branch: `git switch -c <name>`. Copy the naming style of `git branch -r` (such as `feat/short-slug`); otherwise use `<type>/<short-slug>`.
3. If anything is uncommitted → do Task A.
4. Look at the whole branch, not only the last commit: `git log --oneline origin/<default>..HEAD` and `git diff --stat origin/<default>...HEAD`.
5. Push: `git push -u origin <branch>`. If rejected → stop and report. Do not force, pull or rebase unless the user says so.
6. If `gh pr view --json url,state` succeeds → a PR already exists and the push updated it. Report its URL and stop.
7. If `.github/pull_request_template.md`, `.github/PULL_REQUEST_TEMPLATE/` or `docs/pull_request_template.md` exists → fill in that template as the body. Otherwise:

   ```bash
   gh pr create --base main --title "Retry uploads with backoff on 503" --body-file - <<'EOF'
   ## Summary
   - <what changed and why; 1-3 bullets covering every commit on the branch>

   ## Test plan
   - [ ] <command you ran, or one the reviewer should run>
   - [ ] <manual check>
   EOF
   ```

   - Replace `main` with the default branch. Title in the repository's commit style.
   - Add `--draft` if the user asked for a draft. Add `Closes #123` if the user named an issue.
   - Add only the attribution lines your own instructions require.
8. Done when `gh pr view --json url -q .url` prints the URL and you have given it to the user.

## Task C - Delete local branches whose remote is gone

A branch is `[gone]` when the remote branch it tracked was deleted, usually after its PR merged.

1. Run `git fetch --prune` so deleted remote branches are noticed.
2. List them:

   ```bash
   git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads | awk '$2 == "[gone]" {print $1}'
   ```

3. If the list is empty → report "no branches to clean up" and stop.
4. Show the list. If the user only asked to see stale branches, stop here.
5. Delete them. This skips the checked-out branch, removes a linked worktree first, keeps any worktree with uncommitted or untracked files, and `git branch -D` prints each branch's last commit (`was <sha>`), so it can be restored with `git branch <name> <sha>`:

   ```bash
   current=$(git branch --show-current)
   here=$(git rev-parse --show-toplevel)
   git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads |
   awk '$2 == "[gone]" {print $1}' |
   while read -r branch; do
     if [ "$branch" = "$current" ]; then echo "SKIP $branch: checked out here"; continue; fi
     wt=$(git worktree list --porcelain | awk -v ref="refs/heads/$branch" '/^worktree /{p=substr($0,10)} $1=="branch" && $2==ref {print p}')
     if [ -n "$wt" ] && [ "$wt" != "$here" ]; then
       if git worktree remove "$wt"; then echo "REMOVED worktree $wt"
       else echo "SKIP $branch: could not remove worktree $wt (uncommitted or untracked files?)"; continue; fi
     fi
     git branch -D "$branch"
   done
   ```

6. Run step 2 again. Done when it prints nothing, or only branches the script reported as `SKIP`.
7. Report the deleted branches with their SHAs, the removed worktrees, and each skipped branch with its reason.

## If this → do that

- If a pre-commit hook fails → the commit did not happen. Read the hook output, fix the cause, `git add` the fixed files, and run the same `git commit` again. Do not `--amend`: that would rewrite the previous commit.
- If a hook reformatted files → `git add` those files and commit again.
- If the secret check matches → do not stage that file (`git restore --staged <file>` if it already is), tell the user, and suggest adding it to `.gitignore`.
- If `gh` is missing or not logged in → push anyway, give the user the pull-request link that `git push` prints, and ask them to run `gh auth login` themselves. Never handle tokens.
- If there is no `origin` remote → stop and ask which remote to use.
- If the user says "commit everything" → still apply step 4 and say what you left out.
- If the branch is behind its remote → report it; do not rebase or merge unless asked.
- If a `[gone]` branch is checked out → say so; the user can switch branches and rerun Task C.
