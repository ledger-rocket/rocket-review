---
name: rr-review
description: Get a cross-vendor second-opinion review via rr (rocket-review) on a plan, diff, commit, PR, or file — and triage the findings. Use when the user says "rr", "review this", "get a review", "second opinion", or before pushing any non-trivial change or building from a non-trivial plan.
---

# Second-opinion review via rr

`rr` sends a plan, diff, commit, PR, or file to an agentic reviewer and
returns prose or a JSON findings envelope. The agentic backends run read-only
inside the project, so they open imports, tests, and related files before
judging (the `api` backend is the exception — it sees only what is sent).
Backend defaults are per-mode: plans go to codex, code and diffs to claude —
and since this agent *is* Claude, pass `--backend codex` (or
`--backend codex,claude` for both opinions) on code, diff, commit, and PR
reviews so the second opinion actually comes from a different vendor. Full
flag reference: `rr --help`; the README in the rocket-review repository covers
backends, config files, and the security model.

## When to run

- **Before pushing any non-trivial change** — `rr --diff --backend codex` (or
  `--staged`, `--commit`, `--pr`, whichever fits) catches issues before they
  leave the machine. Skip it for trivial changes: formatting, doc typos,
  mechanical renames, one-line config bumps.
- **Plans too** — a review of a plan or design doc *before* building is worth
  at least as much as a review of the code after: `rr plan.md --docs`.
- If the project has a standards doc (`llms.txt`, `AGENTS.md`, `CLAUDE.md`),
  pass `--docs` so the reviewer checks compliance with the project's own
  documented rules, not just generic ones.

## Auto-detect what to review

When asked for a review without a target ("rr", "review this", "get a
review"):

1. Check `git status` and recent conversation context.
2. If the target is **obvious**, proceed:
   - just created or discussed a PR → `rr --pr <number>`
   - uncommitted changes exist → `rr --diff`
   - clean tree, just committed → `rr --commit HEAD`
   - just wrote a plan file → `rr <plan-file>`
3. If it is **ambiguous** — uncommitted changes AND a recent PR, or several
   plausible targets — ask which one, rather than guessing.

Whatever the target, carry the standing flags from the sections below:
`--backend codex` on everything but plans, and `--docs` when the project has a
standards doc.

## Commands

```bash
rr plan.md --docs                          # review a plan against project standards
rr --diff --docs --backend codex           # review uncommitted changes to tracked files
rr --staged --docs --backend codex         # review staged changes only
rr --commit SHA --docs --backend codex     # review a specific commit
rr --pr 123 --docs --backend codex         # review a GitHub PR
rr src/auth.py --docs --backend codex      # review specific files
rr --diff --backend codex --prompt "focus on the locking"   # add a specific focus
rr --diff --backend codex,claude           # two opinions, side by side
rr --diff --backend codex --json --fail-on high             # machine-readable, gateable
```

- `--backend codex` keeps the diff-shaped reviews cross-vendor (their built-in
  default is claude). A project `.rocket-review.toml` with
  `[backends]` `code = "codex"` / `diff = "codex"` makes that the default, and
  then the flag can be dropped.
- `--diff` is `git diff HEAD`, so untracked new files are invisible to it —
  `git add -N <paths>` first, or review them directly (`rr <paths>`).
- `--docs` with no path auto-discovers `llms.txt` / `AGENTS.md` / `CLAUDE.md`
  in the **current directory** and errors if none is there — run from the repo
  root, name paths explicitly, or drop the flag.

## Timeouts

The agentic backends explore the project before answering; give them room.
`rr`'s own subprocess timeout defaults to 900s — raise it with
`--timeout 1800` for high-effort reviews. Mind the harness's own limit: Claude
Code's Bash tool caps its `timeout` parameter at 600000ms (10 min) by default,
*below* rr's 900s, and a Bash call killed mid-review loses the whole result.
Either raise the cap (`BASH_MAX_TIMEOUT_MS` in the settings `env`) and pass a
Bash timeout that covers rr's, or pass `--timeout 540` so rr finishes under
the harness limit.

## Handling findings

Verify each finding against the actual code before acting on it — and before
dismissing it. Most findings are real and worth fixing; a minority are false
positives or fixate on rare edge cases, especially after the first round or
two. The bar is the same in both directions: confirm a finding is real before
fixing it, and confirm it is actually spurious before dropping it — don't wave
one away just because verifying takes work. What you don't do is overcomplicate
the code to defend against low-probability edge cases that aren't worth the
added complexity.

Then report: what the review found, what is being fixed, and what is
deliberately skipped and why.
