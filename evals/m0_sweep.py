"""Measure how often backends really produce `REVIEW_SCHEMA`-compliant `--json` output.

Runs `rr --commit <sha> --json --full` repeatedly across a set of commits and backends,
stores one JSONL record per run under `evals/results/`, and prints per-backend counts of
valid / schema_violation / decode_failure / backend_error.

Every run is a real backend review and costs real tokens, so this is a deliberate,
manual, offline measurement — never a CI gate. See evals/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
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

DEFAULT_BACKENDS = ["codex"]
DEFAULT_RUNS = 3
DEFAULT_TIMEOUT = 900
DEFAULT_CONCURRENCY = 3

# rr already bounds the backend with --timeout; this only covers rr's own startup,
# git preflight and teardown so a wedged process can't stall the whole sweep.
SUBPROCESS_GRACE = 120


@dataclass
class RunRecord:
    case_id: str
    commit: str
    backend: str
    #: Resolved model as the envelope reports it. The backend CLI version in the sweep
    #: header does not pin this — codex honours the user's ~/.codex/config.toml default,
    #: so two sweeps from the same CLI can measure different models.
    model: str | None
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


def _rr_version() -> str | None:
    # rr has no --version flag, so this reports the importable rocket-review
    # distribution — which is the same install in the normal `uv pip install -e .` setup
    # but can diverge if --rr points at a different environment.
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


def _extract_backend_result(stdout: str, backend: str) -> tuple[dict | None, str | None]:
    """Pull this backend's entry out of an rr --json envelope.

    Returns (result, envelope_error): envelope_error is set when the envelope itself
    is unusable, which is a harness problem rather than a backend format problem.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "rr did not emit a JSON envelope"
    for result in envelope.get("results", []):
        if result.get("backend") == backend:
            return result, None
    return None, f"no {backend} entry in rr envelope"


def run_case(
    rr_command: list[str], repo: Path, commit: str, backend: str, run: int, timeout: int
) -> RunRecord:
    # --full is what makes this measurable: without it rr truncates raw at 4000 chars,
    # and a truncated review is unparsable by construction. `results[].raw` is the exact
    # string the runtime parser was handed, so validating it measures the backend rather
    # than the harness.
    command = [
        *rr_command,
        "--commit", commit,
        "--backend", backend,
        "--json", "--full",
        "--timeout", str(timeout),
    ]
    case_id = f"{backend}:{commit}:r{run}"
    started_at = datetime.now(UTC).isoformat()
    start = time.monotonic()
    exit_code: int | None = None
    stdout = ""
    stderr = ""
    backend_error: str | None = None
    try:
        proc = subprocess.run(
            command, cwd=repo, capture_output=True, text=True,
            errors="replace", timeout=timeout + SUBPROCESS_GRACE,
        )
        exit_code = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        backend_error = f"rr exceeded the harness timeout ({timeout + SUBPROCESS_GRACE}s)"
    except OSError as e:
        backend_error = f"could not launch rr: {e}"

    duration = round(time.monotonic() - start, 2)
    raw = ""
    model: str | None = None
    if backend_error is None:
        result, envelope_error = _extract_backend_result(stdout, backend)
        if result is None:
            # rr failed before it could emit an envelope, so its stderr is the only
            # evidence of why.
            backend_error = f"{envelope_error} (exit {exit_code}): {stderr.strip()[:500]}"
        else:
            model = result.get("model")
            if result.get("error"):
                backend_error = str(result["error"])
            else:
                raw = result.get("raw") or ""

    if backend_error is not None:
        return RunRecord(
            case_id=case_id, commit=commit, backend=backend, model=model, run=run,
            command=command, exit_code=exit_code, duration_s=duration, raw=raw,
            outcome=BACKEND_ERROR, errors=[], excerpt="", bare_json=False,
            backend_error=backend_error, started_at=started_at,
        )

    classification = classify_output(raw)
    return RunRecord(
        case_id=case_id, commit=commit, backend=backend, model=model, run=run,
        command=command, exit_code=exit_code, duration_s=duration, raw=raw,
        outcome=classification.outcome, errors=classification.errors,
        excerpt=classification.excerpt, bare_json=classification.bare_json,
        backend_error=None, started_at=started_at,
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
        "--backends", default=",".join(DEFAULT_BACKENDS),
        help="Comma-separated rr backends",
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
        help="Command used to invoke rocket-review; overridable to test the harness "
             "against a stub instead of a real backend (default: %(default)s)",
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
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    if not commits or not backends:
        print("Error: --commits and --backends must each name at least one value.",
              file=sys.stderr)
        return 1
    unknown = [b for b in backends if b not in BACKENDS]
    if unknown:
        print(f"Error: unknown backend(s) {', '.join(unknown)}. "
              f"Available: {', '.join(BACKENDS)}.", file=sys.stderr)
        return 1
    if not args.repo.is_dir():
        print(f"Error: --repo {args.repo} is not a directory.", file=sys.stderr)
        return 1

    cases = [
        (commit, backend, run)
        for backend in backends
        for commit in commits
        for run in range(1, args.runs + 1)
    ]

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.out / f"sweep-{stamp}.jsonl"
    header = {
        "type": "header",
        "started_at": datetime.now(UTC).isoformat(),
        "rr_version": _rr_version(),
        "rr_command": rr_command,
        "backend_versions": _backend_versions(backends),
        "repo": str(args.repo.resolve()),
        "commits": commits,
        "backends": backends,
        "runs": args.runs,
        "timeout": args.timeout,
        "concurrency": args.concurrency,
        "cases_total": len(cases),
    }

    print(f"{len(cases)} case(s) -> {out_path}", file=sys.stderr)
    records: list[RunRecord] = []
    write_lock = Lock()
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        fh.flush()

        def execute(case: tuple[str, str, int]) -> RunRecord:
            commit, backend, run = case
            record = run_case(rr_command, args.repo, commit, backend, run, args.timeout)
            # Flush per record: a sweep is long and expensive, so an interrupt partway
            # through must not cost the runs that already completed.
            with write_lock:
                fh.write(json.dumps(asdict(record)) + "\n")
                fh.flush()
                records.append(record)
                print(f"[{len(records)}/{len(cases)}] {record.case_id} "
                      f"{record.outcome} ({record.duration_s}s)", file=sys.stderr)
            return record

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            list(pool.map(execute, cases))

    print_summary(records, backends)
    print(f"\nresults: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
