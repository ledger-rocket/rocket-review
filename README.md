# rocket-review

[![CI](https://github.com/ledger-rocket/rocket-review/actions/workflows/ci.yml/badge.svg)](https://github.com/ledger-rocket/rocket-review/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

**`rr` — a second opinion on your code, from a model that didn't write it.**

One small CLI that sends your plan, diff, commit, or PR to an agentic reviewer
(Codex CLI, Claude Code, or opencode) that explores your project before judging —
then gives you prose or structured JSON you can gate CI on.

Unlike editor-bound plugins (e.g. OpenAI's codex-plugin-cc, which does GPT reviews
but only inside Claude Code), `rr` is a standalone CLI: call it from any shell, any
editor, any agent, or CI.

## What a review looks like

Reviewing an uncommitted diff that quietly breaks a billing invariant
(`rr --diff`, abridged):

```
[HIGH] billing.py — `split_evenly` no longer preserves the input total
> The documented contract says the returned list "always sums to exactly
> `total_cents`," but the new implementation rounds one share and repeats it.
> For inputs like `split_evenly(100, 3)` it returns `[33, 33, 33]`, summing to 99.
> Suggested fix:
    base = total_cents // n
    parts = [base] * n
    for i in range(total_cents - base * n):
        parts[i] += 1
    return parts

- Change Assessment: Do not merge
- Top Issues: The changed implementation loses cents and breaks the documented
  remainder allocation contract.
```

The reviewer read the function's docstring and reasoned about its contract before
flagging the regression — not a pattern match on the diff.

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
- or none of the above: `--backend api` (or the `--api` shorthand) calls the OpenAI API directly (`OPENAI_API_KEY`)

`--pr` also needs the [gh CLI](https://cli.github.com). Not on PyPI yet — install from Git as above.

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
rr --diff --backend opencode:ollama/qwen3      # fully local (opencode → Ollama)
rr --diff --backend codex,claude               # both, side by side
rr --diff --backend codex:gpt-5.5,claude:claude-opus-4-8
```

The CLI backends (Codex, Claude, opencode) run agentically in read-only mode: they
navigate your project — imports, tests, related files — before judging. That context
is what makes the review worth reading. The `api` backend is the exception — it calls
the OpenAI API directly on the supplied content plus any files it references, without
navigating your project.

### Structured output

```bash
rr --diff --json | jq '.findings[] | {severity, title, backend}'
rr --staged --json --fail-on high && git commit   # block the commit on high+ findings
```

Every finding carries `severity, title, file, line, why, fix, backend, model`.
The envelope leads with a `summary` block — `findings_total`, per-severity counts
(explicit zeros for absent severities), `worst_severity`, per-backend verdicts, and
the `gate` result when `--fail-on` is set — so an agent gets the counts and the
gate answer without parsing the findings array. Backend output over 4000 chars is
spilled to a temp file, with `raw` holding a truncation marker and `raw_file` its
path; pass `--full` to inline the untruncated output instead. Parse failures and
backend errors fail the gate closed.

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
(like `llms.txt`) pulls in everything it references. Bare `--docs` errors if none of
`llms.txt` / `AGENTS.md` / `CLAUDE.md` exist in the current directory — pass explicit
paths when your standards live elsewhere. `--llms [PATH]` is kept as a compatibility
alias for `--docs [PATH]`, defaulting to `llms.txt`.

## How it works

`rr` assembles a review prompt — a mode-specific rubric, your standards docs, and
the plan or diff under review — and hands it to an agentic CLI (Codex, Claude Code,
or opencode) running read-only inside your project. Because the reviewer runs
*in* your checkout, it can open imports, tests, and related files to understand
context before it judges, rather than reasoning from the diff alone. It then
returns either prose or a parsed findings envelope (`--json`). There is no
rocket-review server in the loop: the only thing that leaves your machine is the
review request — the diff or plan, your standards docs, and any files the reviewer
opens — sent to whichever backend and provider you chose. Point `rr` at a local
opencode/Ollama model to keep everything on your machine.

## Requirements

- **Python** ≥ 3.13
- **OS** — macOS or Linux
- **A backend CLI**, installed and authenticated — you only need the one(s) you use:
  - `codex` — [Codex CLI](https://github.com/openai/codex), signed in with your ChatGPT/OpenAI account
  - `claude` — [Claude Code](https://claude.com/claude-code), on a Claude subscription or API key
  - `opencode` — [opencode](https://opencode.ai), configured for any provider (including a local Ollama model)
  - `api` — no CLI; set `OPENAI_API_KEY` and `rr` calls the OpenAI API directly
- `gh` CLI, authenticated, for `--pr`

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
- Exit codes: 0 no gate tripped · 1 operational error (or every backend failed) · 2 findings at/above `--fail-on`. A partial backend failure warns on stderr but still exits 0 — gate CI with `--json --fail-on` to fail closed.

## Contributing

Issues and PRs are welcome. To set up a dev environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q      # run the tests
.venv/bin/ruff check .   # lint
```

Please run the tests and the linter before opening a PR.

## License

Apache-2.0 licensed. See [LICENSE](LICENSE).
