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


def format_duration(seconds: int) -> str:
    """Render a second count as whole minutes when it divides evenly, else seconds."""
    if seconds != 0 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} second" if seconds == 1 else f"{seconds} seconds"


def run_command(cmd: list[str], *, stdin: str | None = None, timeout: int = TIMEOUT) -> str:
    try:
        # errors="replace": backend output may quote non-UTF8 bytes from reviewed
        # files; a stray byte must not turn a finished review into a decode crash.
        result = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise BackendError(f"{cmd[0]} timed out after {format_duration(timeout)}")
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
