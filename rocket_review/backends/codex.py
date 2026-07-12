import json
import tempfile
from pathlib import Path

from rocket_review.backends import base
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.models import REVIEW_SCHEMA
from rocket_review.prompts import build_agent_prompt

NAME = "codex"
BINARY = "codex"
INSTALL_HINT = "npm install -g @openai/codex (https://github.com/openai/codex)"
# The ChatGPT-account-accessible 5.6 variant; plain gpt-5.6 is API-key-only
# (codex with a ChatGPT account rejects gpt-5.6 and gpt-5.6-codex).
DEFAULT_MODEL = "gpt-5.6-sol"


def review(job: ReviewJob) -> str:
    prompt_file = base.write_prompt_file(build_agent_prompt(job))
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        outfile = Path(f.name)
    schema_file = None
    try:
        cmd = ["codex", "exec", "-s", "read-only", "-o", str(outfile),
               "-m", job.model or DEFAULT_MODEL]
        if job.effort:
            cmd += ["-c", f"model_reasoning_effort={job.effort}"]
        if job.json_output:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as sf:
                json.dump(REVIEW_SCHEMA, sf)
                schema_file = Path(sf.name)
            cmd += ["--output-schema", str(schema_file)]
        cmd.append(f"Read the file {prompt_file} for your full instructions, then follow them.")
        base.run_command(cmd)
        output = outfile.read_text(encoding="utf-8", errors="replace").strip()
        if not output:
            raise BackendError("codex produced no output")
        return output
    finally:
        outfile.unlink(missing_ok=True)
        prompt_file.unlink(missing_ok=True)
        if schema_file:
            schema_file.unlink(missing_ok=True)
