# rocket-review

[![CI](https://github.com/ledger-rocket/rocket-review/actions/workflows/ci.yml/badge.svg)](https://github.com/ledger-rocket/rocket-review/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/rocket-review)](https://pypi.org/project/rocket-review/)

**`rr` — a second opinion on your code, from a model that didn't write it.**

> **v0.1 — experimental.** Interfaces and flags may change. Read
> [Security & data flow](#security--data-flow) before pointing it at anything sensitive.

One small CLI that sends your plan, diff, commit, or PR to an agentic reviewer
(Codex CLI, Claude Code, or opencode) that explores your project before judging —
then gives you prose or structured JSON you can gate CI on.

Single-model local review is built into the vendor CLIs now (`codex review`,
Claude Code's `/code-review`) — `rr` exists for what a single vendor can't give
you: a second opinion from a *different* vendor's model, in one command, from any
shell, editor, agent, or CI.

`rr` is deliberately **not a PR bot**. It reviews before you push — the point is
that the issues get fixed before a PR exists. It posts nothing anywhere; if you
want PR comments, pipe the `--json` envelope into whatever posts them.

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

- **Cross-vendor review** — the model that wrote the code shouldn't be the only one
  grading it: a model reviewing its own output inherits its own blind spots, and a
  vendor's built-in reviewer is always the same family that wrote the code. Implement
  with Claude, review with GPT; implement with Codex, review with Claude; or
  `--backend codex,claude` fans out to both and shows you where they disagree.
- **Plans are reviewable too** — `rr plan.md` stress-tests a design doc *before*
  you build it. Most review tools only understand diffs.
- **Standards-aware** — `--docs` auto-discovers `llms.txt`, `AGENTS.md`, or
  `CLAUDE.md` (or takes explicit paths) and the reviewer flags deviations from
  *your* documented rules, not generic style opinions.
- **CI-gateable** — `--json --fail-on high` exits 2 when a high-severity finding
  lands. Pipe the envelope to `jq` or your bot of choice.

## Install

```bash
pipx install rocket-review
```

Or the latest from source:

```bash
pipx install git+https://github.com/ledger-rocket/rocket-review.git
```

Requires Python 3.13+ and at least one backend:

- [Codex CLI](https://github.com/openai/codex) (default backend)
- [Claude Code](https://claude.com/claude-code) (`--backend claude`)
- [opencode](https://opencode.ai) (`--backend opencode` — any provider, including local models; **experimental**, see below)
- or none of the above: `--backend api` (or the `--api` shorthand) calls the OpenAI API directly — set `OPENAI_API_KEY` and install the SDK extra: `pipx install 'rocket-review[api]'` (or `pipx inject rocket-review openai` into an existing install)

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
rr --diff --backend opencode:ollama/qwen3      # fully local (opencode → Ollama)
rr --diff --backend codex,claude               # both, side by side
rr --diff --backend codex:gpt-5.6-sol,claude:claude-opus-4-8
rr --diff --effort high                        # more reasoning effort (per-backend flag)
```

Model names: the codex backend passes no `-m`, so it honors your codex default
(set `model` in `~/.codex/config.toml`). On ChatGPT plans use `gpt-5.6-sol`, the
ChatGPT-account-accessible 5.6 variant — codex signed into a ChatGPT account rejects
the bare `gpt-5.6` alias and `gpt-5.6-codex`. The `--backend api` path (API-key auth)
defaults to `gpt-5.6-terra` — balanced cost/quality. Use `--model gpt-5.6-sol` for
max quality (flagship pricing) or `gpt-5.6-luna` for the cheapest tier. rr always
uses explicit, suffixed model names and never relies on the bare `gpt-5.6` alias,
which points at the flagship today but can be remapped by OpenAI.
Use `--effort` to set reasoning effort; values differ per backend (codex/api accept
e.g. `minimal|low|medium|high`, claude `low|medium|high|xhigh|max`) and an unsupported
value fails loudly at the backend. opencode has no effort flag, so `--effort` errors
if opencode is among the selected backends. Heavy `--effort high` reviews — especially
on reasoning models — can outrun the default 900s (15 min) subprocess timeout; raise it
with `--timeout 1800`.

The Codex and Claude backends run agentically in read-only mode: they navigate your
project — imports, tests, related files — before judging. That context is what makes the
review worth reading. The `api` backend is the exception — it calls the OpenAI API
directly on the supplied content plus any files it references, without navigating your
project.

> **opencode is experimental.** The integration works, but end-to-end review
> reliability depends on the provider you have configured, and non-interactive
> `opencode run` can restrict the read-only `plan` agent's tools. `rr` materializes the
> diff and feeds the prompt to opencode on stdin so it always reviews the real change,
> but for a gated CI check prefer `codex` or `claude`. The local-model value prop stands
> — point opencode at Ollama to keep everything on your machine — just verify its output
> before trusting it as a gate.

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
truncated inline — `raw` keeps the head plus a marker naming the full length — so the
envelope stays bounded and no review text (which may quote proprietary code) is written
to disk; pass `--full` to inline the untruncated output instead. Parse failures and
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

## Security & data flow

`rr` runs the reviewer in a **read-only sandbox** (no writes to your files), but
read-only is not the same as safe:

- Your code leaves your machine. Each review sends the diff/plan, your standards
  docs, and any files the reviewer opens to **that backend's provider** — codex/api →
  OpenAI, claude → Anthropic, opencode → whichever provider you configured (point it
  at a local Ollama model to keep everything on your machine).
- Read-only stops *writes*, not *reads*. An agent can still read any secret your shell
  can (`.env`, `~/.aws`, tokens) and send it upstream.
- Untrusted input can prompt-inject the reviewer — a hostile PR body, diff, comment,
  or `AGENTS.md` can try to steer an agentic backend. Be especially careful with `--pr`
  on a dev machine.

**Don't run agentic backends against untrusted repos or PRs on a machine where readable
secrets exist.** See [SECURITY.md](SECURITY.md) for the full threat model and how to
report a vulnerability.

## Requirements

- **Python** ≥ 3.13
- **OS** — macOS or Linux
- **A backend CLI**, installed and authenticated — you only need the one(s) you use:
  - `codex` — [Codex CLI](https://github.com/openai/codex), signed in with your ChatGPT/OpenAI account
  - `claude` — [Claude Code](https://claude.com/claude-code), on a Claude subscription or API key. Needs a version supporting `--permission-mode manual` (Claude Code 2.1.x+); older CLIs fail the review closed with a usage error. Check with `claude --help | grep -A3 permission-mode`.
  - `opencode` — [opencode](https://opencode.ai), configured for any provider (including a local Ollama model)
  - `api` — no CLI, but needs the OpenAI SDK (`pipx install 'rocket-review[api]'`, or `pipx inject rocket-review openai`); set `OPENAI_API_KEY` and `rr` calls the OpenAI API directly
- `gh` CLI, authenticated, for `--pr`

## Agent integration

Drop into your `CLAUDE.md` / `AGENTS.md`:

```markdown
Before pushing non-trivial changes, run `rr --diff --docs` and address the findings.
For plans, run `rr plan.md --docs` before implementing. Use a 900000ms timeout.
```

## Notes

- Every backend runs in a read-only sandbox on your project — **no writes**: Codex
  runs with `-s read-only`, Claude Code with a read-only tool allowlist under
  `--permission-mode manual`, and opencode with its built-in read-only `plan` agent
  (edit/write denied at the tool level). Read-only stops writes; it does not stop the
  agent *reading* readable secrets and sending them to the backend's provider — see
  [Security & data flow](#security--data-flow).
- `--fail-on` requires `--json`.
- Exit codes: 0 no gate tripped · 1 operational error (or every backend failed) · 2 findings at/above `--fail-on`. A partial backend failure warns on stderr but still exits 0 — gate CI with `--json --fail-on` to fail closed.

## Contributing

Issues and PRs are welcome. To set up a dev environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q                  # run the tests
.venv/bin/ruff check .               # lint
.venv/bin/mypy rocket_review/        # type-check
.venv/bin/yamllint .                 # yaml lint
```

CI gates all four plus a package build — run them before opening a PR.

## License

Apache-2.0 licensed. See [LICENSE](LICENSE).
