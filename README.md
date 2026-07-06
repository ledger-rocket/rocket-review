# rocket-review

**`rr` — a second opinion on your code, from a model that didn't write it.**

One small CLI that sends your plan, diff, commit, or PR to an agentic reviewer
(Codex CLI, Claude Code, or opencode) that explores your project before judging —
then gives you prose or structured JSON you can gate CI on.

Unlike editor-bound plugins (e.g. OpenAI's codex-plugin-cc, which does GPT reviews
but only inside Claude Code), `rr` is a standalone CLI: call it from any shell, any
editor, any agent, or CI.

## Why

- **Cross-model review** — the model that wrote the code shouldn't be the only one
  grading it. Implement with Claude, review with GPT; implement with Codex, review
  with Claude; or fan out to both and compare.
- **Plans are reviewable too** — `rr plan.md` stress-tests a design doc *before*
  you build it. Most review tools only understand diffs.
- **Standards-aware** — `--docs` auto-discovers `llms.txt`, `AGENTS.md`, or
  `CLAUDE.md` (or takes explicit paths) and the reviewer flags deviations from
  *your* documented rules, not generic style opinions.
- **CI-gateable** — `--json --fail-on high` exits 2 when a high-severity finding
  lands. Pipe the envelope to `jq` or your bot of choice.

## Install

```bash
pipx install git+https://github.com/ledger-rocket/rocket-review.git
```

Requires Python 3.13+ and at least one backend:

- [Codex CLI](https://github.com/openai/codex) (default backend)
- [Claude Code](https://claude.com/claude-code) (`--backend claude`)
- [opencode](https://opencode.ai) (`--backend opencode` — any provider, including local models)
- or none of the above: `--backend api` calls the OpenAI API directly (`OPENAI_API_KEY`)

`--pr` also needs the [gh CLI](https://cli.github.com).

## Usage

```bash
rr plan.md                        # stress-test a plan before building
rr --diff                         # review uncommitted changes
rr --staged                       # review staged changes only
rr --commit abc1234               # review a commit
rr --pr 123                       # review a GitHub PR (number, URL, or branch)
rr --pr 123 --repo acme/api-server  # ...from outside that repo's checkout
git diff HEAD~3 | rr              # pipe anything
rr src/auth.py --docs             # review files against your documented standards
```

### Pick your reviewer

```bash
rr --diff --backend claude                     # Claude reviews (read-only sandbox)
rr --diff --backend opencode:ollama/qwen3      # fully local review
rr --diff --backend codex,claude               # both, side by side
rr --diff --backend codex:gpt-5.5,claude:claude-opus-4-8
```

Backends run agentically in read-only mode: they navigate your project — imports,
tests, related files — before judging. That context is what makes the review
worth reading.

### Structured output

```bash
rr --diff --json | jq '.findings[] | {severity, title, backend}'
rr --staged --json --fail-on high && git commit   # block the commit on high+ findings
```

Every finding carries `severity, title, file, line, why, fix, backend, model`.
Parse failures and backend errors fail the gate closed.

## Review modes

- `plan` — auto-detected for `.md`/`.txt`/`.plan` files: completeness, ordering, risks, over-engineering.
- `code` — source files: correctness, security, performance, maintainability.
- `diff` — for `--diff`/`--staged`/`--commit`/`--pr`/stdin: bugs introduced, missing changes, contract breaks.

Override with `--mode`, add focus with `--prompt "check the locking"`.

## Project standards (`--docs`)

Point the reviewer at your project's standards docs — it flags deviations from
*your* documented rules:

```bash
rr src/auth.py --docs                     # auto-discovers llms.txt / AGENTS.md / CLAUDE.md
rr src/auth.py --docs docs/standards.md docs/smells.md
```

Relative markdown links inside the docs are followed one level, so an index file
(like `llms.txt`) pulls in everything it references. `--llms [PATH]` is kept as a
compatibility alias for `--docs [PATH]`, defaulting to `llms.txt`.

## Agent integration

Drop into your `CLAUDE.md` / `AGENTS.md`:

```markdown
Before pushing non-trivial changes, run `rr --diff --docs` and address the findings.
For plans, run `rr plan.md --docs` before implementing. Use a 900000ms timeout.
```

## Notes

- Every backend is read-only on your project: Codex runs with `-s read-only`,
  Claude Code with a read-only tool allowlist, and opencode with its built-in
  read-only `plan` agent (edit/write denied at the tool level).
- `--fail-on` requires `--json`.
- Exit codes: 0 no gate tripped · 1 operational error · 2 findings at/above `--fail-on`.

MIT licensed.
