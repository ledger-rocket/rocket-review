# rocket-review

[![CI](https://github.com/ledger-rocket/rocket-review/actions/workflows/ci.yml/badge.svg)](https://github.com/ledger-rocket/rocket-review/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/rocket-review)](https://pypi.org/project/rocket-review/)

**`rr` — a second opinion on your code, from a model that didn't write it.**

> **Pre-1.0.** Interfaces and flags may change between minor versions. Read
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

Or with Homebrew, which brings its own Python:

```bash
brew install ledger-rocket/tap/rocket-review
```

Or the latest from source:

```bash
pipx install git+https://github.com/ledger-rocket/rocket-review.git
```

Requires Python 3.13+ — the Homebrew formula vendors it, so that floor only
applies to the pipx and from-source routes. Either way you need at least one
backend, which no install method provides:

- [Codex CLI](https://github.com/openai/codex) (default for plan reviews)
- [Claude Code](https://claude.com/claude-code) (default for code and diff reviews)
- [opencode](https://opencode.ai) (`--backend opencode` — any provider, including local models; **experimental**, see below)
- or none of the above — `--backend api` (shorthand `--api`) calls the OpenAI API
  directly, with no agent CLI. It needs three things:
  - `OPENAI_API_KEY` in your environment
  - the SDK extra: `pipx install 'rocket-review[api]'`, or `pipx inject rocket-review
    openai` into an existing install
  - a pipx install — the Homebrew formula ships the base package only

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
rr --diff --no-config             # ignore the config files (hermetic run)
rr --version                      # print the installed version
```

### Default backends by mode

With no `--backend`, the reviewer follows the review mode:

| Mode | Default | Why |
| --- | --- | --- |
| `plan` | `codex` | focused plan reviews, the fastest runs, schema-enforced JSON |
| `code` | `claude` | deeper findings on source files, with fewer false alarms |
| `diff` | `claude` | deeper findings on changes, with fewer false alarms |

These come from measuring the backends against each other on rocket-review's own
eval corpus, not from a preference between vendors — and they are only defaults.

- **Overriding** — an explicit `--backend` always wins, in any mode, single
  (`--backend codex`) or as a list (`--backend codex,claude`). `--mode` is applied
  before the backend is chosen, so `rr plan.md --mode code` reviews as code *and*
  uses the code default. To change the table itself for a project or for yourself,
  set `[backends]` in a [config file](#config-file).
- **Missing backend** — if the mode's default isn't available, `rr` uses the next
  available one and says so in a single line on stderr; pass `--backend` to choose
  explicitly and silence it. The order is the other agentic CLI first, then
  `opencode` (skipped when `--effort` is set, which it doesn't support), then `api`
  — and only when both `OPENAI_API_KEY` and the SDK extra are present, since a
  substitute that can't run would defeat the point. The fallback is never silent. If
  nothing is available, `rr` errors with the install hint for the backend the mode
  wanted.
- **Two opinions at once** — `--backend codex,claude` runs both and prints each
  review under its own heading. Some teams do this on pre-merge changes and read the
  disagreements; others keep the single per-mode default and spend the time
  elsewhere. Both are reasonable — it is a choice about your pipeline, not a
  recommendation.

### Pick your reviewer

```bash
rr --diff --backend claude                     # Claude reviews (read-only sandbox)
rr --diff --backend opencode:ollama/qwen3      # fully local (opencode → Ollama)
rr --diff --backend codex,claude               # both, side by side
rr --diff --backend codex:gpt-5.6-sol,claude:claude-opus-4-8
rr --diff --effort high                        # more reasoning effort (per-backend flag)
```

**Model names.**

- **codex** passes no `-m`, so it honors your own codex default (`model` in
  `~/.codex/config.toml`). On ChatGPT plans use `gpt-5.6-sol`, the
  ChatGPT-account-accessible 5.6 variant — codex signed into a ChatGPT account rejects
  both the bare `gpt-5.6` alias and `gpt-5.6-codex`.
- **api** (API-key auth) defaults to `gpt-5.6-terra`, balanced on cost and quality.
  `--model gpt-5.6-sol` buys max quality at flagship pricing; `gpt-5.6-luna` is the
  cheapest tier.
- rr always names models explicitly, with the suffix, and never relies on the bare
  `gpt-5.6` alias — it points at the flagship today but OpenAI can remap it.

**Reasoning effort and timeouts.**

- `--effort` sets reasoning effort, and the accepted values differ by backend: codex and
  api take `minimal|low|medium|high`, claude takes `low|medium|high|xhigh|max`. An
  unsupported value fails loudly at the backend rather than being silently ignored.
- opencode has no effort flag at all, so `--effort` errors if opencode is among the
  selected backends.
- Heavy `--effort high` reviews — especially on reasoning models — can outrun the default
  900s (15 min) subprocess timeout. Raise it with `--timeout 1800`.

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

- **Findings** each carry `severity, title, file, line, why, fix, backend, model`.
- **A `summary` block leads the envelope** — `findings_total`, per-severity counts (with
  explicit zeros for absent severities), `worst_severity`, per-backend verdicts, and the
  `gate` result when `--fail-on` is set. An agent gets the counts and the gate answer
  without parsing the findings array.
- **`schema_version`** (currently the string `"1"`) tags the envelope shape. It bumps
  only on a breaking change — a field removed, or a type or meaning changed — so new
  fields can appear without one. Match it exactly against versions you know, treat
  anything else as unsupported, and ignore keys you don't recognise. No numeric ordering
  is implied, which is why it is a string.
- **Long output is truncated** at 4000 chars: `raw` keeps the head plus a marker naming
  the full length. This bounds the envelope and keeps review text — which may quote
  proprietary code — off disk. `--full` inlines the untruncated output instead.
- **Failures fail the gate closed**, both parse failures and backend errors.

## Review modes

- `plan` — auto-detected for `.md`/`.txt`/`.plan` files: completeness, ordering, risks, over-engineering.
- `code` — source files: correctness, security, performance, maintainability.
- `diff` — for `--diff`/`--staged`/`--commit`/`--pr`/stdin: bugs introduced, missing changes, contract breaks.

Override with `--mode`, add focus with `--prompt "check the locking"`. The mode also
picks the reviewer — see [Default backends by mode](#default-backends-by-mode).

## Project standards (`--docs`)

Point the reviewer at your project's standards docs — it flags deviations from
*your* documented rules:

```bash
rr src/auth.py --docs                     # auto-discovers llms.txt / AGENTS.md / CLAUDE.md
rr src/auth.py --docs docs/standards.md docs/smells.md
```

Relative markdown links inside the docs are followed one level, so an index file
(like `llms.txt`) pulls in everything it references.

**When you ask for docs and there are none, that is an error — except as a standing
preference.** The three ways to ask differ only in that:

- `--docs` (bare) — errors if none of `llms.txt` / `AGENTS.md` / `CLAUDE.md` is in the
  current directory. Pass explicit paths when your standards live elsewhere.
- `--llms [PATH]` — a compatibility alias for `--docs [PATH]`, identical in both forms:
  bare it takes the repository's `llms.txt` and errors if there isn't one; with a path it
  reads what you name.
- `docs = true` in a [config file](#config-file) — a standing preference rather than a
  request, so it stays **silent** when a project has no standards doc.

Inside a git repository, an auto-discovered doc and every link followed out of any doc
must be a file the repository tracks — the repo, not you, chose those, and a standards
doc is copied into the prompt verbatim. Name a path directly (`--docs CLAUDE.md`) to
read one the repository does not carry. The full rule is under
[What rr will read as a standards doc](#what-rr-will-read-as-a-standards-doc).

## Config file

Two optional TOML files hold the defaults you would otherwise retype:

- **Project** — `.rocket-review.toml`, found by walking up from the current directory
  to the git root (and no further; outside a repo only the current directory is read).
- **User** — `~/.config/rocket-review/config.toml`, or `$XDG_CONFIG_HOME/rocket-review/config.toml`.

**Precedence: CLI flag > project file > user file > built-in default**, settled per
key. A project file that sets only `[backends]` leaves your user file's `timeout` in
force, and the same holds inside `[backends]` and `[models]`.

```toml
timeout = 1800          # --timeout
effort = "high"         # --effort
fail_on = "high"        # --fail-on (needs json = true, exactly as the flag needs --json)
json = false            # --json
full = false            # --full
docs = true             # --docs with no path (auto-discovery); or a list of paths

[backends]              # per-mode default backend, overriding the built-in table
plan = "codex"
code = "claude"
diff = "claude"
default = "claude"      # optional: covers any mode you leave out above

[models]                # per backend, i.e. what `--backend name:model` pins
codex = "gpt-5.6-sol"
claude = "claude-opus-5"
```

Every key mirrors a flag — a config file changes what `rr` does by default, never what
it can do. What to review (`--diff`, `--pr`, files, `--mode`, `--prompt`) stays on the
command line, where it is visible in the invocation.

Anything else is an error, named rather than ignored: an unknown key or mode, a backend
`rr` doesn't have, a non-integer `timeout`, a `fail_on` that isn't a severity, malformed
TOML (reported with the file and the parse position). Config is validated before any git,
`gh`, or backend work starts.

`--no-config` ignores both files. It is also the way out of a `json = true`,
`full = true`, or `docs` you don't want on this run: `rr` has no `--no-json`-style
inverse flags, so a lower layer can only be turned off by a higher config file
(`json = false` in the project file) or by `--no-config`. Flags always win where they
can say anything at all.

**In a gated CI job, pass `--no-config` (or at least an explicit `--backend`).** The
config a job picks up comes from the branch under test, which on a fork's PR is the
contributor's: `[backends]`/`[models]` decide *which model reviews the code*, so a
config change alone can downgrade the reviewer your gate depends on. `--fail-on` is the
one direction that is safe either way — a file can only tighten a gate the job did not
set, never loosen the one it did, since your flag outranks it.

### `docs` in a config file

`docs = true` means "use this project's standards doc if it has one" — `llms.txt` /
`AGENTS.md` / `CLAUDE.md`, and a project without one is not an error the way a typed
`--docs` is. Which project depends on which file asked:

- **Project file** — looks in the directory holding the `.rocket-review.toml`, so
  everyone gets the same standards wherever in the repo they run from.
- **User file** — looks in the checkout you are in (its git root, or the current
  directory outside a repo), never beside `~/.config/rocket-review/config.toml`, which
  is nobody's project.

Explicit `docs` paths are relative to the config file that names them.

### What rr will read as a standards doc

One rule decides every docs path, whether a config named it, discovery found it, or a
markdown link inside another doc points at it:

> **A doc the repository chose is read only if the repository tracks it (at `HEAD`), it
> resolves inside the directory it came from, and it is not inside `.git`. A doc *you*
> name is read as-is.**

"The repository chose it" covers three cases:

- **a path in a project's `.rocket-review.toml`** — that file is repository content, and
  on a fork's PR it is a contributor's;
- **anything auto-discovered** (`docs = true`, or `--docs` / `--llms` with no path) — the
  repo decides which file answers the pattern, whoever asked for the pattern;
- **every markdown link followed out of a doc that resolves inside a repository** — the
  link was written by whoever wrote that doc. This holds even for a doc you named
  yourself: naming `--docs STANDARDS.md` vouches for that file, not for what it points at.

Everything is decided on the *resolved* path, so a tracked symlink is judged by the file
it actually opens — the repository carries the link, not its target. Untracked and ignored
files are yours, not the project's: a `.env`, a key, a private note. A named path that is
refused stops the run and says so; a discovered or linked one is skipped with a warning,
since discovery is a standing "if there is one".

**Outside a git repository** nothing is tracked, so what remains is the directory
confinement and the `.git` guard: a config's docs must stay inside the config's own
directory, and a link inside the doc's.

**What you name is yours** — `--docs PATH`, `--llms PATH`, or a path in your own
`~/.config/rocket-review/config.toml` — and is read wherever it points, including files
no repository tracks. The single exception is `.git`, which `rr` never reads into a
review, as a footgun check.

`--docs` and `--llms` sit on the same boundary in both forms, which is what makes them
the alias this README calls them: bare, each takes what the repository offers; with a
path, each reads what you named.

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
- A repo's `.rocket-review.toml` configures the runs you make inside it — including which
  backend, and therefore which provider, gets your code, and which model at what cost. The
  docs it can name are bounded (see [`docs` in a config file](#docs-in-a-config-file)) and
  it can only select backends you have installed, but it is repo-supplied input: read it
  like any other tooling config a clone brings with it, or use `--no-config`.

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
- `--fail-on` requires `--json` — including when a [config file](#config-file) is what
  set it; the error names the file.
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
