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
    # Free-form reasoning-effort string passed through to the backend; each backend
    # maps it to its own flag and lets invalid values fail loudly downstream.
    effort: str | None = None
    # Per-backend subprocess timeout in seconds; None means the backend falls back
    # to base.TIMEOUT. High-effort reasoning models can outrun the 900s default.
    timeout: int | None = None


def run_command(cmd: list[str], *, stdin: str | None = None, timeout: int = TIMEOUT) -> str:
    try:
        # errors="replace": backend output may quote non-UTF8 bytes from reviewed
        # files; a stray byte must not turn a finished review into a decode crash.
        result = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # --timeout now takes arbitrary seconds; only render "N minutes" when it
        # divides evenly, otherwise report the exact seconds so the message stays true.
        duration = f"{timeout // 60} minutes" if timeout % 60 == 0 else f"{timeout} seconds"
        raise BackendError(f"{cmd[0]} timed out after {duration}")
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
        mode="w", suffix=".md", delete=False, prefix="rr-prompt-", encoding="utf-8",
    ) as f:
        f.write(prompt)
        return Path(f.name)
