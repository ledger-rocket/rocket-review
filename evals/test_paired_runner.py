"""The paired runner, end to end against a stub backend binary. No model is ever called.

The load-bearing test here is `test_the_arms_prompt_text_reaches_the_backend`: a runner
that cannot actually vary the prompt would still produce a tidy JSONL file full of numbers
that mean nothing. Everything else builds on that proof.
"""

import json
import subprocess
import sys
from pathlib import Path

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
    build_tasks,
    main,
)
from rr_arm_launcher import ARM_ENV
from strict_validator import BACKEND_ERROR, VALID

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
        "id: c-001\nmode: diff\nsource: merged-pr\nrepo_commit: abc1234\n", encoding="utf-8"
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
            f"id: {case_id}\nmode: diff\nsource: merged-pr\nrepo_commit: abc1234\n",
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
