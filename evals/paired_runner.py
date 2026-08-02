"""Compare two prompt arms on identical cases, paired and interleaved in one session.

Prompt edits are easy to make and hard to verify. This runner answers one question: did a
prompt change make `rr` sharper, or just noisier? Both arms see the same cases, the same
backend, the same model and the same session — never one arm today and the other next week,
which would confound the prompt change with everything else that moved in between.

Arms are injected by `rr_arm_launcher.py`, one process per run; nothing in
`rocket_review/` knows an eval is happening. Results land as JSONL under `evals/results/`
and are scored offline by `tier1.py`. The decision rules that turn those numbers into a
ship/don't-ship call are pre-registered in `evals/README.md`.

Every run is a real, billed backend review.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arms import Arm, ArmError, load_arm  # noqa: E402
from cases import (  # noqa: E402
    CASES_DIR,
    Case,
    CaseError,
    Materialized,
    load_cases,
    materialize,
    remove_worktree,
)
from eval_common import (  # noqa: E402
    REPO_ROOT,
    RESULTS_DIR,
    SUBPROCESS_GRACE,
    backend_versions,
    extract_backend_result,
    harness_rr_version,
    invoke_rr,
    new_results_path,
    parse_backend_specs,
)
from rr_arm_launcher import ARM_ENV  # noqa: E402
from strict_validator import BACKEND_ERROR, OUTCOMES, classify_output  # noqa: E402

LAUNCHER = Path(__file__).resolve().parent / "rr_arm_launcher.py"

DEFAULT_RUNS = 3
DEFAULT_TIMEOUT = 900
# One slot per arm by default, so a repetition's control and treatment run against the
# same backend conditions rather than minutes apart.
DEFAULT_CONCURRENCY = 2

CONTROL = "control"
TREATMENT = "treatment"

# Stated here and echoed into every result file's header, because the pairing is the whole
# design and a reader of raw JSONL has no other way to recover it.
ALTERNATION_SCHEME = (
    "per-repetition toggle: odd repetitions run control then treatment, even repetitions "
    "run treatment then control (C,T,T,C,C,T,...) within each case/backend"
)

#: One retry per failed run. A backend that fails twice in a row on the same case is a
#: fact about that case, not a blip, and inventing more attempts would quietly weight
#: flaky cases more heavily than reliable ones.
MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class Provenance:
    """Versions of the things doing the reviewing, recorded on every row.

    Also in the header, but rows get filtered, concatenated and compared across files, and
    a row that cannot say which rocket-review and which backend build produced it is not
    evidence of anything.
    """

    harness_rr_version: str | None
    backend_versions: dict[str, str | None]


@dataclass
class PairedRecord:
    case_id: str
    mode: str
    source: str
    #: The snapshot the case is defined against. tier1 resolves cited files at this commit.
    repo_commit: str
    #: Which decision rule governs this row: the veto is written per clean-control
    #: case. On the row, not only in the header, so a filtered or concatenated
    #: results file stays readable.
    case_is_control: bool
    arm: str
    arm_role: str
    #: sha256 of the arm's prompt text. The row's link back to exact prompt bytes.
    arm_hash: str
    backend: str
    #: The model asked for, not the one that answered — see evals/README.md.
    requested_model: str
    backend_version: str | None
    harness_rr_version: str | None
    rep: int
    #: Position in this case/backend's interleaved sequence, so the alternation can be
    #: checked after the fact rather than taken on trust.
    order_index: int
    attempt: int
    command: list[str]
    cwd: str
    exit_code: int | None
    duration_s: float
    raw: str
    outcome: str
    errors: list[str]
    excerpt: str
    bare_json: bool
    backend_error: str | None
    started_at: str


@dataclass(frozen=True)
class Task:
    case: Case
    materialized: Materialized
    backend: str
    model: str
    arm: Arm
    role: str
    rep: int
    order_index: int


def build_tasks(
    cases: list[Case], staged: dict[str, Materialized],
    specs: list[tuple[str, str]], control: Arm, treatment: Arm, runs: int,
) -> list[Task]:
    """Lay out every run in interleaved order. See ALTERNATION_SCHEME."""
    tasks: list[Task] = []
    for case in cases:
        for backend, model in specs:
            order_index = 0
            for rep in range(1, runs + 1):
                pair = (
                    [(control, CONTROL), (treatment, TREATMENT)] if rep % 2
                    else [(treatment, TREATMENT), (control, CONTROL)]
                )
                for arm, role in pair:
                    tasks.append(Task(
                        case=case, materialized=staged[case.id], backend=backend,
                        model=model, arm=arm, role=role, rep=rep,
                        order_index=order_index,
                    ))
                    order_index += 1
    return tasks


def build_command(task: Task, python: str, timeout: int) -> list[str]:
    return [
        python, str(LAUNCHER),
        *task.materialized.rr_args,
        # Declared rather than auto-detected: the manifest's mode decides which prompt
        # constant the arm is actually being measured on.
        "--mode", task.case.mode,
        "--backend", f"{task.backend}:{task.model}",
        # --full is what makes this measurable: without it rr truncates raw at 4000 chars
        # and a truncated review is unparsable by construction.
        "--json", "--full",
        "--timeout", str(timeout),
    ]


def run_attempt(
    task: Task, provenance: Provenance, python: str, timeout: int, attempt: int,
) -> PairedRecord:
    command = build_command(task, python, timeout)
    env = os.environ.copy()
    env[ARM_ENV] = str(task.arm.path)
    invocation = invoke_rr(command, task.materialized.cwd, timeout + SUBPROCESS_GRACE, env)

    exit_code = invocation.exit_code
    backend_error = invocation.harness_error
    raw = ""
    if backend_error is None:
        result, envelope_error = extract_backend_result(invocation.stdout, task.backend)
        if exit_code != 0:
            # Exit status is authoritative: rr prints its envelope before exiting non-zero
            # on a failed backend, so stdout alone can look like a clean run.
            detail = (
                (result or {}).get("error")
                or invocation.stderr.strip()[:500]
                or envelope_error
            )
            backend_error = f"rr exited {exit_code}" + (f": {detail}" if detail else "")
        elif result is None:
            backend_error = (
                f"{envelope_error} (exit {exit_code}): {invocation.stderr.strip()[:500]}"
            )
        elif result.get("error"):
            backend_error = str(result["error"])
        else:
            raw = result.get("raw") or ""

    common = {
        "case_id": task.case.id, "mode": task.case.mode, "source": task.case.source,
        "repo_commit": task.case.repo_commit,
        "case_is_control": task.case.is_control, "arm": task.arm.name,
        "arm_role": task.role, "arm_hash": task.arm.content_hash,
        "backend": task.backend, "requested_model": task.model,
        "backend_version": provenance.backend_versions.get(task.backend),
        "harness_rr_version": provenance.harness_rr_version, "rep": task.rep,
        "order_index": task.order_index, "attempt": attempt, "command": command,
        "cwd": str(task.materialized.cwd), "exit_code": exit_code,
        "duration_s": invocation.duration_s, "raw": raw,
        "started_at": invocation.started_at,
    }
    if backend_error is not None:
        return PairedRecord(
            **common, outcome=BACKEND_ERROR, errors=[], excerpt="", bare_json=False,
            backend_error=backend_error,
        )
    classification = classify_output(raw)
    return PairedRecord(
        **common, outcome=classification.outcome, errors=classification.errors,
        excerpt=classification.excerpt, bare_json=classification.bare_json,
        backend_error=None,
    )


def run_task(
    task: Task, provenance: Provenance, python: str, timeout: int,
) -> list[PairedRecord]:
    """Run one unit, retrying a failure once. Every attempt is recorded, none replaced.

    A dropped attempt would make the failure rate unrecoverable from the results file;
    tier1 scores the last attempt per unit and reports the rest as what they were.
    """
    records = [run_attempt(task, provenance, python, timeout, attempt=1)]
    while records[-1].outcome == BACKEND_ERROR and len(records) < MAX_ATTEMPTS:
        records.append(
            run_attempt(task, provenance, python, timeout, attempt=len(records) + 1)
        )
    return records


CELL = 18


def print_summary(records: list[PairedRecord], arms: list[Arm], backends: list[str]) -> None:
    label_width = max(len(f"{b} / {a.name}") for b in backends for a in arms)
    label_width = max(label_width, len("backend / arm"))
    header = (
        "backend / arm".ljust(label_width) + "  "
        + "  ".join(c.rjust(CELL) for c in OUTCOMES) + "  total"
    )
    print("\n" + header)
    print("-" * len(header))
    for backend in backends:
        for arm in arms:
            rows = [
                r for r in records
                if r.backend == backend and r.arm == arm.name and _is_final(r, records)
            ]
            counts = [
                str(sum(1 for r in rows if r.outcome == outcome)).rjust(CELL)
                for outcome in OUTCOMES
            ]
            label = f"{backend} / {arm.name}".ljust(label_width)
            print(label + "  " + "  ".join(counts) + f"  {len(rows):>5}")


def _unit_key(record: PairedRecord) -> tuple:
    return (record.case_id, record.backend, record.arm, record.rep)


def _is_final(record: PairedRecord, records: list[PairedRecord]) -> bool:
    key = _unit_key(record)
    return record.attempt == max(r.attempt for r in records if _unit_key(r) == key)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="paired_runner",
        description="Compare two prompt arms on identical cases (spends real tokens).",
    )
    parser.add_argument(
        "--control", required=True, metavar="ARM",
        help="Arm treated as the baseline: a directory under evals/prompts/, or a path",
    )
    parser.add_argument(
        "--treatment", required=True, metavar="ARM",
        help="Arm under test. Naming the same arm as --control is a valid A/A run that "
             "measures the backend's own noise floor rather than a prompt change.",
    )
    parser.add_argument(
        "--backends", required=True, metavar="NAME:MODEL",
        help="Comma-separated backend specs, e.g. codex:gpt-5.6-sol. The model is "
             "mandatory: it is recorded as the run's requested model, and rr reports null "
             "for a backend left on its own default. One model per backend.",
    )
    parser.add_argument(
        "--cases", type=Path, default=CASES_DIR,
        help="Directory of case manifests (default: evals/cases)",
    )
    parser.add_argument(
        "--case-id", default=None, metavar="ID[,ID...]",
        help="Restrict the run to these case ids (default: every manifest)",
    )
    parser.add_argument(
        "--runs", type=int, default=DEFAULT_RUNS,
        help="Repetitions per case per arm (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help="Per-run timeout in seconds, passed through to rr (default: %(default)s)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help="Maximum parallel rr subprocesses (default: %(default)s, one per arm)",
    )
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT,
        help="Repository the cases are materialized in (default: this checkout)",
    )
    parser.add_argument(
        "--out", type=Path, default=RESULTS_DIR,
        help="Directory for the JSONL result file (default: evals/results, the only path "
             "gitignored for this — records embed full review text)",
    )
    parser.add_argument(
        "--python", default=sys.executable,
        help="Interpreter used to launch rr under an arm. Its rocket_review is what gets "
             "patched and measured, so there is no way to point this at an installation "
             "the prompts were not injected into (default: this interpreter)",
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

    specs, spec_error = parse_backend_specs(args.backends)
    if spec_error:
        print(f"Error: {spec_error}", file=sys.stderr)
        return 1
    if not args.repo.is_dir():
        print(f"Error: --repo {args.repo} is not a directory.", file=sys.stderr)
        return 1

    try:
        control = load_arm(args.control)
        treatment = load_arm(args.treatment)
    except ArmError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if control.content_hash == treatment.content_hash:
        print(
            f"Note: {control.name} and {treatment.name} have identical prompt text — this "
            "is an A/A run measuring run-to-run noise, not a prompt change.",
            file=sys.stderr,
        )

    only = (
        {c.strip() for c in args.case_id.split(",") if c.strip()}
        if args.case_id else None
    )
    try:
        cases = load_cases(args.cases, only)
    except CaseError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    arms = [control, treatment]
    backends = [name for name, _ in specs]
    workdir = Path(tempfile.mkdtemp(prefix="rr-eval-cases-"))
    staged: dict[str, Materialized] = {}
    try:
        # Materialized serially and up front: `git worktree add` mutates repo-level admin
        # state. One worktree per case then serves all of that case's runs, which is not
        # quite read-only sharing — the backends review under read-only sandboxes, but
        # `rr --diff` runs `git diff HEAD`, and git refreshes the index and takes
        # index.lock to do it. Two concurrent runs of the same case can therefore collide
        # on git's own writes with perfectly behaved backends. The retry absorbs it; a
        # worktree per run would not be worth the churn of hundreds of checkouts.
        for case in cases:
            staged[case.id] = materialize(case, args.repo, workdir)
        tasks = build_tasks(cases, staged, specs, control, treatment, args.runs)
        provenance = Provenance(
            harness_rr_version=harness_rr_version(),
            backend_versions=backend_versions(backends),
        )
        return _execute(args, tasks, arms, backends, specs, cases, provenance)
    except CaseError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        for staged_case in staged.values():
            if staged_case.worktree:
                remove_worktree(args.repo, staged_case.worktree)
        shutil.rmtree(workdir, ignore_errors=True)


def _execute(
    args: argparse.Namespace, tasks: list[Task], arms: list[Arm],
    backends: list[str], specs: list[tuple[str, str]], cases: list[Case],
    provenance: Provenance,
) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = new_results_path(args.out, "paired")
    header = {
        "type": "header",
        "started_at": datetime.now(UTC).isoformat(),
        # The rocket-review in this environment: with --python defaulting to this
        # interpreter, it is also the one whose prompts were patched and run.
        "harness_rr_version": provenance.harness_rr_version,
        "python": args.python,
        "launcher": str(LAUNCHER),
        "arms": {
            role: {"name": arm.name, "path": str(arm.path), "hash": arm.content_hash}
            for role, arm in ((CONTROL, arms[0]), (TREATMENT, arms[1]))
        },
        "backend_specs": dict(specs),
        "backend_versions": provenance.backend_versions,
        "repo": str(args.repo.resolve()),
        "cases": [
            {"id": c.id, "mode": c.mode, "source": c.source, "repo_commit": c.repo_commit,
             "control_case": c.is_control}
            for c in cases
        ],
        "runs": args.runs,
        "timeout": args.timeout,
        "concurrency": args.concurrency,
        "alternation": ALTERNATION_SCHEME,
        "max_attempts": MAX_ATTEMPTS,
        "units_total": len(tasks),
    }

    print(f"{len(tasks)} unit(s) -> {out_path}", file=sys.stderr)
    records: list[PairedRecord] = []
    write_lock = Lock()
    with out_path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        fh.flush()

        def execute(task: Task) -> None:
            produced = run_task(task, provenance, args.python, args.timeout)
            # Flush per unit: a sweep is long and expensive, so an interrupt partway
            # through must not cost the runs that already completed.
            with write_lock:
                for record in produced:
                    fh.write(json.dumps(asdict(record)) + "\n")
                fh.flush()
                # Retain only what the summary counts; the full review text is on disk.
                records.extend(
                    replace(r, raw="", errors=[], excerpt="") for r in produced
                )
                last = produced[-1]
                print(
                    f"[{len({_unit_key(r) for r in records})}/{len(tasks)}] "
                    f"{last.case_id} {last.arm}({last.arm_role}) rep{last.rep} "
                    f"{last.outcome} ({sum(r.duration_s for r in produced):.1f}s"
                    f"{', retried' if len(produced) > 1 else ''})",
                    file=sys.stderr,
                )

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            list(pool.map(execute, tasks))

    print_summary(records, arms, backends)
    failed = sum(1 for r in records if _is_final(r, records) and r.outcome == BACKEND_ERROR)
    if failed:
        print(
            f"\n{failed} unit(s) still failing after {MAX_ATTEMPTS} attempts; tier1 "
            "excludes them from metric denominators.",
            file=sys.stderr,
        )
    print(f"\nresults: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
