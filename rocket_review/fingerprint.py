"""`rr --fingerprint`: one hash of everything, other than the content, that decides a verdict.

A caller that keys a cached verdict on the reviewed content plus this fingerprint reuses the
verdict only when neither moved. The two ways to be wrong cost differently: a fingerprint
that holds still across a change to the reviewer hands back a verdict that reviewer never
gave, while one that moves on a change that cannot matter costs one fresh review. So
everything that reaches a backend is in, and only what provably cannot change an answer is
out.

In: the rr version; the mode; each backend and the model it runs; effort; the codex sandbox;
the fail-on threshold; JSON mode; the standards docs as read; the extra instructions; and the
prompt each backend assembles, for every source shape. Out: the timeout, which decides
whether a backend answers and never what it answers, and --full, which only decides how much
of an answer is printed.

Config files enter through the settings they resolve to, never as bytes or paths: a comment
edit is not a different reviewer, and a hook that reviews in a fresh temporary worktree
finds the same project config at a new path every time.
"""

import hashlib
import json
from typing import Any

from rocket_review import config
from rocket_review.backends import BACKENDS
from rocket_review.backends.base import ReviewJob
from rocket_review.cli import rr_version
from rocket_review.models import REVIEW_SCHEMA

#: Bumps when the material below changes meaning, so a caller can refuse a document it
#: does not know rather than compare fingerprints computed two different ways.
FINGERPRINT_VERSION = "1"

#: Every way build_agent_prompt can present the change: an inlined pull request, a commit to
#: `git show`, a working-tree diff to run, and inline content. The prompt is assembled for
#: all of them, because the fingerprint describes the reviewer rather than one run of it, and
#: a per-source instruction reworded in a shape this run did not use is still a different
#: reviewer for the next run that does. The same four shapes as the eval harness's runtime
#: prompt probe (evals/paired_runner.py).
SOURCE_SHAPES: tuple[dict[str, Any], ...] = (
    {"content": "INLINE"},
    {"git_cmd": "git diff HEAD"},
    {"commit": "0" * 40},
    {"pr": True, "content": "PULL REQUEST"},
)


def _sha256(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        for shape in SOURCE_SHAPES:
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
    return {k: v for k, v in doc.items() if k not in ("fingerprint", "models_pinned")}


def digest(hashed: dict[str, Any]) -> str:
    canonical = json.dumps(hashed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def describe(
    *,
    mode: str,
    specs: list[tuple[str, str | None]],
    settings: config.Settings,
    docs_content: str | None,
    extra: str | None,
) -> dict[str, Any]:
    """The fingerprint document `rr --fingerprint` prints.

    `specs` are the backends after every default, fallback and [models] pin has been applied
    — the list a review would fan out to. A backend with no model of its own runs the CLI's
    default, which can change without anything here changing, so it is recorded as null and
    `models_pinned` says so; the caller decides whether that is good enough to reuse on.
    """
    backends = [
        {"name": name, "model": model or BACKENDS[name].DEFAULT_MODEL} for name, model in specs
    ]
    doc: dict[str, Any] = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "rr_version": rr_version(),
        "mode": mode,
        "backends": backends,
        "effort": settings.effort,
        "codex_sandbox": settings.codex_sandbox,
        "fail_on": settings.fail_on,
        "json": settings.json,
        "docs_sha256": _sha256(docs_content),
        "extra_sha256": _sha256(extra),
        "prompt_sha256": prompt_digest(
            mode, specs, docs_content=docs_content, extra=extra,
            json_output=settings.json, codex_sandbox=settings.codex_sandbox,
        ),
    }
    doc["fingerprint"] = digest(material(doc))
    doc["models_pinned"] = all(b["model"] is not None for b in backends)
    return doc
