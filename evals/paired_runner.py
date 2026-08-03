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
import subprocess
import sys
import tempfile
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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
    verify_repo_commits,
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
    "run treatment then control (C,T,T,C,C,T,...) within each case/backend. Both runs of a "
    "repetition are scheduled together and the next repetition of that case/backend starts "
    "only after both have finished, so the ordering is a property of the run rather than of "
    "how the thread pool happened to interleave. Different cases still run concurrently."
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

    #: Identifies this sweep. Two sessions must never pool: the whole design rests on both
    #: arms having faced the same conditions, which is only true within one run.
    sweep_id: str
    #: rocket-review as importable by the *harness* — the code in this checkout.
    harness_rr_version: str | None
    #: rocket-review as importable by the interpreter that actually ran the reviews. With
    #: --python pointing elsewhere these differ, and the second one is the thing measured.
    runtime_rr_version: str | None
    runtime_rr_path: str | None
    #: HEAD of the harness checkout. The PyPI version is constant across source commits, so
    #: it alone cannot say which prompts and which harness code a result came from.
    harness_commit: str | None
    backend_versions: dict[str, str | None]


def resolve_interpreter(python: str) -> tuple[str | None, str | None]:
    """Turn --python into an absolute path. Returns (path, error).

    Absolute but deliberately *not* symlink-resolved: a virtualenv's `bin/python` is a
    symlink to the base interpreter, and following it lands on a Python that cannot import
    the rocket_review the venv installed. Only the leading directory matters here — the
    runs happen with cwd set to a case's worktree, so a relative path would be looked up
    somewhere this process never was.
    """
    found = shutil.which(python)
    if found:
        return os.path.abspath(found), None
    candidate = Path(python).expanduser()
    if candidate.is_file():
        return os.path.abspath(candidate), None
    return None, f"--python {python!r} is not on PATH and is not a file"


def probe_runtime(python: str) -> tuple[str | None, str | None]:
    """Ask the selected interpreter which rocket_review it would import.

    Not the harness's own: `--python` can point at another environment entirely, and it is
    that one whose prompts get patched and whose reviews are being measured.
    """
    code = (
        "import json, rocket_review\n"
        "try:\n"
        "    from importlib.metadata import version\n"
        "    v = version('rocket-review')\n"
        "except Exception:\n"
        "    v = None\n"
        "print(json.dumps([v, rocket_review.__file__]))"
    )
    try:
        proc = subprocess.run(
            [python, "-c", code], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if proc.returncode != 0:
        return None, None
    try:
        version, path = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None, None
    return version, path


def harness_head_commit() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


@dataclass
class PairedRecord:
    #: The sweep this run belongs to. Rows from two sweeps must never be treated as one:
    #: they are the same keys describing different sessions. See tier1's --allow-multiple-sweeps.
    sweep_id: str
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
    #: The rocket-review that actually ran this review, and the harness commit it ran from.
    runtime_rr_version: str | None
    harness_commit: str | None
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


@dataclass(frozen=True)
class RepGroup:
    """One repetition of one case on one backend: the control run and the treatment run.

    The unit of scheduling, not the individual run. Both arms of a repetition go to the
    executor together and the next repetition of that case/backend waits for both, which
    is what makes "the two arms faced the same conditions" a property of the run rather
    than a hope about how the thread pool happened to interleave.
    """

    #: (case id, backend) — the sequence this group belongs to. Reps within a stream are
    #: strictly ordered; different streams run concurrently.
    stream: tuple[str, str]
    rep: int
    tasks: tuple[Task, ...]


def build_rep_groups(
    cases: list[Case], staged: dict[str, Materialized],
    specs: list[tuple[str, str]], control: Arm, treatment: Arm, runs: int,
) -> list[RepGroup]:
    """Lay out every repetition in interleaved order. See ALTERNATION_SCHEME."""
    groups: list[RepGroup] = []
    for case in cases:
        for backend, model in specs:
            order_index = 0
            for rep in range(1, runs + 1):
                pair = (
                    [(control, CONTROL), (treatment, TREATMENT)] if rep % 2
                    else [(treatment, TREATMENT), (control, CONTROL)]
                )
                tasks = []
                for arm, role in pair:
                    tasks.append(Task(
                        case=case, materialized=staged[case.id], backend=backend,
                        model=model, arm=arm, role=role, rep=rep,
                        order_index=order_index,
                    ))
                    order_index += 1
                groups.append(
                    RepGroup(stream=(case.id, backend), rep=rep, tasks=tuple(tasks))
                )
    return groups


def build_tasks(
    cases: list[Case], staged: dict[str, Materialized],
    specs: list[tuple[str, str]], control: Arm, treatment: Arm, runs: int,
) -> list[Task]:
    """Every run in interleaved order, flattened out of its repetition groups."""
    return [
        task
        for group in build_rep_groups(cases, staged, specs, control, treatment, runs)
        for task in group.tasks
    ]


def run_groups(
    groups: list[RepGroup], concurrency: int, execute: Callable[[Task], None],
) -> None:
    """Run repetition groups, submitting only what can start now.

    Two things this does that `executor.map` cannot.

    Ordering: a stream's next repetition is submitted only once both runs of the previous
    one have landed. Handing the whole sweep to the pool up front means a fast arm races
    ahead — two runs of the same arm overlap, and a repetition's control and treatment can
    end up minutes and several other runs apart, which is exactly the confound the pairing
    exists to remove.

    Cancellation: every queued task is a billed backend review. `map` enqueues all of them,
    so a Ctrl-C or a worker exception still launches whatever the workers pick up next. Here
    at most `concurrency // 2` repetitions are ever in flight, and an interrupt cancels the
    rest instead of paying for them.
    """
    streams: dict[tuple[str, str], deque[RepGroup]] = {}
    for group in groups:
        streams.setdefault(group.stream, deque()).append(group)
    # Streams with nothing in flight, in first-seen order.
    ready = deque(streams)
    # A group is two runs, so concurrency is spent two at a time; one group always runs
    # even at --concurrency 1, where its two runs simply take the single worker in turn.
    max_groups = max(1, concurrency // 2)

    pool = ThreadPoolExecutor(max_workers=concurrency)
    in_flight: dict[Future, tuple[str, str]] = {}
    outstanding: dict[tuple[str, str], int] = {}
    try:
        while ready or in_flight:
            while ready and len(outstanding) < max_groups:
                key = ready.popleft()
                group = streams[key].popleft()
                outstanding[key] = len(group.tasks)
                for task in group.tasks:
                    in_flight[pool.submit(execute, task)] = key
            done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
            for future in done:
                key = in_flight.pop(future)
                future.result()  # re-raise a worker's exception here, not silently
                outstanding[key] -= 1
                if outstanding[key] == 0:
                    del outstanding[key]
                    if streams[key]:
                        ready.append(key)
    except BaseException:
        # Nothing queued gets to start. Cancelling the futures is what stops the units the
        # workers have not picked up yet; runs already inside a worker are bounded by their
        # own timeout, and rr tears its own backend down when it is interrupted.
        for future in in_flight:
            future.cancel()
        # wait=True so the runs still executing can finish writing their rows. The caller
        # records results inside an open file that closes as soon as this exception
        # propagates, so returning early would strand up to `concurrency` already-billed
        # runs mid-write and lose them. It costs no wall time either: the pool's threads
        # are non-daemon, so the interpreter joins them at exit regardless.
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown()


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
        # Hermetic: whoever runs the sweep may have an rr config file, and `docs` would
        # change the prompt, `effort` the reasoning budget, `fail_on` the exit code this
        # runner treats as authoritative. The results would still look clean and would
        # quietly mean something else.
        "--no-config",
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
        "sweep_id": provenance.sweep_id,
        "case_id": task.case.id, "mode": task.case.mode, "source": task.case.source,
        "repo_commit": task.case.repo_commit,
        "case_is_control": task.case.is_control, "arm": task.arm.name,
        "arm_role": task.role, "arm_hash": task.arm.content_hash,
        "backend": task.backend, "requested_model": task.model,
        "backend_version": provenance.backend_versions.get(task.backend),
        "harness_rr_version": provenance.harness_rr_version,
        "runtime_rr_version": provenance.runtime_rr_version,
        "harness_commit": provenance.harness_commit, "rep": task.rep,
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
    # Rows are selected by role, not by arm name: in an A/A run both roles carry the same
    # name, and selecting on it would count every run twice and print the same line twice.
    roles = [(CONTROL, arms[0]), (TREATMENT, arms[1])]
    labels = {
        (backend, role): f"{backend} / {role} ({arm.name})"
        for backend in backends for role, arm in roles
    }
    label_width = max([len(v) for v in labels.values()] + [len("backend / arm")])
    header = (
        "backend / arm".ljust(label_width) + "  "
        + "  ".join(c.rjust(CELL) for c in OUTCOMES) + "  total"
    )
    print("\n" + header)
    print("-" * len(header))
    for backend in backends:
        for role, _ in roles:
            rows = [
                r for r in records
                if r.backend == backend and r.arm_role == role and _is_final(r, records)
            ]
            counts = [
                str(sum(1 for r in rows if r.outcome == outcome)).rjust(CELL)
                for outcome in OUTCOMES
            ]
            print(labels[(backend, role)].ljust(label_width) + "  "
                  + "  ".join(counts) + f"  {len(rows):>5}")


def _unit_key(record: PairedRecord) -> tuple:
    # Role included: an A/A run puts the same arm name on both roles, so keying without it
    # would merge each pair of runs into one unit and under-count the sweep's progress.
    return (record.case_id, record.backend, record.arm, record.arm_role, record.rep)


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
    # Resolved once, here: the runs happen with cwd set to a case's worktree, so a
    # relative interpreter path would be looked up somewhere this process never was.
    python, python_error = resolve_interpreter(args.python)
    if python_error:
        print(f"Error: {python_error}", file=sys.stderr)
        return 1
    args.python = python
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
        # Before any worktree is built or any token is spent: every manifest must name a
        # commit this repository actually has.
        verify_repo_commits(cases, args.repo)
        for case in cases:
            staged[case.id] = materialize(case, args.repo, workdir)
        groups = build_rep_groups(cases, staged, specs, control, treatment, args.runs)
        runtime_version, runtime_path = probe_runtime(args.python)
        provenance = Provenance(
            sweep_id=uuid.uuid4().hex,
            harness_rr_version=harness_rr_version(),
            runtime_rr_version=runtime_version,
            runtime_rr_path=runtime_path,
            harness_commit=harness_head_commit(),
            backend_versions=backend_versions(backends),
        )
        return _execute(args, groups, arms, backends, specs, cases, provenance)
    except CaseError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        for staged_case in staged.values():
            if staged_case.worktree:
                remove_worktree(args.repo, staged_case.worktree)
        shutil.rmtree(workdir, ignore_errors=True)


def _execute(
    args: argparse.Namespace, groups: list[RepGroup], arms: list[Arm],
    backends: list[str], specs: list[tuple[str, str]], cases: list[Case],
    provenance: Provenance,
) -> int:
    tasks = [task for group in groups for task in group.tasks]
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = new_results_path(args.out, "paired")
    header = {
        "type": "header",
        "sweep_id": provenance.sweep_id,
        "started_at": datetime.now(UTC).isoformat(),
        # Two rocket-reviews, because --python can select a different one: the harness's
        # own, and the one that actually ran the reviews. The harness commit disambiguates
        # what the version string cannot — the released version is constant across many
        # source commits, including every change to the prompts under test.
        "harness_rr_version": provenance.harness_rr_version,
        "harness_commit": provenance.harness_commit,
        "runtime_rr_version": provenance.runtime_rr_version,
        "runtime_rr_path": provenance.runtime_rr_path,
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
             "case_is_control": c.is_control}
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

        try:
            run_groups(groups, args.concurrency, execute)
        except BaseException:
            # Every exit here is expensive — a Ctrl-C or a harness bug partway through a
            # billed sweep — and the runs that did complete are already on disk. Say so
            # before the traceback, whichever way it ended.
            print(
                f"\nStopped early: no further units will start. Everything that finished "
                f"is already in {out_path}.",
                file=sys.stderr,
            )
            raise

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
