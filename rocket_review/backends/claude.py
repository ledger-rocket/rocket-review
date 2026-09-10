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


def _git_view_command(job: ReviewJob) -> str | None:
    """The single git command that surfaces the change, or None when the prompt inlines it.

    Same precedence as build_agent_prompt, which decides what the prompt asks the reviewer to
    run: an inlined pull request needs no git, then --commit, then --diff/--staged. Resolving
    it in another order would allow-list one command and ask for another, and the reviewer
    would meet a denial on the command the prompt named.
    """
    if job.pr and job.content:
        return None
    if job.commit:
        return f"git show {job.commit}"
    return job.git_cmd or None


def _git_view_rule(job: ReviewJob) -> str | None:
    """Exact-match Bash allow rule for the single git command that surfaces the change.

    build_agent_prompt directs the agent to run this exact command for `--diff` /
    `--staged` / `--commit`; allow only it — no `:*` wildcard — so an `--output` (or
    any other) write flag can't be appended. Returns None for sources whose content is
    already inlined in the prompt (PR, files, stdin, plan), which need no git at all.
    """
    command = _git_view_command(job)
    return f"Bash({command})" if command else None


def _environment(job: ReviewJob) -> str:
    """What this sandbox lets the reviewer do, stated in its prompt.

    Built from the same sources as the allowlist, so the prompt cannot promise a tool that
    `--allowedTools` denies or hide one it allows.
    """
    names = READ_ONLY_TOOLS.split()
    tools = f"the {', '.join(names[:-1])} and {names[-1]} tools"
    command = _git_view_command(job)
    if command:
        tools += f", and the single shell command `{command}`"
    return (
        f"This review runs in a read-only sandbox. You can use only {tools}. Every other "
        "command is denied, including tests, type checks, linters, builds, package managers, "
        "gh, and any other git command, so do not attempt them."
    )


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
        cmd, stdin=build_agent_prompt(job, _environment(job)), timeout=timeout
    ).strip()
    if not output:
        raise BackendError("claude produced no output")
    return output
