from rocket_review.backends import base
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.prompts import build_agent_prompt

NAME = "opencode"
BINARY = "opencode"
INSTALL_HINT = "brew install anomalyco/tap/opencode or npm i -g opencode-ai (https://opencode.ai)"
DEFAULT_MODEL = None  # honor the user's configured opencode default


def review(job: ReviewJob) -> str:
    prompt_file = base.write_prompt_file(build_agent_prompt(job))
    try:
        # `plan` is opencode's built-in read-only agent: it denies edit/write at the
        # tool level, matching codex `-s read-only` and claude's read-only allowlist.
        cmd = ["opencode", "run", "--agent", "plan"]
        if job.model:
            cmd += ["--model", job.model]
        cmd.append(
            f"Read the file {prompt_file} for your full instructions, then follow them. "
            "You are performing a code review: Do not modify any files."
        )
        output = base.run_command(cmd).strip()
        if not output:
            raise BackendError("opencode produced no output")
        return output
    finally:
        prompt_file.unlink(missing_ok=True)
