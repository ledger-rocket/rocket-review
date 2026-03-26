# rocket-review

CLI tool to get GPT review of plans, code, or diffs. Uses Codex CLI as the backend so the reviewer can navigate your project, read related files, and give context-aware feedback.

Command: **`rr`**

## Install

```bash
pipx install ~/Projects/rocket-review
```

Requires Python 3.13+, [Codex CLI](https://github.com/openai/codex), and [gh CLI](https://cli.github.com) (for `--pr`).

If Codex is not installed, `rr` will error with instructions. Use `--api` to explicitly opt into direct API mode (requires `OPENAI_API_KEY`).

### API key setup (for `--api` mode only)

Set `OPENAI_API_KEY` in your environment or in a `.env` file (checked in cwd then home dir):

```bash
export OPENAI_API_KEY="sk-..."
# or
echo 'OPENAI_API_KEY=sk-...' >> ~/.env
```

## Usage

```bash
# Review a plan file (Codex reads related project files for context)
rr plan.md

# Review code files
rr src/auth.py src/models.py

# Review git diff (Codex runs git diff and inspects context itself)
rr --diff

# Review staged changes only
rr --staged

# Review a specific commit
rr --commit abc1234

# Review a GitHub PR (by number, URL, or branch)
rr --pr 123
rr --pr https://github.com/org/repo/pull/123
rr --pr feature-branch

# Pipe anything
git diff HEAD~3 | rr

# Pick a different model
rr plan.md --model gpt-5.4-mini
rr plan.md --model gpt-5.3-codex

# Add extra review instructions
rr plan.md --prompt "focus on security implications"

# Force a specific review mode
rr plan.md --mode code

# Use direct API instead of Codex CLI
rr plan.md --api
```

## Backends

**Codex CLI (default)** — Codex runs in read-only sandbox and can navigate the full project: read imports, tests, related files, run `git diff`, etc. This gives much better reviews than sending isolated content to an API.

**API mode (`--api`)** — Direct OpenAI API call. Automatically extracts file paths referenced in the content and includes their contents. Useful when Codex is not installed. Requires `OPENAI_API_KEY` env var or `.env` file.

```bash
# Force API mode
rr plan.md --api
```

## Review modes

| Mode | Auto-detected when | Focus |
|------|-------------------|-------|
| `plan` | Input is `.md`, `.txt`, or `.plan` files | Completeness, risks, ordering, pragmatism |
| `code` | Input is source code files | Security, performance, correctness, maintainability |
| `diff` | Using `--diff`, `--staged`, `--commit`, `--pr`, or stdin pipe | Bugs introduced, missing changes, contract breaks |

Override with `--mode plan|code|diff`.

## Project standards (`--llms`)

If your project has an `llms.txt` that links to standards docs, use `--llms` to automatically read it and follow all its markdown links:

```bash
# Reads llms.txt + all docs it references (code_standards.md, code_smells.md, etc.)
rr src/auth.py --llms

# Point to a custom path
rr src/auth.py --llms path/to/llms.txt
```

The reviewer checks compliance with your documented standards and flags deviations.

You can also pass explicit doc files with `--docs` (combinable with `--llms`):

```bash
rr src/auth.py --llms --docs extra_notes.md
```

## Models

Default: `gpt-5.4`. Also available: `gpt-5.4-mini`, `gpt-5.3-codex`.

Short aliases auto-resolve to the latest dated snapshot available on your API key.

## Claude Code integration

Add this to your project's `CLAUDE.md`:

```markdown
## External review

When asked to get a review, or before implementing a complex plan, use `rr` to get a GPT second opinion:

- Review a plan: `rr plan.md --llms`
- Review local changes: `rr --diff --llms`
- Review staged changes: `rr --staged --llms`
- Review a commit: `rr --commit SHA --llms`
- Review a PR: `rr --pr 123 --llms`
- Review specific files: `rr src/auth.py --llms`
- Use 600000ms timeout for Bash calls to `rr` (Codex needs time to explore the project)

Always pass `--llms` if the project has an llms.txt so the reviewer checks project standards.
After getting the review, summarize key findings and address them before proceeding.
```
