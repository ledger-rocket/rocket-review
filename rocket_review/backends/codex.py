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
DEFAULT_MODEL = None  # honor the user's codex default (~/.codex/config.toml)

# What each `codex exec -s <mode>` policy means for the reviewer, stated in its prompt
# The keys cover config.CODEX_SANDBOX_MODES; a test pins that.
SANDBOX_ENVIRONMENTS = {
    "read-only": (
        "This review runs under Codex's read-only sandbox policy: the sandbox permits reading "
        "files only, and network access is restricted. A test, build or package manager that "
        "writes a cache or fetches a dependency fails here."
    ),
    "workspace-write": (
        "This review runs under Codex's workspace-write sandbox policy: the sandbox permits "
        "reading files, and editing files in the working directory and the sandbox's other "
        "writable roots, which normally include a temporary directory; editing anywhere else "
        "needs approval, and network access is restricted. Do not modify the project's files."
    ),
    "danger-full-access": (
        "This review runs with no Codex sandbox (danger-full-access): there is no filesystem "
        "sandboxing and network access is enabled. Do not modify any files, and run only "
        "commands that inspect."
    ),
}


def _environment(job: ReviewJob) -> str:
    try:
        return SANDBOX_ENVIRONMENTS[job.codex_sandbox]
    except KeyError:
        raise BackendError(
            f"no sandbox description for codex sandbox {job.codex_sandbox!r}"
        ) from None


def review(job: ReviewJob) -> str:
    prompt_file = base.write_prompt_file(build_agent_prompt(job, _environment(job)))
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        outfile = Path(f.name)
    schema_file = None
    try:
        cmd = ["codex", "exec", "-s", job.codex_sandbox, "-o", str(outfile)]
        model = job.model or DEFAULT_MODEL
        if model:
            cmd += ["-m", model]
        if job.effort:
            cmd += ["-c", f"model_reasoning_effort={job.effort}"]
        if job.json_output:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as sf:
                json.dump(REVIEW_SCHEMA, sf)
                schema_file = Path(sf.name)
            cmd += ["--output-schema", str(schema_file)]
        cmd.append(f"Read the file {prompt_file} for your full instructions, then follow them.")
        timeout = base.TIMEOUT if job.timeout is None else job.timeout
        base.run_command(cmd, timeout=timeout)
        output = outfile.read_text(encoding="utf-8", errors="replace").strip()
        if not output:
            raise BackendError("codex produced no output")
        return output
    finally:
        outfile.unlink(missing_ok=True)
        prompt_file.unlink(missing_ok=True)
        if schema_file:
            schema_file.unlink(missing_ok=True)
