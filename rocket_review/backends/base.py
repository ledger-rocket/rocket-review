import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

TIMEOUT = 900


class BackendError(Exception):
    """A backend failed to produce a review."""


@dataclass
class ReviewJob:
    mode: str
    content: str | None
    docs_content: str | None
    extra: str | None
    commit: str | None
    pr: bool
    git_cmd: str | None
    model: str | None
    json_output: bool = False


def run_command(cmd: list[str], *, stdin: str | None = None, timeout: int = TIMEOUT) -> str:
    try:
        result = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise BackendError(f"{cmd[0]} timed out after {timeout // 60} minutes")
    except FileNotFoundError:
        raise BackendError(f"{cmd[0]} not found on PATH")
    if result.returncode != 0:
        raise BackendError(
            f"{cmd[0]} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def write_prompt_file(prompt: str) -> Path:
    # File indirection instead of argv keeps large prompts under ARG_MAX.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, prefix="rr-prompt-",
    ) as f:
        f.write(prompt)
        return Path(f.name)
