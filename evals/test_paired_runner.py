"""The paired runner, end to end against a stub backend binary. No model is ever called.

The load-bearing test here is `test_the_arms_prompt_text_reaches_the_backend`: a runner
that cannot actually vary the prompt would still produce a tidy JSONL file full of numbers
that mean nothing. Everything else builds on that proof.
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from threading import Lock

import pytest
import rocket_review.prompts as rr_prompts
from arms import PROMPT_CONSTANTS, load_arm
from cases import remove_worktree
from conftest import git
from paired_runner import (
    ALTERNATION_SCHEME,
    LAUNCHER,
    CONTROL,
    MAX_ATTEMPTS,
    TREATMENT,
    RepGroup,
    build_tasks,
    main,
    resolve_interpreter,
    run_groups,
)
from rr_arm_launcher import ARM_ENV
from strict_validator import BACKEND_ERROR, VALID
from tier1 import compute

#: Well-formed 40-hex oids that no repository has.
FAKE_OID = "abc1234000000000000000000000000000000000"
ABSENT_OID = "deadbeefbeefbeefbeefbeefbeefbeefbeefbeef"

MARKER = "MARKER-0d5f-{name}-ARM-{arm}"


def write_arm(directory: Path, arm: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in PROMPT_CONSTANTS:
        (directory / f"{name}.txt").write_text(
            MARKER.format(name=name, arm=arm) + "\n", encoding="utf-8"
        )
    return directory


@pytest.fixture
def arms(tmp_path) -> tuple[Path, Path]:
    return write_arm(tmp_path / "arms" / "alpha", "alpha"), write_arm(
        tmp_path / "arms" / "beta", "beta"
    )


@pytest.fixture
def corpus(tmp_path, git_repo, head_oid) -> Path:
    """A two-case corpus over the throwaway repo: one mutant, one clean merged commit."""
    cases = tmp_path / "corpus" / "cases"
    cases.mkdir(parents=True)
    git(git_repo, "checkout", "-q", head_oid, "--", "sample.py")
    source = (git_repo / "sample.py").read_text(encoding="utf-8")
    (git_repo / "sample.py").write_text(
        source.replace("    if not label:\n        return 0\n", ""), encoding="utf-8"
    )
    (cases / "m-001.patch").write_text(git(git_repo, "diff").stdout, encoding="utf-8")
    git(git_repo, "checkout", "-q", "--", "sample.py")

    (cases / "m-001.yaml").write_text(
        f"id: m-001\nmode: diff\nsource: mutant\ndiff: cases/m-001.patch\n"
        f"repo_commit: {head_oid}\n"
        "defect:\n  class: dropped-null-check\n  file: sample.py\n  span: [3, 4]\n"
        "  expected: the empty-label guard is gone\n",
        encoding="utf-8",
    )
    (cases / "c-001.yaml").write_text(
        f"id: c-001\nmode: diff\nsource: merged-pr\nrepo_commit: {head_oid}\n",
        encoding="utf-8",
    )
    return cases


# --- the injection proof ---------------------------------------------------------------


def test_the_arms_prompt_text_reaches_the_backend(tmp_path, git_repo, stub_backend, arms):
    alpha, _ = arms
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER), "--commit", "HEAD",
         "--backend", "codex:stub-model", "--json", "--full"],
        cwd=git_repo, env=stub_backend.env(**{ARM_ENV: str(alpha)}),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    prompts = stub_backend.captured_prompts()
    assert len(prompts) == 1
    captured = prompts[0]
    # The arm's diff and json-addendum text is what codex was handed...
    assert MARKER.format(name="DIFF_REVIEW_PROMPT", arm="alpha") in captured
    assert MARKER.format(name="JSON_OUTPUT_ADDENDUM", arm="alpha") in captured
    # ...in place of the live prompts, not alongside them.
    assert rr_prompts.DIFF_REVIEW_PROMPT.strip() not in captured
    assert rr_prompts.JSON_OUTPUT_ADDENDUM.strip() not in captured
    # Everything else about the run is still rr's own: the review source is assembled by
    # the CLI and reaches the backend unchanged.
    assert "git show" in captured

    envelope = json.loads(proc.stdout)
    assert envelope["results"][0]["backend"] == "codex"
    assert envelope["results"][0]["verdict"] == "approve"


def test_a_different_arm_produces_different_prompt_text(tmp_path, git_repo, stub_backend, arms):
    for arm in arms:
        subprocess.run(
            [sys.executable, str(LAUNCHER), "--commit", "HEAD",
             "--backend", "codex:stub-model", "--json", "--full"],
            cwd=git_repo, env=stub_backend.env(**{ARM_ENV: str(arm)}),
            capture_output=True, text=True, check=True,
        )
    captured = stub_backend.captured_prompts()
    assert len(captured) == 2
    alpha_seen = [MARKER.format(name="DIFF_REVIEW_PROMPT", arm="alpha") in c for c in captured]
    beta_seen = [MARKER.format(name="DIFF_REVIEW_PROMPT", arm="beta") in c for c in captured]
    assert sorted(alpha_seen) == [False, True]
    assert sorted(beta_seen) == [False, True]


def test_the_launcher_refuses_to_run_without_an_arm(git_repo, stub_backend):
    env = stub_backend.env()
    env.pop(ARM_ENV, None)
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER), "--commit", "HEAD", "--backend", "codex:stub"],
        cwd=git_repo, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert ARM_ENV in proc.stderr
    assert stub_backend.captured_prompts() == []


def test_the_launcher_refuses_a_malformed_arm(tmp_path, git_repo, stub_backend, arms):
    alpha, _ = arms
    (alpha / "DIFF_REVIEW_PROMPT.txt").unlink()
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER), "--commit", "HEAD", "--backend", "codex:stub"],
        cwd=git_repo, env=stub_backend.env(**{ARM_ENV: str(alpha)}),
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "DIFF_REVIEW_PROMPT.txt" in proc.stderr


# --- alternation -----------------------------------------------------------------------


def test_arms_alternate_within_each_case(tmp_path, arms):
    from cases import load_cases

    control, treatment = (load_arm(p) for p in arms)
    directory = tmp_path / "corpus" / "cases"
    directory.mkdir(parents=True)
    (directory / "c-001.yaml").write_text(
        f"id: c-001\nmode: diff\nsource: merged-pr\nrepo_commit: {FAKE_OID}\n",
        encoding="utf-8",
    )
    cases = load_cases(directory)
    staged = {"c-001": None}
    tasks = build_tasks(cases, staged, [("codex", "m")], control, treatment, runs=3)

    assert [t.role for t in tasks] == [
        CONTROL, TREATMENT, TREATMENT, CONTROL, CONTROL, TREATMENT,
    ]
    assert [t.order_index for t in tasks] == [0, 1, 2, 3, 4, 5]
    assert [t.rep for t in tasks] == [1, 1, 2, 2, 3, 3]
    # Both arms get exactly the same work on the same case.
    assert sum(t.role == CONTROL for t in tasks) == sum(t.role == TREATMENT for t in tasks)


def test_order_index_restarts_per_case_and_backend(tmp_path, arms):
    from cases import load_cases

    control, treatment = (load_arm(p) for p in arms)
    directory = tmp_path / "corpus" / "cases"
    directory.mkdir(parents=True)
    for case_id in ("a-001", "b-002"):
        (directory / f"{case_id}.yaml").write_text(
            f"id: {case_id}\nmode: diff\nsource: merged-pr\n"
            f"repo_commit: {FAKE_OID}\n",
            encoding="utf-8",
        )
    cases = load_cases(directory)
    tasks = build_tasks(
        cases, dict.fromkeys(["a-001", "b-002"]), [("codex", "m"), ("claude", "n")],
        control, treatment, runs=1,
    )
    grouped: dict[tuple[str, str], list[int]] = {}
    for task in tasks:
        grouped.setdefault((task.case.id, task.backend), []).append(task.order_index)
    assert list(grouped.values()) == [[0, 1]] * 4


# --- scheduling ---------------------------------------------------------------------------


def fake_groups(cases=("a", "b"), backends=("codex",), runs=2):
    """Rep groups whose tasks carry only what the scheduler looks at."""
    groups = []
    for case_id in cases:
        for backend in backends:
            for rep in range(1, runs + 1):
                groups.append(RepGroup(
                    stream=(case_id, backend), rep=rep,
                    tasks=(f"{case_id}-r{rep}-{CONTROL}", f"{case_id}-r{rep}-{TREATMENT}"),
                ))
    return groups


def test_a_repetitions_two_runs_finish_before_the_next_one_starts():
    # The pairing is only real if it survives scheduling. Submitting the whole sweep lets a
    # fast arm run ahead, so a repetition's control and treatment stop facing the same
    # conditions — which is the confound the design exists to remove.
    events: list[tuple[str, str]] = []
    lock = Lock()

    def execute(task):
        with lock:
            events.append(("start", task))
        time.sleep(0.01)
        with lock:
            events.append(("end", task))

    run_groups(fake_groups(runs=3), concurrency=4, execute=execute)

    order = [(kind, task) for kind, task in events]
    for case_id in ("a", "b"):
        for rep in (1, 2):
            ends = [
                i for i, (kind, task) in enumerate(order)
                if kind == "end" and task.startswith(f"{case_id}-r{rep}-")
            ]
            starts = [
                i for i, (kind, task) in enumerate(order)
                if kind == "start" and task.startswith(f"{case_id}-r{rep + 1}-")
            ]
            assert len(ends) == 2 and len(starts) == 2
            assert max(ends) < min(starts), f"{case_id} rep{rep + 1} started too early"


def test_different_cases_still_run_concurrently():
    running = 0
    peak = 0
    lock = Lock()

    def execute(task):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1

    run_groups(fake_groups(cases=("a", "b"), runs=1), concurrency=4, execute=execute)
    assert peak > 2, "two cases with four slots should overlap"


@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_no_more_runs_are_in_flight_than_the_concurrency_limit(concurrency):
    started: list[str] = []
    running = 0
    peak = 0
    lock = Lock()

    def execute(task):
        nonlocal running, peak
        with lock:
            started.append(task)
            running += 1
            peak = max(peak, running)
        time.sleep(0.01)
        with lock:
            running -= 1

    run_groups(
        fake_groups(cases=("a", "b", "c"), runs=3), concurrency=concurrency, execute=execute,
    )
    assert peak <= concurrency
    assert len(started) == 18  # and everything still runs


@pytest.mark.parametrize("blow_up", [RuntimeError, KeyboardInterrupt])
def test_runs_still_in_flight_when_a_sweep_fails_keep_their_rows(
    tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend, blow_up,
):
    """A row already paid for must survive the exception that ends the sweep.

    The results file is open for the duration of the sweep, so abandoning workers that are
    still inside `run_task` lets it close underneath them: their `fh.write` then raises
    into a future nobody reads, and up to `--concurrency` billed runs vanish silently.
    """
    import paired_runner

    calls: list[str] = []
    lock = Lock()
    real_record = None

    def fake_run_task(task, provenance, python, timeout):
        nonlocal real_record
        with lock:
            calls.append(task.role)
            first = len(calls) == 1
        if first:
            time.sleep(0.1)  # let the sibling get picked up before we bring it all down
            raise blow_up("stop")
        time.sleep(0.4)  # still inside run_task when the failure propagates
        real_record = paired_runner.PairedRecord(
            sweep_id=provenance.sweep_id, case_id=task.case.id, mode=task.case.mode,
            source=task.case.source, repo_commit=task.case.repo_commit,
            case_is_control=task.case.is_control, arm=task.arm.name, arm_role=task.role,
            arm_hash=task.arm.content_hash, backend=task.backend,
            requested_model=task.model, backend_version=None,
            harness_rr_version=None, runtime_rr_version=None, harness_commit=None,
            rep=task.rep, order_index=task.order_index, attempt=1, command=["stub"],
            cwd=str(task.materialized.cwd), exit_code=0, duration_s=0.4,
            raw=json.dumps({"verdict": "approve", "summary": "s", "findings": []}),
            outcome=VALID, errors=[], excerpt="", bare_json=True, backend_error=None,
            started_at="2026-01-01T00:00:00+00:00",
        )
        return [real_record]

    monkeypatch.delenv("CI", raising=False)
    for key, value in stub_backend.env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(paired_runner, "run_task", fake_run_task)

    control, treatment = arms
    out = tmp_path / "results"
    with pytest.raises(blow_up):
        main([
            "--control", str(control), "--treatment", str(treatment),
            "--backends", "codex:stub-model", "--cases", str(corpus),
            "--runs", "2", "--concurrency", "2", "--timeout", "60",
            "--repo", str(git_repo), "--out", str(out),
        ])

    rows = [
        json.loads(line)
        for line in sorted(out.glob("paired-*.jsonl"))[0].read_text(encoding="utf-8")
                    .splitlines()[1:]
    ]
    assert real_record is not None, "the sibling run never completed; test proves nothing"
    assert len(rows) == 1, "the in-flight run's row was lost when the file closed"
    assert rows[0]["arm_role"] == real_record.arm_role
    assert rows[0]["outcome"] == VALID


@pytest.mark.parametrize("blow_up", [RuntimeError, KeyboardInterrupt])
def test_a_failure_stops_the_sweep_instead_of_launching_the_rest(blow_up):
    # Every queued task is a billed review, so an interrupt has to mean "stop spending",
    # not "finish the queue". executor.map submits everything up front and cannot.
    started: list[str] = []
    lock = Lock()

    def execute(task):
        with lock:
            started.append(task)
            first = len(started) == 1
        if first:
            raise blow_up("stop")
        time.sleep(0.005)

    groups = fake_groups(cases=("a", "b", "c"), runs=3)
    with pytest.raises(blow_up):
        run_groups(groups, concurrency=2, execute=execute)

    total = sum(len(g.tasks) for g in groups)
    assert total == 18
    # Only the failing repetition's own two runs may have started.
    assert len(started) <= 2, started


# --- end to end ------------------------------------------------------------------------


def run_paired(tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend, **extra):
    monkeypatch.delenv("CI", raising=False)
    for key, value in stub_backend.env().items():
        monkeypatch.setenv(key, value)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    control, treatment = arms
    out = tmp_path / "results"
    exit_code = main([
        "--control", str(control), "--treatment", str(treatment),
        "--backends", "codex:stub-model", "--cases", str(corpus),
        "--runs", "2", "--concurrency", "1", "--timeout", "60",
        "--repo", str(git_repo), "--out", str(out),
    ])
    assert exit_code == 0
    results = sorted(out.glob("paired-*.jsonl"))
    assert len(results) == 1
    lines = [json.loads(line) for line in results[0].read_text(encoding="utf-8").splitlines()]
    return lines[0], lines[1:]


def test_a_paired_run_produces_a_complete_result_file(
    tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend, capsys,
):
    header, rows = run_paired(tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend)

    # 2 cases x 2 arms x 2 reps, no retries.
    assert header["units_total"] == 8
    assert len(rows) == 8
    assert header["alternation"] == ALTERNATION_SCHEME
    assert header["max_attempts"] == MAX_ATTEMPTS
    assert header["backend_specs"] == {"codex": "stub-model"}
    assert {c["id"] for c in header["cases"]} == {"m-001", "c-001"}
    assert header["arms"][CONTROL]["hash"] != header["arms"][TREATMENT]["hash"]

    assert all(row["outcome"] == VALID for row in rows)
    assert all(row["attempt"] == 1 for row in rows)
    assert {row["arm_role"] for row in rows} == {CONTROL, TREATMENT}

    # Every row carries the provenance a comparison needs: which prompt bytes, which
    # snapshot, which versions answered, which model was asked for, and the exact command.
    assert header["backend_versions"] == {"codex": "stub-codex 0.0.1"}
    for row in rows:
        arm_hash = header["arms"][row["arm_role"]]["hash"]
        assert row["arm_hash"] == arm_hash
        assert row["repo_commit"] == header["cases"][0]["repo_commit"]
        assert row["requested_model"] == "stub-model"
        assert row["sweep_id"] == header["sweep_id"]
        assert row["backend_version"] == "stub-codex 0.0.1"
        assert row["harness_rr_version"] == header["harness_rr_version"]
        # Which decision rule governs this row, on the row itself — under the same key
        # the header uses, so a filter that keeps only rows and one that keeps only the
        # header are read the same way.
        assert row["case_is_control"] == (row["case_id"] == "c-001")
        header_case = next(c for c in header["cases"] if c["id"] == row["case_id"])
        assert header_case["case_is_control"] == row["case_is_control"]
        assert row["command"][1] == str(LAUNCHER)
        assert "--full" in row["command"]
        assert json.loads(row["raw"])["verdict"] == "approve"

    # Each case saw both arms the same number of times, which is the pairing.
    per_case: dict[tuple[str, str], int] = {}
    for row in rows:
        per_case[(row["case_id"], row["arm"])] = per_case.get((row["case_id"], row["arm"]), 0) + 1
    assert set(per_case.values()) == {2}


def test_each_arms_prompts_reach_the_backend_during_a_paired_run(
    tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend,
):
    run_paired(tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend)
    captured = stub_backend.captured_prompts()
    assert len(captured) == 8
    for arm in ("alpha", "beta"):
        marker = MARKER.format(name="DIFF_REVIEW_PROMPT", arm=arm)
        assert sum(marker in prompt for prompt in captured) == 4


def test_a_failed_run_is_retried_once_and_both_attempts_are_kept(
    tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend,
):
    # The first backend invocation of the whole sweep fails; its retry succeeds.
    _, rows = run_paired(
        tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend, STUB_FAIL_UNTIL="1",
    )
    assert len(rows) == 9
    failed = [r for r in rows if r["outcome"] == BACKEND_ERROR]
    assert len(failed) == 1
    assert failed[0]["attempt"] == 1
    assert failed[0]["raw"] == ""
    assert failed[0]["backend_error"]

    retry = next(
        r for r in rows
        if r["attempt"] == 2
        and (r["case_id"], r["arm"], r["rep"]) == (
            failed[0]["case_id"], failed[0]["arm"], failed[0]["rep"]
        )
    )
    assert retry["outcome"] == VALID


def test_a_run_that_never_succeeds_is_recorded_not_dropped(
    tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend,
):
    _, rows = run_paired(
        tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend, STUB_FAIL_UNTIL="99",
    )
    assert len(rows) == 16  # every unit tried MAX_ATTEMPTS times
    assert all(r["outcome"] == BACKEND_ERROR for r in rows)
    assert {r["attempt"] for r in rows} == {1, 2}


def test_the_runner_leaves_no_worktree_behind(
    tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend,
):
    run_paired(tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend)
    assert "case-m-001" not in git(git_repo, "worktree", "list").stdout


# --- refusals --------------------------------------------------------------------------


def test_it_refuses_to_run_in_ci(tmp_path, corpus, arms, monkeypatch):
    monkeypatch.setenv("CI", "1")
    control, treatment = arms
    assert main([
        "--control", str(control), "--treatment", str(treatment),
        "--backends", "codex:stub-model", "--cases", str(corpus),
    ]) == 1


def test_it_refuses_a_backend_without_a_model(tmp_path, corpus, arms, monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    control, treatment = arms
    assert main([
        "--control", str(control), "--treatment", str(treatment),
        "--backends", "codex", "--cases", str(corpus),
    ]) == 1
    assert "must be name:model" in capsys.readouterr().err


def test_it_reports_an_a_a_run_rather_than_pretending_it_is_a_comparison(
    tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend, capsys,
):
    monkeypatch.delenv("CI", raising=False)
    control, _ = arms
    for key, value in stub_backend.env().items():
        monkeypatch.setenv(key, value)
    assert main([
        "--control", str(control), "--treatment", str(control),
        "--backends", "codex:stub-model", "--cases", str(corpus),
        "--runs", "1", "--concurrency", "1", "--timeout", "60",
        "--repo", str(git_repo), "--out", str(tmp_path / "results"),
    ]) == 0
    assert "A/A run" in capsys.readouterr().err


def test_an_a_a_run_records_and_summarises_both_roles_separately(
    tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend, capsys,
):
    # 2 cases x 2 roles x 2 reps of one arm. Everything downstream keys on the arm's name
    # as well as its role, so a run where the two names are identical is where a
    # role-blind key silently halves the sweep.
    monkeypatch.delenv("CI", raising=False)
    control, _ = arms
    for key, value in stub_backend.env().items():
        monkeypatch.setenv(key, value)
    out = tmp_path / "results"
    assert main([
        "--control", str(control), "--treatment", str(control),
        "--backends", "codex:stub-model", "--cases", str(corpus),
        "--runs", "2", "--concurrency", "1", "--timeout", "60",
        "--repo", str(git_repo), "--out", str(out),
    ]) == 0

    results = sorted(out.glob("paired-*.jsonl"))
    rows = [
        json.loads(line)
        for line in results[0].read_text(encoding="utf-8").splitlines()[1:]
    ]
    assert len(rows) == 8
    assert {r["arm"] for r in rows} == {"alpha"}
    assert sorted(r["arm_role"] for r in rows) == [CONTROL] * 4 + [TREATMENT] * 4

    captured = capsys.readouterr()
    # The progress counter counts units, not halves of them.
    assert "[8/8]" in captured.err
    # And the summary reports two rows of four, not one row counted twice.
    summary = [line for line in captured.out.splitlines() if line.startswith("codex /")]
    assert len(summary) == 2
    assert f"codex / {CONTROL} (alpha)" in summary[0]
    assert f"codex / {TREATMENT} (alpha)" in summary[1]
    assert [line.split()[-1] for line in summary] == ["4", "4"]

    metrics, incomplete = compute(rows, lambda commit, path: 5, git_repo)
    assert incomplete == []
    assert [(m.arm_role, m.scores.runs_scored) for m in metrics] == [
        (CONTROL, 4), (TREATMENT, 4),
    ]


def test_the_interpreter_is_resolved_to_an_absolute_path():
    resolved, error = resolve_interpreter(sys.executable)
    assert error is None
    assert Path(resolved).is_absolute()
    # Not symlink-resolved: a venv's bin/python points at the base interpreter, which
    # cannot import the rocket_review the venv installed.
    assert Path(resolved).name == Path(sys.executable).name


def test_an_interpreter_that_does_not_exist_is_rejected_before_any_spend(
    tmp_path, corpus, arms, monkeypatch, capsys,
):
    monkeypatch.delenv("CI", raising=False)
    control, treatment = arms
    assert main([
        "--control", str(control), "--treatment", str(treatment),
        "--backends", "codex:stub-model", "--cases", str(corpus),
        "--python", str(tmp_path / "no-such-python"),
    ]) == 1
    assert "is not on PATH and is not a file" in capsys.readouterr().err


def test_a_case_naming_an_unknown_commit_stops_the_run_before_any_spend(
    tmp_path, git_repo, corpus, arms, monkeypatch, stub_backend, capsys,
):
    monkeypatch.delenv("CI", raising=False)
    for key, value in stub_backend.env().items():
        monkeypatch.setenv(key, value)
    (corpus / "c-001.yaml").write_text(
        f"id: c-001\nmode: diff\nsource: merged-pr\nrepo_commit: {ABSENT_OID}\n",
        encoding="utf-8",
    )
    control, treatment = arms
    assert main([
        "--control", str(control), "--treatment", str(treatment),
        "--backends", "codex:stub-model", "--cases", str(corpus),
        "--repo", str(git_repo), "--out", str(tmp_path / "results"),
    ]) == 1
    assert "repo_commit not found" in capsys.readouterr().err
    assert stub_backend.captured_prompts() == []


def test_an_unknown_arm_is_rejected(tmp_path, corpus, arms, monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    control, _ = arms
    assert main([
        "--control", str(control), "--treatment", str(tmp_path / "nope"),
        "--backends", "codex:stub-model", "--cases", str(corpus),
    ]) == 1
    assert "not found" in capsys.readouterr().err


def test_a_case_that_cannot_be_staged_stops_the_run(
    tmp_path, git_repo, corpus, arms, monkeypatch, capsys,
):
    monkeypatch.delenv("CI", raising=False)
    (corpus / "m-001.patch").write_text("garbage that is not a patch\n", encoding="utf-8")
    control, treatment = arms
    assert main([
        "--control", str(control), "--treatment", str(treatment),
        "--backends", "codex:stub-model", "--cases", str(corpus),
        "--repo", str(git_repo), "--out", str(tmp_path / "results"),
    ]) == 1
    assert "does not apply" in capsys.readouterr().err
    assert "case-m-001" not in git(git_repo, "worktree", "list").stdout


def test_remove_worktree_reports_a_failure_without_raising(git_repo, tmp_path, capsys):
    # Teardown runs in a finally block alongside other cleanups, so it must not raise —
    # but a worktree that would not go away leaves admin state only `prune` clears, and
    # saying nothing about that is how it gets discovered weeks later.
    remove_worktree(git_repo, tmp_path / "never-created")
    stderr = capsys.readouterr().err
    assert "could not remove worktree" in stderr
    assert "worktree prune" in stderr
