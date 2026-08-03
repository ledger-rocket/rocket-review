import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from rocket_review import repo
from rocket_review.backends.base import BackendError, ReviewJob, format_duration, interrupted
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
    # Reviewing another repository's text against this checkout's files is not something a
    # gate can make safe: "does the repository track it" would be asked of the wrong
    # repository, so a path the remote PR names and this checkout happens to track would be
    # attached from here. Nothing is attachable in that case.
    if job.foreign_repo:
        print(
            "Note: not attaching files named in the review text — --repo names a different "
            "repository than this checkout, so nothing here is that repository's content.",
            file=sys.stderr,
        )
    return _call_openai(
        content, system_prompt, job.model or DEFAULT_MODEL, job.extra, job.effort,
        job.timeout, job.json_output, extract=not job.foreign_repo,
    )


def _load_env_file() -> None:
    """Load OPENAI_API_KEY from .env files if not already in environment."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    candidates = [Path.cwd() / ".env"]
    try:
        candidates.append(Path.home() / ".env")
    except RuntimeError:
        # HOME unset and no passwd entry for this uid — an ordinary container setup. There
        # is no ~/.env to read then; the environment variable path is unaffected.
        pass
    for candidate in candidates:
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
    """The git repo root, or None outside one — and None rather than a wait, if git hangs.

    Through repo.capture like every other git call on this path: it runs in a backend worker
    with the user's --timeout ticking, so it is bounded, and it never exits the process.
    """
    result = repo.capture(["git", "rev-parse", "--show-toplevel"])
    if result is not None and result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    return None


def _is_local_file(candidate: str) -> bool:
    """Whether the text named something that is actually here, however it was spelled."""
    try:
        return Path(candidate).is_file()
    except (OSError, ValueError):
        return False


def extract_referenced_files(text: str, max_size: int = 100_000) -> str:
    """Attach the local files the reviewed text names, under the repository's own rule.

    The text is repository content — a diff, a PR description, a standards doc's prose — so
    a path it mentions is the repository's word rather than the user's, and it is read only
    when the repository tracks it at HEAD, it resolves inside the checkout, and it is not
    repository metadata. Outside a checkout there is nothing to track, and confinement to
    the working directory is what remains.
    """
    repo_root = _get_repo_root()
    if not repo_root:
        repo_root = Path.cwd().resolve()

    backtick = re.findall(r"`([^`\s]+\.\w{1,10})`", text)
    bare = re.findall(r"(?<![`\w])([\w][\w./-]*\.\w{1,10})(?![\w`])", text)
    candidates = sorted(set(backtick + bare))

    # One gate call for the whole set: the tracked half is a single git query and the rest
    # is pure path work. Unresolved paths go in, so a candidate carrying a NUL — a diff is
    # decoded with errors="replace", and the pattern will match one — raises inside the
    # gate's guarded resolve rather than out here, where it would cost every other candidate
    # its attachment.
    allowed = repo.resolve_doc_paths(
        (Path(c) for c in candidates), user_named=False, base=repo_root,
    )

    parts = []
    seen = set()
    withheld = 0
    for candidate, path in zip(candidates, allowed, strict=True):
        if path is None:
            # Only a file that is really here was withheld; a path the text merely mentions
            # and this checkout does not have was never a candidate for attachment, and
            # counting it would make the note fire on ordinary prose.
            if _is_local_file(candidate):
                withheld += 1
            continue
        if path.is_file() and path not in seen:
            try:
                if path.stat().st_size <= max_size:
                    parts.append(f"=== {candidate} ===\n{path.read_text(encoding='utf-8')}")
                    seen.add(path)
            except (OSError, UnicodeDecodeError):
                continue
    if withheld:
        # Every docs route says why it skipped something; this one used to refuse in silence,
        # which reads as "there was nothing there" rather than "the repository does not
        # vouch for it". Aggregated, because a diff can name a great many paths.
        named = "file named" if withheld == 1 else "files named"
        print(
            f"Note: {withheld} local {named} in the reviewed text not attached: rr reads one "
            "only when the repository tracks it, it resolves inside the checkout, and it is "
            "not repository metadata (.git).",
            file=sys.stderr,
        )
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

    A refusal carries an `output_text` of ""; a truncated response carries a fragment.
    Either one returned as-is reads like a finished review, and under --json the parser
    would blame the model for unparsable output rather than surfacing the cause. A prose
    review does lose the fragment, which is why the error says so — a partial review
    presented as a whole one is worse than an error.
    """
    status = getattr(response, "status", None)
    if status is not None and status != "completed":
        raise BackendError(
            f"OpenAI API returned an unfinished response "
            f"(status {status}){_status_detail(response)}; partial output discarded"
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
    extract: bool = True,
) -> str:
    # Same gate the subprocess backends check before Popen: once an interrupt is under way
    # this worker must not open a billed call the teardown has no way to stop. Checked at
    # the top so neither model resolution nor the response call goes out.
    if interrupted():
        raise BackendError("interrupted before launch")

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

    # Extract referenced files and append as context. Each of the gate's git calls carries
    # its own bound (repo.GATE_TIMEOUT), and none of them is subtracted from --timeout — so
    # a run with less than one call's worth left would spend the remainder deciding what to
    # attach and have nothing left to ask the model with. Under that line, attach nothing and
    # go straight to the call. This bounds the common case, not the worst one: extraction can
    # make up to three calls (repo root, the scoped query, and a case-fold fallback), so a
    # deadline that must hold exactly needs one threaded through the gate itself.
    if extract:
        remaining = None if timeout is None else timeout - (time.monotonic() - start)
        if remaining is not None and remaining < repo.GATE_TIMEOUT:
            print(
                f"Note: not attaching files named in the review text — under "
                f"{repo.GATE_TIMEOUT}s of --timeout is left, which is the budget the "
                f"tracked-file check alone may take.",
                file=sys.stderr,
            )
        elif referenced := extract_referenced_files(content):
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
