"""Measure how often backends really produce `REVIEW_SCHEMA`-compliant `--json` output.

Runs `rr --commit <sha> --backend <name>:<model> --json --full` repeatedly across a set of
commits and backends, stores one JSONL record per run under `evals/results/`, and prints
per-backend counts of valid / schema_violation / decode_failure / backend_error.

Every run is a real backend review and costs real tokens, so this is a deliberate,
manual, offline measurement — never a CI gate. See evals/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from threading import Lock

from rocket_review.backends import BACKENDS

# evals/ is a script directory, not an installed package, so put it on the path
# explicitly: `python -m evals.m0_sweep` would otherwise not find its sibling.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from strict_validator import (  # noqa: E402
    BACKEND_ERROR,
    OUTCOMES,
    SCHEMA_VIOLATION,
    VALID,
    classify_output,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Representative diffs from this repo's own history: a one-line pin bump, a docs-only
# edit, a config/YAML rewrite, a small bugfix, and the large multi-backend feature.
# Spread matters more than count — schema drift shows up on long reviews with many
# findings, which the small commits would never produce.
DEFAULT_COMMITS = ["4923a44", "dd56c0d", "c518e67", "1208fd3", "a8ee29f"]

DEFAULT_RUNS = 3
DEFAULT_TIMEOUT = 900
DEFAULT_CONCURRENCY = 3

# rr already bounds the backend with --timeout; this only covers rr's own startup,
# git preflight and teardown so a wedged process can't stall the whole sweep.
SUBPROCESS_GRACE = 120

# How long rr gets to run its own backend teardown after SIGINT before we give up
# and kill it. See _terminate_rr.
CLEANUP_GRACE = 10


@dataclass
class RunRecord:
    case_id: str
    commit: str
    backend: str
    #: The model asked for, not the one that answered. rr's envelope reports the model
    #: *argument*, which is null on a default run because codex resolves its own default
    #: from ~/.codex/config.toml internally. The sweep therefore requires an explicit
    #: model per backend so this field always names something — see evals/README.md.
    requested_model: str
    run: int
    command: list[str]
    exit_code: int | None
    duration_s: float
    raw: str
    outcome: str
    errors: list[str]
    excerpt: str
    bare_json: bool
    backend_error: str | None
    started_at: str


def _tool_version(command: list[str]) -> str | None:
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


def _harness_rr_version() -> str | None:
    # The rocket-review importable *here*, which is also where REVIEW_SCHEMA comes from.
    # rr has no --version flag, so an --rr pointing at another environment cannot be
    # interrogated: for those runs this names the harness, not the thing under test.
    try:
        return version("rocket-review")
    except PackageNotFoundError:
        return None


def _backend_versions(backends: list[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in backends:
        binary = getattr(BACKENDS[name], "BINARY", None)
        versions[name] = _tool_version([binary, "--version"]) if binary else None
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


def _terminate_rr(proc: subprocess.Popen, deadline: int) -> tuple[str, str, str]:
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


def _extract_backend_result(stdout: str, backend: str) -> tuple[dict | None, str | None]:
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


def run_case(
    rr_command: list[str], repo: Path, commit: str, backend: str,
    requested_model: str, run: int, timeout: int,
) -> RunRecord:
    # --full is what makes this measurable: without it rr truncates raw at 4000 chars,
    # and a truncated review is unparsable by construction. `results[].raw` is the exact
    # string the runtime parser was handed, so validating it measures the backend rather
    # than the harness.
    command = [
        *rr_command,
        "--commit", commit,
        "--backend", f"{backend}:{requested_model}",
        "--json", "--full",
        "--timeout", str(timeout),
    ]
    case_id = f"{backend}:{commit}:r{run}"
    started_at = datetime.now(UTC).isoformat()
    start = time.monotonic()
    deadline = timeout + SUBPROCESS_GRACE
    exit_code: int | None = None
    stdout = ""
    stderr = ""
    backend_error: str | None = None
    try:
        proc = subprocess.Popen(
            command, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as e:
        backend_error = f"could not launch rr: {e}"
    else:
        try:
            stdout, stderr = proc.communicate(timeout=deadline)
        except subprocess.TimeoutExpired:
            stdout, stderr, backend_error = _terminate_rr(proc, deadline)
        exit_code = proc.returncode

    duration = round(time.monotonic() - start, 2)
    raw = ""
    if backend_error is None:
        result, envelope_error = _extract_backend_result(stdout, backend)
        if exit_code != 0:
            # Exit status is authoritative. rr prints its envelope before exiting 1 on a
            # failed backend, so stdout can look complete for a run that did not succeed;
            # scoring that as valid would contradict the taxonomy.
            detail = (result or {}).get("error") or stderr.strip()[:500] or envelope_error
            backend_error = f"rr exited {exit_code}" + (f": {detail}" if detail else "")
        elif result is None:
            backend_error = f"{envelope_error} (exit {exit_code}): {stderr.strip()[:500]}"
        elif result.get("error"):
            backend_error = str(result["error"])
        else:
            raw = result.get("raw") or ""

    common = {
        "case_id": case_id, "commit": commit, "backend": backend,
        "requested_model": requested_model, "run": run, "command": command,
        "exit_code": exit_code, "duration_s": duration, "raw": raw,
        "started_at": started_at,
    }
    if backend_error is not None:
        return RunRecord(
            **common, outcome=BACKEND_ERROR, errors=[], excerpt="", bare_json=False,
            backend_error=backend_error,
        )

    classification = classify_output(raw)
    return RunRecord(
        **common, outcome=classification.outcome, errors=classification.errors,
        excerpt=classification.excerpt, bare_json=classification.bare_json,
        backend_error=None,
    )


CELL = 16
WRAPPED_COLUMN = "fenced/wrapped"


def _count(rows: list[RunRecord], column: str) -> int:
    # Not an outcome: a fenced or prose-wrapped object still validates, so the outcome
    # counts alone would score a backend 100% compliant while it ignores the prompt's
    # "no prose before or after it, no markdown fence" instruction. It overlaps `valid`
    # and `schema_violation` by design, hence a separate column rather than a fifth
    # mutually exclusive bucket.
    #
    # Only runs that actually yielded a JSON object can be judged on how it was wrapped.
    # decode_failure and backend_error carry bare_json=False as a default rather than an
    # observation, so counting them here would report a refusal as a formatting problem.
    if column == WRAPPED_COLUMN:
        return sum(
            1 for r in rows
            if r.outcome in (VALID, SCHEMA_VIOLATION) and not r.bare_json
        )
    return sum(1 for r in rows if r.outcome == column)


def print_summary(records: list[RunRecord], backends: list[str]) -> None:
    width = max([len(b) for b in backends] + [len("backend")])
    columns = [*OUTCOMES, WRAPPED_COLUMN]
    header = "backend".ljust(width) + "  " + "  ".join(c.rjust(CELL) for c in columns) + "  total"
    print("\n" + header)
    print("-" * len(header))
    for backend in backends:
        rows = [r for r in records if r.backend == backend]
        counts = [str(_count(rows, c)).rjust(CELL) for c in columns]
        print(backend.ljust(width) + "  " + "  ".join(counts) + f"  {len(rows):>5}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="m0_sweep",
        description="Measure backend --json compliance with REVIEW_SCHEMA (spends real tokens).",
    )
    parser.add_argument(
        "--commits", default=",".join(DEFAULT_COMMITS),
        help="Comma-separated commit revisions to review",
    )
    parser.add_argument(
        "--backends", required=True, metavar="NAME:MODEL",
        help="Comma-separated backend specs, e.g. codex:gpt-5.6-sol,claude:sonnet. The "
             "model is mandatory: it is recorded as the run's requested model, and rr "
             "reports null for a backend left on its own default. One model per backend.",
    )
    parser.add_argument(
        "--runs", type=int, default=DEFAULT_RUNS,
        help="Repetitions per commit/backend pair (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help="Per-run timeout in seconds, passed through to rr (default: %(default)s)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help="Maximum parallel rr subprocesses (default: %(default)s)",
    )
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT,
        help="Repository the commits are reviewed in (default: this checkout)",
    )
    parser.add_argument(
        "--out", type=Path, default=RESULTS_DIR,
        help="Directory for the JSONL result file (default: evals/results, the only "
             "path gitignored for this — records embed full review text, so any other "
             "location inside the repo produces committable output)",
    )
    parser.add_argument(
        "--rr", default="rr",
        help="Command used to invoke rocket-review. Intended for stubs or another rr in "
             "this same environment: pointing it at a different installation leaves the "
             "recorded schema and version describing the harness, not what ran "
             "(default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if os.environ.get("CI"):
        print("Refusing to run: every case is a real, billed backend review.", file=sys.stderr)
        return 1
    for name, value in (("runs", args.runs), ("concurrency", args.concurrency),
                        ("timeout", args.timeout)):
        if value <= 0:
            print(f"Error: --{name} must be positive.", file=sys.stderr)
            return 1

    rr_command = shlex.split(args.rr)
    commits = [c.strip() for c in args.commits.split(",") if c.strip()]
    if not commits:
        print("Error: --commits must name at least one revision.", file=sys.stderr)
        return 1
    specs, spec_error = parse_backend_specs(args.backends)
    if spec_error:
        print(f"Error: {spec_error}", file=sys.stderr)
        return 1
    if not args.repo.is_dir():
        print(f"Error: --repo {args.repo} is not a directory.", file=sys.stderr)
        return 1

    backends = [name for name, _ in specs]
    cases = [
        (commit, name, model, run)
        for name, model in specs
        for commit in commits
        for run in range(1, args.runs + 1)
    ]

    args.out.mkdir(parents=True, exist_ok=True)
    # Microseconds plus a random suffix, opened "x": two sweeps started in the same
    # second must not truncate each other's (expensive, unrepeatable) results.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    out_path = args.out / f"sweep-{stamp}-{uuid.uuid4().hex[:8]}.jsonl"
    header = {
        "type": "header",
        "started_at": datetime.now(UTC).isoformat(),
        # Named for what it is: the rocket-review in the harness environment, which is
        # also the source of the REVIEW_SCHEMA being validated against. It describes the
        # executed rr only when --rr points into this same environment.
        "harness_rr_version": _harness_rr_version(),
        "rr_command": rr_command,
        "backend_specs": dict(specs),
        "backend_versions": _backend_versions(backends),
        "repo": str(args.repo.resolve()),
        "commits": commits,
        "runs": args.runs,
        "timeout": args.timeout,
        "concurrency": args.concurrency,
        "cases_total": len(cases),
    }

    print(f"{len(cases)} case(s) -> {out_path}", file=sys.stderr)
    records: list[RunRecord] = []
    write_lock = Lock()
    with out_path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        fh.flush()

        def execute(case: tuple[str, str, str, int]) -> None:
            commit, backend, model, run = case
            record = run_case(rr_command, args.repo, commit, backend, model, run, args.timeout)
            # Flush per record: a sweep is long and expensive, so an interrupt partway
            # through must not cost the runs that already completed.
            with write_lock:
                fh.write(json.dumps(asdict(record)) + "\n")
                fh.flush()
                # Retain only what the summary counts. Full review text is already on
                # disk, and holding every raw response would grow with the sweep.
                records.append(replace(record, raw="", errors=[], excerpt=""))
                print(f"[{len(records)}/{len(cases)}] {record.case_id} "
                      f"{record.outcome} ({record.duration_s}s)", file=sys.stderr)

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            list(pool.map(execute, cases))

    print_summary(records, backends)
    print(f"\nresults: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
