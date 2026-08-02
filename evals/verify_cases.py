"""Prove that every defect mutant in the corpus is one the test suite actually catches.

A mutant is only worth measuring recall on if it is a *real* defect. The check is
mechanical, not editorial: check out `repo_commit` in a throwaway worktree, apply the
case's patch, run the project's own test suite, and see whether anything fails. A mutant
no test distinguishes from the original is an equivalent mutant — the code still behaves
correctly — and scoring a reviewer on finding it would measure nothing.

Two runs make the proof, and both are needed:

- the **baseline** at `repo_commit` with no patch, which must be fully green. Against a red
  baseline every "kill" could be the pre-existing failure, so the run stops there.
- the **patched** run per case, whose failures are that case's `killed_by`.

This is developer tooling, run on demand. It creates a git worktree and runs the whole
suite per case, so it is minutes of subprocesses — deliberately not a CI gate. What CI does
check is cheap and static: `test_cases.py` asserts every mutant manifest *carries* a
`killed_by`, this script is what puts a true one there.

    python evals/verify_cases.py                      # check the committed corpus
    python evals/verify_cases.py --write              # record what killed each mutant
    python evals/verify_cases.py --cases /tmp/cand/cases --write   # triage candidates
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import (  # noqa: E402
    CASES_DIR,
    Case,
    CaseError,
    load_cases,
    materialize,
    remove_worktree,
)
from eval_common import REPO_ROOT  # noqa: E402

#: The project's own suite, run through uv so the worktree gets the `dev` extra without a
#: pre-existing venv. Overridable because a corpus can live against another project.
DEFAULT_PYTEST_CMD = [
    "uv", "run", "--extra", "dev", "pytest", "-q", "--tb=no", "-rfE",
    # The worktree is thrown away; leaving a cache directory in it only slows teardown.
    "-p", "no:cacheprovider",
]

#: 45 minutes: the suite takes well under a minute, but a mutant can hang a test that a
#: subprocess or a wait loop drives, and that must end as a reported failure, not a wedge.
SUITE_TIMEOUT = 2700

DEFAULT_JOBS = 4

KILLED = "killed"
SURVIVED = "survived"
MISMATCH = "mismatch"
ERROR = "error"

#: pytest's `-rfE` short summary: one `FAILED <nodeid> - <reason>` line per failing node.
_SUMMARY_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(.+)$")

#: `killed_by` is written as the manifest's last key so this can rewrite it by truncating
#: from that line — PyYAML round-tripping would drop every comment in the file.
_KILLED_BY_MARKER = "killed_by:"

_GENERATED_HEADER = (
    "# Admission proof: these tests pass at repo_commit and fail with `diff` applied.\n"
    "# Written by `python evals/verify_cases.py --write`; re-run it rather than editing.\n"
)


class VerifyError(Exception):
    """The verification could not be carried out, so it proves nothing either way."""


@dataclass(frozen=True)
class SuiteRun:
    failing: tuple[str, ...]
    exit_code: int
    #: Kept for the failure paths; a suite that could not run at all has to say why.
    output: str


@dataclass(frozen=True)
class Verdict:
    case_id: str
    status: str
    observed: tuple[str, ...]
    recorded: tuple[str, ...]
    detail: str = ""


def node_id(summary_tail: str) -> str:
    """Strip pytest's ` - <reason>` tail without cutting a parametrized id in half.

    A parameter can itself contain " - " (`test_x[60-1 minute]`), so the split point is
    the first separator at bracket depth zero rather than the first one in the line.
    """
    depth = 0
    for i, ch in enumerate(summary_tail):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif depth == 0 and summary_tail.startswith(" - ", i):
            return summary_tail[:i]
    return summary_tail.rstrip()


def run_suite(cwd: Path, pytest_cmd: list[str]) -> SuiteRun:
    """Run the suite in `cwd` and return the node ids of everything that failed."""
    try:
        proc = subprocess.run(
            pytest_cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=SUITE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise VerifyError(f"the suite did not finish within {SUITE_TIMEOUT}s in {cwd}") from e
    except OSError as e:
        raise VerifyError(f"could not run {pytest_cmd[0]}: {e}") from e
    output = proc.stdout + proc.stderr
    failing = tuple(
        node_id(m.group(1))
        for line in output.splitlines() if (m := _SUMMARY_LINE.match(line))
    )
    return SuiteRun(failing=failing, exit_code=proc.returncode, output=output)


def check_baseline(repo: Path, commit: str, workdir: Path, pytest_cmd: list[str]) -> None:
    """Refuse to judge any mutant until the unpatched snapshot is green.

    A single red test at `repo_commit` would be counted as a kill for every case built on
    it, which is the one way this script could certify a corpus of equivalent mutants.
    """
    worktree = workdir / f"baseline-{commit[:12]}"
    added = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), commit],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if added.returncode != 0:
        raise VerifyError(f"could not check out {commit}: {added.stderr.strip()}")
    try:
        run = run_suite(worktree, pytest_cmd)
    finally:
        remove_worktree(repo, worktree)
    if run.exit_code != 0:
        failures = ", ".join(run.failing) or f"exit {run.exit_code}"
        raise VerifyError(
            f"the suite is not green at {commit[:12]} ({failures}); with a red baseline a "
            "failure under a patch proves nothing about the patch"
        )


def verify_case(
    case: Case, repo: Path, workdir: Path, pytest_cmd: list[str], stage_lock: Lock,
) -> Verdict:
    """Run the suite against one mutant and compare the result with its manifest."""
    # `git worktree add`/`remove` mutate repo-level admin state, so staging is serialized
    # even though the suite runs themselves are not.
    try:
        with stage_lock:
            staged = materialize(case, repo, workdir)
    except CaseError as e:
        # One unstageable case — a patch that no longer applies, most likely — is a fact
        # about that case. Letting it out of here would abandon every other case in the
        # run, which is the opposite of what a corpus-wide check is for.
        return Verdict(case.id, ERROR, (), case.killed_by, str(e))
    assert staged.worktree is not None
    try:
        run = run_suite(staged.worktree, pytest_cmd)
    finally:
        with stage_lock:
            remove_worktree(repo, staged.worktree)

    if not run.failing:
        if run.exit_code != 0:
            # No node-level failure but a non-zero exit: a collection error, an internal
            # pytest error, a crashed interpreter. Nothing here is evidence about the
            # mutant, so it must not be reported as either killed or survived.
            return Verdict(
                case.id, ERROR, (), case.killed_by,
                f"suite exited {run.exit_code} without naming a failing test: "
                f"{run.output.strip()[-400:]}",
            )
        return Verdict(case.id, SURVIVED, (), case.killed_by)
    if case.killed_by and set(case.killed_by) != set(run.failing):
        return Verdict(case.id, MISMATCH, run.failing, case.killed_by)
    return Verdict(case.id, KILLED, run.failing, case.killed_by)


def write_killed_by(case: Case, failing: tuple[str, ...]) -> None:
    """Record the killing tests as the manifest's trailing `killed_by` block.

    A line-level rewrite rather than a YAML round-trip: every manifest's comments explain
    why the case exists, and PyYAML would silently drop all of them.
    """
    lines = case.manifest_path.read_text(encoding="utf-8").splitlines(keepends=True)
    marker = next(
        (i for i, line in enumerate(lines) if line.startswith(_KILLED_BY_MARKER)), None
    )
    if marker is not None:
        # Truncating from the marker is only safe while nothing follows the block. A key
        # added after it would be silently deleted, so refuse rather than eat it.
        trailing = [
            line for line in lines[marker + 1:]
            if line.strip() and not line.startswith((" ", "-", "#"))
        ]
        if trailing:
            raise VerifyError(
                f"{case.manifest_path}: killed_by must be the last key — "
                f"{trailing[0].split(':')[0].strip()!r} follows it and would be lost"
            )
        lines = lines[:marker]
        header_lines = _GENERATED_HEADER.splitlines(keepends=True)
        # Only this script's own header is dropped, so a comment a human wrote just above
        # the block survives being rewritten.
        while lines and lines[-1] in header_lines:
            lines.pop()
    if lines and not lines[-1].endswith("\n"):
        lines.append("\n")
    block = _GENERATED_HEADER + "killed_by:\n" + "".join(f"  - {n}\n" for n in failing)
    case.manifest_path.write_text("".join(lines) + block, encoding="utf-8")


def print_report(verdicts: list[Verdict]) -> None:
    width = max((len(v.case_id) for v in verdicts), default=4)
    for v in sorted(verdicts, key=lambda v: v.case_id):
        head = f"{v.case_id:<{width}}  {v.status.upper():<9}"
        if v.status == SURVIVED:
            print(f"{head} no test distinguishes it — equivalent mutant, not admissible")
        elif v.status == MISMATCH:
            print(
                f"{head} manifest records {len(v.recorded)}, suite failed {len(v.observed)}"
            )
            print(f"{' ' * width}    recorded: {', '.join(v.recorded) or '(none)'}")
            print(f"{' ' * width}    observed: {', '.join(v.observed)}")
        elif v.status == ERROR:
            print(f"{head} {v.detail}")
        else:
            print(f"{head} {len(v.observed)} test(s): {', '.join(v.observed)}")

    counts = {
        status: sum(1 for v in verdicts if v.status == status)
        for status in (KILLED, SURVIVED, MISMATCH, ERROR)
    }
    print(
        f"\n{len(verdicts)} mutant(s): {counts[KILLED]} killed, {counts[SURVIVED]} survived, "
        f"{counts[MISMATCH]} mismatched, {counts[ERROR]} errored"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_cases.py",
        description="Prove each defect mutant is killed by the project's own test suite.",
    )
    parser.add_argument("--cases", type=Path, default=CASES_DIR, help="corpus directory")
    parser.add_argument("--only", help="comma-separated case ids to verify")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repository to check out")
    parser.add_argument(
        "--write", action="store_true",
        help="record the observed failures as each manifest's killed_by",
    )
    parser.add_argument(
        "--jobs", type=int, default=DEFAULT_JOBS, help="suite runs in parallel",
    )
    parser.add_argument(
        "--pytest", default=" ".join(DEFAULT_PYTEST_CMD),
        help="the suite command, run in each worktree",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if os.environ.get("CI"):
        # Minutes of worktrees and full suite runs per case. The manifest-integrity test
        # is what CI runs; this is the on-demand tool that makes those manifests true.
        print("verify_cases.py is developer tooling and does not run in CI.", file=sys.stderr)
        return 1
    if args.jobs < 1:
        print("Error: --jobs must be at least 1.", file=sys.stderr)
        return 1

    pytest_cmd = args.pytest.split()
    only = {c.strip() for c in args.only.split(",")} if args.only else None
    try:
        cases = [c for c in load_cases(args.cases, only) if c.source == "mutant"]
    except CaseError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not cases:
        print(f"Error: no mutant cases in {args.cases}.", file=sys.stderr)
        return 1

    repo = args.repo.resolve()
    stage_lock = Lock()
    with tempfile.TemporaryDirectory(prefix="rr-verify-") as tmp:
        workdir = Path(tmp)
        try:
            # Always, with no flag to turn it off: half the admission rule is "all pass
            # without the patch", and an operator switch to skip it is a switch that
            # certifies equivalent mutants.
            for commit in sorted({c.repo_commit for c in cases}):
                print(f"baseline: running the suite at {commit[:12]}", file=sys.stderr)
                check_baseline(repo, commit, workdir, pytest_cmd)
            print(f"{len(cases)} mutant(s) to verify", file=sys.stderr)
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                verdicts = list(pool.map(
                    lambda c: verify_case(c, repo, workdir, pytest_cmd, stage_lock), cases,
                ))
        except (CaseError, VerifyError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.write:
        by_id = {c.id: c for c in cases}
        try:
            for v in verdicts:
                if v.status in (KILLED, MISMATCH):
                    write_killed_by(by_id[v.case_id], v.observed)
        except VerifyError as e:
            # The verdicts are still worth printing: the runs happened, and only the
            # recording of them failed.
            print_report(verdicts)
            print(f"Error: {e}", file=sys.stderr)
            return 1

    print_report(verdicts)
    admitted = {KILLED} | ({MISMATCH} if args.write else set())
    return 0 if all(v.status in admitted for v in verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
