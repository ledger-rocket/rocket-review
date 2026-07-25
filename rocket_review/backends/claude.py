from rocket_review.backends import base
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.prompts import build_agent_prompt

NAME = "claude"
BINARY = "claude"
INSTALL_HINT = "npm install -g @anthropic-ai/claude-code (https://claude.com/claude-code)"
DEFAULT_MODEL = None  # honor the user's Claude Code default model

# Read-only review sandbox, layer 1: deny by default. --allowedTools ALONE is not
# restrictive — in headless (`-p`) mode Claude Code auto-approves tools that are on
# neither the allow- nor the deny-list, so an unlisted Write/Edit or arbitrary Bash
# runs unprompted. `--permission-mode manual` flips the default to deny: with no TTY
# to grant approval, only allow-listed tools execute. Now the allowlist bounds the
# sandbox.
PERMISSION_MODE = "manual"

# Layer 2: an allowlist of pure-read tools. Git is deliberately kept OFF this wildcard
# set. `git diff`, `git show`, and `git log` all accept `--output=<file>`, which git
# opens itself — bypassing Claude Code's shell-redirect guard — so a wildcard such as
# `Bash(git diff:*)` would let prompt-injected content run `git diff --output=<path>`
# and overwrite files despite the read-only claim. The one git command needed to view
# the change under review is instead allow-listed by EXACT string (see _git_view_rule),
# so no write flag can be appended. Read/Glob/Grep cover project navigation.
READ_ONLY_TOOLS = "Read Glob Grep"


def _git_view_rule(job: ReviewJob) -> str | None:
    """Exact-match Bash allow rule for the single git command that surfaces the change.

    build_agent_prompt directs the agent to run this exact command for `--diff` /
    `--staged` / `--commit`; allow only it — no `:*` wildcard — so an `--output` (or
    any other) write flag can't be appended. Returns None for sources whose content is
    already inlined in the prompt (PR, files, stdin, plan), which need no git at all.
    """
    if job.git_cmd:
        return f"Bash({job.git_cmd})"
    if job.commit:
        return f"Bash(git show {job.commit})"
    return None


def review(job: ReviewJob) -> str:
    allowed = READ_ONLY_TOOLS
    git_rule = _git_view_rule(job)
    if git_rule:
        allowed = f"{allowed} {git_rule}"
    cmd = [
        "claude", "-p",
        "--permission-mode", PERMISSION_MODE,
        "--allowedTools", allowed,
    ]
    if job.model:
        cmd += ["--model", job.model]
    if job.effort:
        cmd += ["--effort", job.effort]
    # Prompt goes via stdin: no ARG_MAX concern and no temp file needed.
    timeout = base.TIMEOUT if job.timeout is None else job.timeout
    output = base.run_command(
        cmd, stdin=build_agent_prompt(job), timeout=timeout
    ).strip()
    if not output:
        raise BackendError("claude produced no output")
    return output
