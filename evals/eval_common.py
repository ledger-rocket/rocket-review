"""Machinery shared by the eval runners: subprocess handling, versions, result files.

Both sweeps launch `rr` as a subprocess, bound it the same way, and write the same style of
JSONL result file. The teardown in particular (see `terminate_rr`) is subtle enough that a
second copy would drift away from the first and quietly start orphaning billed backends.
"""

from __future__ import annotations

import json
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from rocket_review.backends import BACKENDS

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# rr already bounds the backend with --timeout; this only covers rr's own startup,
# git preflight and teardown so a wedged process can't stall the whole sweep.
SUBPROCESS_GRACE = 120

# How long rr gets to run its own backend teardown after SIGINT before we give up
# and kill it. See terminate_rr.
CLEANUP_GRACE = 10


@dataclass(frozen=True)
class Invocation:
    """One completed attempt at running rr, before anything is judged about its output."""

    stdout: str
    stderr: str
    exit_code: int | None
    duration_s: float
    started_at: str
    #: Set only when the harness never got a judgeable run out of rr: it could not be
    #: launched, or it outlived the deadline. A non-zero exit is not this — rr ran, and
    #: what that means is the caller's to decide.
    harness_error: str | None


def tool_version(command: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or proc.stderr).strip().splitlines()
    return lines[0] if lines else None


def harness_rr_version() -> str | None:
    # The rocket-review importable *here*, which is also where REVIEW_SCHEMA comes from.
    # rr has no --version flag, so an --rr pointing at another environment cannot be
    # interrogated: for those runs this names the harness, not the thing under test.
    try:
        return version("rocket-review")
    except PackageNotFoundError:
        return None


def backend_versions(backends: list[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in backends:
        binary = getattr(BACKENDS[name], "BINARY", None)
        versions[name] = tool_version([binary, "--version"]) if binary else None
    return versions


def parse_backend_specs(value: str) -> tuple[list[tuple[str, str]], str | None]:
    """Parse `name:model,name:model` into pairs. Returns (specs, error)."""
    specs: list[tuple[str, str]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, model = item.partition(":")
        name, model = name.strip(), model.strip()
        if not sep or not model:
            return [], (
                f"backend spec {item!r} must be name:model. The model is required because "
                "rr's envelope reports the model argument, and a default run reports null."
            )
        if name not in BACKENDS:
            return [], f"unknown backend {name!r}. Available: {', '.join(BACKENDS)}."
        if any(existing == name for existing, _ in specs):
            # One model per backend per sweep: case ids and the summary both key on the
            # backend name, so two models for one backend would silently merge.
            return [], f"backend {name!r} listed twice; run a separate sweep per model."
        specs.append((name, model))
    if not specs:
        return [], "no backend given."
    return specs, None


def terminate_rr(proc: subprocess.Popen, deadline: int) -> tuple[str, str, str]:
    """Stop a timed-out rr without orphaning the backends it is paying for.

    rr launches backend CLIs with start_new_session=True, so they sit in their own
    process groups and outlive their parent; the only thing that tears them down is rr's
    own KeyboardInterrupt handler. SIGKILL here would therefore leave a codex process
    running and billing. Send SIGINT instead, give rr a short window to run that
    teardown, and kill only if it ignores us.
    """
    note = ""
    try:
        proc.send_signal(signal.SIGINT)
    except (OSError, ValueError):
        note = "; rr had already exited"
    try:
        stdout, stderr = proc.communicate(timeout=CLEANUP_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        note = f"; rr ignored SIGINT for {CLEANUP_GRACE}s and was killed — backend " \
               "processes may still be running"
    return stdout or "", stderr or "", (
        f"rr exceeded the harness timeout ({deadline}s){note}"
    )


def invoke_rr(
    command: list[str], cwd: Path, deadline: int, env: dict[str, str] | None = None,
) -> Invocation:
    """Run one rr subprocess to completion, bounded by `deadline` seconds."""
    started_at = datetime.now(UTC).isoformat()
    start = time.monotonic()
    exit_code: int | None = None
    stdout = ""
    stderr = ""
    harness_error: str | None = None
    try:
        proc = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as e:
        harness_error = f"could not launch rr: {e}"
    else:
        try:
            stdout, stderr = proc.communicate(timeout=deadline)
        except subprocess.TimeoutExpired:
            stdout, stderr, harness_error = terminate_rr(proc, deadline)
        exit_code = proc.returncode
    return Invocation(
        stdout=stdout, stderr=stderr, exit_code=exit_code,
        duration_s=round(time.monotonic() - start, 2),
        started_at=started_at, harness_error=harness_error,
    )


def extract_backend_result(stdout: str, backend: str) -> tuple[dict | None, str | None]:
    """Pull this backend's entry out of an rr --json envelope.

    Returns (result, envelope_error): envelope_error is set when the envelope is missing
    or malformed, which is a harness-visible failure rather than a backend format
    problem. Every shape is checked rather than assumed — a structurally odd envelope
    must degrade to one backend_error record, never take the sweep down and lose the run.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "rr did not emit a JSON envelope"
    if not isinstance(envelope, dict):
        return None, "rr envelope is not a JSON object"
    results = envelope.get("results")
    if not isinstance(results, list):
        return None, "rr envelope has no results list"
    for result in results:
        if isinstance(result, dict) and result.get("backend") == backend:
            return result, None
    return None, f"no {backend} entry in rr envelope"


def new_results_path(out_dir: Path, prefix: str) -> Path:
    """Name a fresh JSONL file that no concurrent run can collide with.

    Microseconds plus a random suffix: two sweeps started in the same second must not
    truncate each other's (expensive, unrepeatable) results. Callers open it with "x".
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return out_dir / f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}.jsonl"
