import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from rocket_review.backends.base import BackendError, ReviewJob, format_duration
from rocket_review.models import REVIEW_SCHEMA
from rocket_review.prompts import get_prompt

NAME = "api"
BINARY = None  # SDK, not a CLI
INSTALL_HINT = "set OPENAI_API_KEY (or put it in .env)"
# Explicit balanced-tier model; _resolve_model maps it to the latest dated snapshot.
# Never use the bare "gpt-5.6" alias here — it maps to the pricier flagship (sol)
# and OpenAI can remap it out from under us.
DEFAULT_MODEL = "gpt-5.6-terra"


def review(job: ReviewJob) -> str:
    content = job.content or ""
    if job.docs_content:
        content = (f"=== PROJECT STANDARDS ===\n{job.docs_content}\n"
                   f"=== END PROJECT STANDARDS ===\n\n{content}")
    system_prompt = get_prompt(job.mode, job.docs_content, job.json_output)
    return _call_openai(
        content, system_prompt, job.model or DEFAULT_MODEL, job.extra, job.effort,
        job.timeout, job.json_output,
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


def _refusal(response) -> str | None:
    """The model's stated reason for declining, if the response is a refusal."""
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return getattr(part, "refusal", None) or "no reason given"
    return None


def _status_detail(response) -> str:
    """Whatever the API said about why a response is not a finished answer."""
    for holder, field in (
        (getattr(response, "incomplete_details", None), "reason"),
        (getattr(response, "error", None), "message"),
    ):
        value = getattr(holder, field, None)
        if value:
            return f": {value}"
    return ""


def _output_text(response) -> str:
    """The review text, or a BackendError naming why there is none.

    A refused or truncated response still carries an `output_text` of "" — returning it
    would present a non-answer as a clean review, and under --json the parser would blame
    the model for unparsable output rather than surfacing the refusal.
    """
    status = getattr(response, "status", None)
    if status is not None and status != "completed":
        raise BackendError(
            f"OpenAI API returned an unfinished response "
            f"(status {status}){_status_detail(response)}"
        )
    if refusal := _refusal(response):
        raise BackendError(f"OpenAI API refused the request: {refusal}")
    text = response.output_text or ""
    if not text.strip():
        raise BackendError("OpenAI API returned an empty response")
    return text


def _call_openai(
    content: str,
    system_prompt: str,
    model: str,
    extra: str | None,
    effort: str | None = None,
    timeout: int | None = None,
    json_output: bool = False,
) -> str:
    _load_env_file()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise BackendError("OPENAI_API_KEY not set. Export it or put it in .env")

    # Lazy import so the base install stays SDK-free; only the api backend needs it.
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise BackendError(
            "the api backend needs the OpenAI SDK — install with "
            "`pipx inject rocket-review openai` or `pip install 'rocket-review[api]'`"
        ) from exc

    if timeout is not None:
        # Bound every call this client makes — including any models.list() — to --timeout,
        # with retries off so the SDK can't back off past the deadline. For the default
        # canonical model, resolution skips listing, so that single responses.create is the
        # whole budget; a non-canonical alias adds one bounded list call.
        client = OpenAI(api_key=api_key, timeout=timeout, max_retries=0)
    else:
        client = OpenAI(api_key=api_key)
    # One deadline spans the whole backend, so --timeout bounds resolution + the response
    # call together rather than allowing each up to a full timeout.
    start = time.monotonic()
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

    kwargs: dict[str, Any] = {}
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    if json_output:
        # The schema is enforced by the API rather than merely described in the prompt, and
        # it is REVIEW_SCHEMA itself — the same object the envelope parser expects, so the
        # two can never describe different shapes.
        kwargs["text"] = {
            "format": {
                "type": "json_schema",
                "name": "review",
                "strict": True,
                "schema": REVIEW_SCHEMA,
            }
        }
    if timeout is not None:
        # Give the response call only the budget left after resolution, so the two together
        # stay within --timeout. Omitted entirely (not None) when no deadline was requested,
        # so the SDK's own default applies.
        remaining = timeout - (time.monotonic() - start)
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

    return _output_text(response)
