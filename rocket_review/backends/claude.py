from rocket_review.backends import base
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.prompts import build_agent_prompt

NAME = "claude"
BINARY = "claude"
INSTALL_HINT = "npm install -g @anthropic-ai/claude-code (https://claude.com/claude-code)"
DEFAULT_MODEL = None  # honor the user's Claude Code default model

# Read-only review sandbox: exploration tools plus non-mutating git.
READ_ONLY_TOOLS = (
    "Read Glob Grep "
    "Bash(git diff:*) Bash(git show:*) Bash(git log:*) Bash(git status:*) Bash(ls:*)"
)


def review(job: ReviewJob) -> str:
    cmd = ["claude", "-p", "--allowedTools", READ_ONLY_TOOLS]
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
