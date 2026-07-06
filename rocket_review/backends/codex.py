import tempfile
from pathlib import Path

from rocket_review.backends import base
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.prompts import build_agent_prompt

NAME = "codex"
BINARY = "codex"
INSTALL_HINT = "npm install -g @openai/codex (https://github.com/openai/codex)"
DEFAULT_MODEL = "gpt-5.5"


def review(job: ReviewJob) -> str:
    prompt_file = base.write_prompt_file(build_agent_prompt(job))
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        outfile = Path(f.name)
    try:
        cmd = ["codex", "exec", "-s", "read-only", "-o", str(outfile),
               "-m", job.model or DEFAULT_MODEL,
               f"Read the file {prompt_file} for your full instructions, then follow them."]
        base.run_command(cmd)
        output = outfile.read_text().strip()
        if not output:
            raise BackendError("codex produced no output")
        return output
    finally:
        outfile.unlink(missing_ok=True)
        prompt_file.unlink(missing_ok=True)
