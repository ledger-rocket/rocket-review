# Claude Code integration

Two pieces: a skill that teaches the agent how to run `rr` and triage what it
returns, and a short standing rule for your `CLAUDE.md` that tells it *when*
to reach for a second opinion at all.

## Install the skill

User-level (available in every project):

```bash
mkdir -p ~/.claude/skills
cp -r integrations/claude-code/rr-review ~/.claude/skills/
```

Or project-level, for one repository (commit it so the whole team gets it):

```bash
mkdir -p .claude/skills
cp -r integrations/claude-code/rr-review .claude/skills/
```

## Add the standing rule

Append to your `CLAUDE.md` (user-level `~/.claude/CLAUDE.md`, or the
project's):

```markdown
## Second-opinion review (rr)

`rr` (rocket-review) sends a plan, diff, commit, or PR to an independent
reviewer. Before pushing any non-trivial change, run
`rr --diff --backend codex` (or `--commit`/`--pr` as fits — the codex backend
keeps the review cross-vendor, since the built-in diff default is claude) and
address the findings; review plans the same way before building
(`rr plan.md`). Skip it for trivial changes — formatting, doc typos,
mechanical renames. If the project has a standards doc (`llms.txt`,
`AGENTS.md`, `CLAUDE.md`), add `--docs` so the review checks the project's
own rules. Verify each finding against the code before fixing it — and before
dismissing it; the bar is the same in both directions. Use the `rr-review`
skill for the details.
```

## Why the rule is shaped this way

The skill carries the *how* — target auto-detection, flags, timeouts, findings
triage — but a skill only fires when the agent thinks of reviewing at all. The
standing rule makes "get a second opinion before pushing" part of the default
pipeline rather than something the user has to remember to ask for. The
triage sentence is there because the failure modes are symmetric: blindly
applying every finding overfits the code to a reviewer's edge cases, and
blindly dismissing findings makes the review theater.

`rr` composes with any workflow that produces diffs. If you delegate
implementation with
[`rocket-build`](https://github.com/ledger-rocket/rocket-build), the pair
share envelope conventions and exit-code semantics by design.
