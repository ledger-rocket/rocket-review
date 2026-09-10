from rocket_review.backends import base
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.prompts import build_agent_prompt

NAME = "opencode"
BINARY = "opencode"
INSTALL_HINT = "brew install anomalyco/tap/opencode or npm i -g opencode-ai (https://opencode.ai)"
DEFAULT_MODEL = None  # honor the user's configured opencode default

# The read-only plan agent's limits, stated in its prompt.
ENVIRONMENT = (
    "This review runs as opencode's read-only plan agent: file edits and writes are denied, "
    "and a non-interactive run may also deny shell commands."
)


def review(job: ReviewJob) -> str:
    # `plan` is opencode's built-in read-only agent: it denies edit/write at the tool
    # level, matching codex `-s read-only` and claude's read-only allowlist.
    cmd = ["opencode", "run", "--agent", "plan"]
    if job.model:
        cmd += ["--model", job.model]
    # Deliver the prompt (with the diff the CLI materialized into the job) via stdin, as
    # `opencode run` reads the message from stdin when given no positional. That is
    # permission-independent — unlike telling the read-only plan agent to open a path with
    # its own tools, which non-interactive `opencode run` may deny, leaving it to review
    # nothing — and unbounded, unlike an inline argv message (ARG_MAX, `ps` exposure) or a
    # `--file` attachment (opencode's array-valued flag swallows the trailing message, and
    # its Read tool caps attachments at 50 KiB, silently truncating a large diff).
    timeout = base.TIMEOUT if job.timeout is None else job.timeout
    output = base.run_command(cmd, stdin=build_agent_prompt(job, ENVIRONMENT), timeout=timeout).strip()
    if not output:
        raise BackendError("opencode produced no output")
    return output
