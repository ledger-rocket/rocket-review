# Security Policy

`rr` (rocket-review) is **v0.1, experimental**. This document describes how to report
a vulnerability and the threat model you accept when you run it.

## Reporting a vulnerability

Please report security issues through GitHub **private vulnerability reporting**:

1. Go to the repository's **Security** tab → **Report a vulnerability** (this opens a
   private advisory visible only to you and the maintainers).
2. If private reporting is not yet enabled on your fork/clone, a maintainer can turn it
   on under **Settings → Code security → Private vulnerability reporting**.

Do **not** open a public issue or PR for a vulnerability — the report route above keeps
the details private until a fix is available. Include a description, reproduction steps,
and the impact you observed.

## Threat model

`rr` hands your code to an agentic reviewer running in a **read-only sandbox**. That
sandbox is a real boundary, but a narrow one — understand exactly what it does and does
not protect.

### What read-only guarantees: no writes

Each backend is constrained so the reviewer cannot modify your files:

- **codex** — `-s read-only` (OS-level sandbox).
- **claude** — a read-only tool allowlist (`Read Glob Grep`) under
  `--permission-mode manual`. Two things are load-bearing here:
  - `manual` mode: in headless (`-p`) mode an allowlist *alone* is additive — Claude
    Code auto-approves unlisted tools, including `Write`/`Edit` and arbitrary `Bash`.
    `manual` flips the default to deny, so only allow-listed tools run.
  - No wildcard git. `git diff`, `git show`, and `git log` all accept `--output=<file>`,
    which git opens itself (bypassing Claude Code's shell-redirect guard), so a rule
    like `Bash(git diff:*)` would be a write vector. Instead, only the single exact git
    command needed to view the change under review is allow-listed (e.g.
    `Bash(git diff HEAD)`) — with no `:*`, no write flag can be appended.
- **opencode** — the built-in read-only `plan` agent, with edit/write denied at the
  tool level.

### What read-only does NOT protect against

Read-only stops the agent from **writing**. It does nothing about **reading and
exfiltrating**:

- **Secret reads.** The reviewer runs with your shell's privileges and can read
  anything you can — `.env`, `~/.aws/credentials`, `~/.ssh`, API tokens, any readable
  source — and include it in the request sent upstream. Read-only does not stop this.
- **Prompt injection.** The content under review is untrusted input. A hostile PR body,
  diff, source comment, review comment, or an `AGENTS.md` / `CLAUDE.md` in the target
  repo can attempt to steer an agentic backend into reading and leaking secrets or
  emitting misleading findings. This risk is highest with `--pr` run on a developer
  machine, where real credentials are present.
- **Repo-supplied configuration.** A `.rocket-review.toml` in the repo you run `rr` from
  sets that project's defaults. It can only select backends you have installed, and the
  docs it names are confined to the repo and excluded from `.git/` — a doc is copied into
  the prompt verbatim, so without that a clone could have your local git config,
  credentialed remote URLs and all, sent upstream. What it *can* decide is worth knowing,
  because it is input the repo's author controls, and on a fork's PR that is the
  contributor:
  - **Where your code goes** — `[backends]` picks the backend and therefore the provider.
  - **What it costs** — `[models]` can name an expensive model and `timeout` a long run,
    on your account or your CI minutes.
  - **What reviews a gated change** — a PR that only edits the config file can downgrade
    the model your gate depends on. Pass `--no-config`, or an explicit `--backend`, in
    gated jobs.

  Read it as you would any tooling config a clone brings with it, or run with
  `--no-config`, which ignores both config files.
- **Confused-deputy across checkouts.** `--repo <other>` reviews a PR from a *different*
  remote repository, but the agent still runs with read access to your **local current
  working directory**. Do not run it from inside an unrelated sensitive checkout — the
  remote PR's instructions plus local read access is the confused-deputy setup.

### Where your code goes

There is no rocket-review server. The only thing that leaves your machine is the review
request — the diff/plan, your standards docs, and any files the reviewer opens — sent to
**the provider behind the backend you chose**:

- **codex** → OpenAI (via your ChatGPT/OpenAI account).
- **api** → OpenAI (via `OPENAI_API_KEY`).
- **claude** → Anthropic.
- **opencode** → whichever provider you configured — including a **local Ollama model,
  which keeps everything on your machine**.

Choose the backend accordingly: if the code must not leave your machine, use opencode
pointed at a local model.

## Recommended practice

> **Do not run agentic backends against untrusted repos or PRs on a machine where
> readable secrets exist.**

- Review untrusted PRs in a throwaway/isolated environment (a container or CI runner)
  with no standing credentials, not on your primary dev machine.
- Prefer a local model (opencode → Ollama) for code that must not leave your machine.
- Treat every finding as advisory: a prompt-injected reviewer can lie about what it
  found. Read the diff yourself before acting on a verdict.
