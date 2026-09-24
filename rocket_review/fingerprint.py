"""`rr --fingerprint`: one hash of everything, other than the content, that decides a verdict.

A caller that keys a cached verdict on the reviewed content plus this fingerprint reuses the
verdict only when neither moved. The two ways to be wrong cost differently: a fingerprint
that holds still across a change to the reviewer hands back a verdict that reviewer never
gave, while one that moves on a change that cannot matter costs one fresh review. So
everything rr decides is in, and only what provably cannot change an answer is out.

In: rr's version and its own code; the mode and which source flag was given; each backend, the model it runs and its CLI's
version (for api, the OpenAI SDK's version and the endpoint it sends to); effort; the codex sandbox; the fail-on threshold; JSON mode; the timeout, because
api drops its file attachments when too little of it is left; whether the text comes from
another repository, which also turns attachments off; the standards docs as read; the extra
instructions; and the prompt each backend assembles for every source shape. Out: --full,
which only decides how much of an answer is printed.

Config files enter through the settings they resolve to, never as bytes or paths: a comment
edit is not a different reviewer, and a hook that reviews in a fresh temporary worktree
finds the same project config at a new path every time.

What rr cannot see is not in either: the instruction and settings files each backend CLI
loads for itself (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md and the like). `pinned` covers
the defaults rr can see — a model, an effort or a CLI version it does not know.
"""

import hashlib
import json
import os
import re
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import rocket_review
from rocket_review import cli, config
from rocket_review.backends import BACKENDS, api
from rocket_review.backends.base import ReviewJob
from rocket_review.cli import rr_version
from rocket_review.models import REVIEW_SCHEMA

#: Bumps when the material below changes meaning, so a caller can refuse a document it
#: does not know rather than compare fingerprints computed two different ways.
FINGERPRINT_VERSION = "1"

#: Seconds for one `<cli> --version`. The probe runs on every fingerprint, which a pre-push
#: hook asks for on every push.
VERSION_TIMEOUT = 5.0


def source_shapes() -> list[dict[str, Any]]:
    """Every way build_agent_prompt can present the change.

    An inlined pull request, a commit to `git show`, each working-tree diff command, and
    inline content. The prompt is assembled for all of them, because the fingerprint
    describes the reviewer rather than one run of it: a per-source instruction reworded in a
    shape this run did not use is still a different reviewer for the next run that does.
    The same shapes as the eval harness's runtime prompt probe (evals/paired_runner.py).
    Read at call time, so the commands are the ones a review sends.
    """
    return [
        {"content": "INLINE"},
        {"git_cmd": cli.DIFF_GIT_CMD},
        {"git_cmd": cli.STAGED_GIT_CMD},
        {"commit": "0" * 40},
        {"pr": True, "content": "PULL REQUEST"},
    ]


def _sha256(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def code_digest(root: Path | None = None) -> str:
    """sha256 over every module of the installed package, by relative path and content.

    The version string is fixed in a source checkout and in an editable install, and the
    code that parses a review, gates it and builds each backend's command line decides a
    verdict as much as the prompt does.
    """
    root = root or Path(rocket_review.__file__).parent
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        h.update(path.relative_to(root).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def cli_version(binary: str) -> str | None:
    """`<binary> --version`, or None when it does not answer. Never raises, never prompts."""
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=VERSION_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = result.stdout.strip()
    return version if result.returncode == 0 and version else None


def sdk_version() -> str | None:
    """The installed OpenAI SDK's version, which decides how api's request reaches the model."""
    try:
        return f"openai {version('openai')}"
    except PackageNotFoundError:
        return None


#: An api model name that names one model: a dated snapshot, or a family name with one of its
#: tiers, which rr passes verbatim. The bare family name is the alias rr's api backend says
#: OpenAI can remap; any other name is resolved to the newest dated snapshot at run time.
_API_FIXED_MODEL = re.compile(r"-20\d\d-\d\d-\d\d$|^gpt-5\.6-(sol|terra|luna)$")


def _endpoint(url: str | None) -> str | None:
    """Where a request goes — scheme, host, port, path — without userinfo or query.

    The URL can carry a credential, and a rotated one changes nothing about the reviewer.
    """
    if not url:
        return None
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path.rstrip('/')}"


def prompt_digest(
    mode: str,
    specs: list[tuple[str, str | None]],
    *,
    docs_content: str | None,
    extra: str | None,
    json_output: bool,
    codex_sandbox: str,
) -> str:
    """sha256 over the prompt each selected backend would send, for every source shape.

    Assembled through each backend's own `prompt`, the function its `review` sends, so what
    is hashed cannot drift from what is sent. The output schema rides along in JSON mode:
    codex and api hand it to the model as a constraint, which is as much instruction as the
    prompt text is.
    """
    h = hashlib.sha256()
    for name, model in specs:
        for shape in source_shapes():
            job = ReviewJob(
                mode=mode, content=None, docs_content=docs_content, extra=extra,
                commit=None, pr=False, git_cmd=None, model=model,
                json_output=json_output, codex_sandbox=codex_sandbox,
            )
            for key, value in shape.items():
                setattr(job, key, value)
            for part in (name, BACKENDS[name].prompt(job)):
                h.update(part.encode("utf-8"))
                h.update(b"\0")
    if json_output:
        h.update(json.dumps(REVIEW_SCHEMA, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def material(doc: dict[str, Any]) -> dict[str, Any]:
    """The hashed part of a fingerprint document: everything except what is derived from it."""
    return {k: v for k, v in doc.items() if k not in ("fingerprint", "pinned")}


def digest(hashed: dict[str, Any]) -> str:
    canonical = json.dumps(hashed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pinned(backends: list[dict[str, Any]], effort: str | None) -> bool:
    """Whether every choice a backend would otherwise make for itself is written down here.

    A model rr passes no name for, an effort it passes none for, and a CLI that will not say
    its version are each a default that can change under an unchanged fingerprint. So an
    opencode backend is never pinned: it takes no --effort and applies its own config's. An
    api name has to name one model (_API_FIXED_MODEL), and a claude one has to be a model id
    rather than one of Claude Code's aliases (default, opus, sonnet, ...), which follow the
    account's current mapping.

    A name that passes can still be one the vendor moves on its side; rr cannot see that.
    """
    if effort is None:
        return False
    for backend in backends:
        model = backend["model"]
        if model is None:
            return False
        if backend["name"] == "api":
            if not _API_FIXED_MODEL.search(model) or backend["sdk_version"] is None:
                return False
            continue
        if backend["cli_version"] is None:
            return False
        if backend["name"] == "claude" and not model.startswith("claude-"):
            return False
    return True


def describe(
    *,
    mode: str,
    source: str,
    specs: list[tuple[str, str | None]],
    settings: config.Settings,
    docs_content: str | None,
    extra: str | None,
    foreign_repo: bool,
) -> dict[str, Any]:
    """The fingerprint document `rr --fingerprint` prints.

    `specs` are the backends after every default, fallback and [models] pin has been applied
    — the list a review would fan out to. A backend with no model of its own runs the CLI's
    default, recorded as null; `pinned` says whether anything the fingerprint cannot see is
    left to a default, and the caller decides whether to reuse on it.
    """
    backends = []
    for name, model in specs:
        mod = BACKENDS[name]
        entry: dict[str, Any] = {
            "name": name,
            "model": model or mod.DEFAULT_MODEL,
            "cli_version": cli_version(mod.BINARY) if mod.BINARY else None,
        }
        if mod is api:
            # The SDK sends to OPENAI_BASE_URL when it is set. Hashed rather than printed,
            # and without its credentials (_endpoint).
            entry["sdk_version"] = sdk_version()
            entry["endpoint_sha256"] = _sha256(_endpoint(os.environ.get("OPENAI_BASE_URL")))
        backends.append(entry)
    doc: dict[str, Any] = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "rr_version": rr_version(),
        "code_sha256": code_digest(),
        "mode": mode,
        "source": source,
        "backends": backends,
        "effort": settings.effort,
        "codex_sandbox": settings.codex_sandbox,
        "fail_on": settings.fail_on,
        "json": settings.json,
        "timeout": settings.timeout,
        "foreign_repo": foreign_repo,
        "docs_sha256": _sha256(docs_content),
        "extra_sha256": _sha256(extra),
        "prompt_sha256": prompt_digest(
            mode, specs, docs_content=docs_content, extra=extra,
            json_output=settings.json, codex_sandbox=settings.codex_sandbox,
        ),
    }
    doc["fingerprint"] = digest(material(doc))
    doc["pinned"] = _pinned(backends, settings.effort)
    return doc
