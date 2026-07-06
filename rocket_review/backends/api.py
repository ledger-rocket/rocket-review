import os
import re
import subprocess
from pathlib import Path

from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.prompts import get_prompt

NAME = "api"
BINARY = None  # SDK, not a CLI
INSTALL_HINT = "set OPENAI_API_KEY (or put it in .env)"
DEFAULT_MODEL = "gpt-5.5"


def review(job: ReviewJob) -> str:
    content = job.content or ""
    if job.docs_content:
        content = (f"=== PROJECT STANDARDS ===\n{job.docs_content}\n"
                   f"=== END PROJECT STANDARDS ===\n\n{content}")
    system_prompt = get_prompt(job.mode, job.docs_content)
    return _call_openai(content, system_prompt, job.model or DEFAULT_MODEL, job.extra)


def _load_env_file() -> None:
    """Load OPENAI_API_KEY from .env files if not already in environment."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    for candidate in [Path.cwd() / ".env", Path.home() / ".env"]:
        if candidate.is_file():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("OPENAI_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        os.environ["OPENAI_API_KEY"] = val
                        return


def _get_repo_root() -> Path | None:
    """Get the git repo root, or None if not in a repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    return None


def extract_referenced_files(text: str, max_size: int = 100_000) -> str:
    """Extract file paths from text and read their contents (repo-scoped only)."""
    repo_root = _get_repo_root()
    if not repo_root:
        repo_root = Path.cwd().resolve()

    backtick = re.findall(r"`([^`\s]+\.\w{1,10})`", text)
    bare = re.findall(r"(?<![`\w])([\w][\w./-]*\.\w{1,10})(?![\w`])", text)
    candidates = set(backtick + bare)

    parts = []
    seen = set()
    for c in sorted(candidates):
        p = Path(c).resolve()
        # Only include files within the repo/cwd
        if not str(p).startswith(str(repo_root)):
            continue
        if p.is_file() and p not in seen:
            try:
                if p.stat().st_size <= max_size:
                    parts.append(f"=== {c} ===\n{p.read_text()}")
                    seen.add(p)
            except (OSError, UnicodeDecodeError):
                continue
    return "\n\n".join(parts)


def _resolve_model(client, model: str) -> str:
    """Resolve short model alias to available dated ID."""
    if "-202" in model:
        return model
    try:
        available = {m.id for m in client.models.list()}
    except Exception:
        return model
    if model in available:
        return model
    candidates = sorted(m for m in available if m.startswith(model + "-202"))
    return candidates[-1] if candidates else model


def _call_openai(content: str, system_prompt: str, model: str, extra: str | None) -> str:
    _load_env_file()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise BackendError("OPENAI_API_KEY not set. Export it or put it in .env")

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    model = _resolve_model(client, model)

    # Extract referenced files and append as context
    referenced = extract_referenced_files(content)
    if referenced:
        content = f"{content}\n\n=== REFERENCED PROJECT FILES ===\n{referenced}\n=== END REFERENCED FILES ==="

    user_message = content
    if extra:
        user_message = f"Additional review instructions: {extra}\n\n---\n\n{user_message}"

    try:
        response = client.responses.create(
            model=model,
            instructions=system_prompt,
            input=user_message,
        )
    except Exception as exc:
        raise BackendError(f"OpenAI API call failed: {exc}")

    return response.output_text
