import os
import re
import subprocess
import time
from pathlib import Path

from rocket_review.backends.base import BackendError, ReviewJob, format_duration
from rocket_review.prompts import get_prompt

NAME = "api"
BINARY = None  # SDK, not a CLI
INSTALL_HINT = "set OPENAI_API_KEY (or put it in .env)"
# Canonical API model name; _resolve_model maps it to the latest dated snapshot.
DEFAULT_MODEL = "gpt-5.6"


def review(job: ReviewJob) -> str:
    content = job.content or ""
    if job.docs_content:
        content = (f"=== PROJECT STANDARDS ===\n{job.docs_content}\n"
                   f"=== END PROJECT STANDARDS ===\n\n{content}")
    system_prompt = get_prompt(job.mode, job.docs_content, job.json_output)
    return _call_openai(
        content, system_prompt, job.model or DEFAULT_MODEL, job.extra, job.effort, job.timeout
    )


def _load_env_file() -> None:
    """Load OPENAI_API_KEY from .env files if not already in environment."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    for candidate in [Path.cwd() / ".env", Path.home() / ".env"]:
        if candidate.is_file():
            try:
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
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
        capture_output=True, text=True, encoding="utf-8", errors="replace",
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
        # Only include files within the repo/cwd. is_relative_to, not a string-prefix
        # check: /repo-sibling must not pass as inside /repo.
        if not p.is_relative_to(repo_root):
            continue
        if p.is_file() and p not in seen:
            try:
                if p.stat().st_size <= max_size:
                    parts.append(f"=== {c} ===\n{p.read_text(encoding='utf-8')}")
                    seen.add(p)
            except (OSError, UnicodeDecodeError):
                continue
    return "\n\n".join(parts)


# Canonical 5.6 names (gpt-5.6, gpt-5.6-sol/terra/luna, …) and already-dated snapshots
# are accepted by the API verbatim, so they need no models.list() round-trip to resolve.
_CANONICAL_MODEL_RE = re.compile(r"^gpt-5\.6(-[a-z]+)?$")


def _is_canonical(model: str) -> bool:
    return "-202" in model or bool(_CANONICAL_MODEL_RE.match(model))


def _resolve_model(client, model: str) -> str:
    """Resolve a short model alias to an available dated ID.

    Canonical names pass straight through without listing: models.list() would run
    under the SDK's own default deadline and retries, escaping the caller's --timeout
    bound before the (bounded) responses.create even starts.
    """
    if _is_canonical(model):
        return model
    try:
        available = {m.id for m in client.models.list()}
    except Exception:
        return model
    if model in available:
        return model
    candidates = sorted(m for m in available if m.startswith(model + "-202"))
    return candidates[-1] if candidates else model


def _call_openai(
    content: str,
    system_prompt: str,
    model: str,
    extra: str | None,
    effort: str | None = None,
    timeout: int | None = None,
) -> str:
    _load_env_file()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise BackendError("OPENAI_API_KEY not set. Export it or put it in .env")

    from openai import OpenAI

    client_kwargs = {"api_key": api_key}
    if timeout is not None:
        # Bound every call this client makes — including any models.list() — to --timeout,
        # with retries off so the SDK can't back off past the deadline. For the default
        # canonical model, resolution skips listing, so that single responses.create is the
        # whole budget; a non-canonical alias adds one bounded list call.
        client_kwargs.update(timeout=timeout, max_retries=0)
    client = OpenAI(**client_kwargs)
    # One deadline spans the whole backend, so --timeout bounds resolution + the response
    # call together rather than allowing each up to a full timeout.
    deadline = time.monotonic() + timeout if timeout is not None else None
    # Always resolve, so adding --timeout never changes which model is selected. Canonical
    # names short-circuit without a list call; without a deadline the SDK's default retries
    # stay on for reliability.
    model = _resolve_model(client, model)

    # Extract referenced files and append as context
    referenced = extract_referenced_files(content)
    if referenced:
        content = f"{content}\n\n=== REFERENCED PROJECT FILES ===\n{referenced}\n=== END REFERENCED FILES ==="

    user_message = content
    if extra:
        user_message = f"Additional review instructions: {extra}\n\n---\n\n{user_message}"

    kwargs = {}
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    if deadline is not None:
        # Give the response call only the budget left after resolution, so the two together
        # stay within --timeout. Omitted entirely (not None) when no deadline was requested,
        # so the SDK's own default applies.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackendError(f"OpenAI API call timed out after {format_duration(timeout)}")
        kwargs["timeout"] = remaining
    try:
        response = client.responses.create(
            model=model,
            instructions=system_prompt,
            input=user_message,
            **kwargs,
        )
    except Exception as exc:
        raise BackendError(f"OpenAI API call failed: {exc}")

    return response.output_text
