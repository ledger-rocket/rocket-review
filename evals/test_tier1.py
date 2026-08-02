"""Tier-1 metrics on fixed rows: every assertion here is deterministic and backend-free."""

import json
from pathlib import Path

import pytest
from conftest import git
from strict_validator import BACKEND_ERROR, DECODE_FAILURE, SCHEMA_VIOLATION, VALID
from tier1 import (
    DO_NOT_FLAG_PATTERNS,
    SEVERITY_BUCKETS,
    GitLineCounter,
    classify_do_not_flag,
    compute,
    final_attempts,
    load_jsonl,
    main,
    normalize_cited_path,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

REPO_COMMIT = "abc1234def5678"


def review(findings, verdict="needs_fixes", summary="s") -> str:
    if not findings:
        verdict = "approve"
    return json.dumps({"verdict": verdict, "summary": summary, "findings": findings})


def finding(title="Something is wrong", severity="high", file="sample.py", line=3,
            why="because", fix="do it") -> dict:
    return {"severity": severity, "title": title, "file": file, "line": line,
            "why": why, "fix": fix}


def row(**overrides) -> dict:
    base = {
        "case_id": "c-001", "mode": "diff", "source": "merged-pr",
        "repo_commit": REPO_COMMIT, "case_is_control": True,
        "arm": "control-arm", "arm_role": "control",
        "arm_hash": "h", "backend": "codex", "requested_model": "m", "rep": 1,
        "order_index": 0, "attempt": 1, "command": ["rr"], "cwd": "/repo",
        "exit_code": 0, "duration_s": 1.0, "raw": review([]), "outcome": VALID,
        "errors": [], "excerpt": "", "bare_json": True, "backend_error": None,
        "started_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def counting(lines_by_path: dict[str, int]):
    return lambda commit, path: lines_by_path.get(path)


def group(rows, **kwargs):
    return compute(rows, counting(kwargs.pop("lines", {})), Path("/repo"))[0]


def only(rows, **kwargs):
    """The single group's pooled scores — what most metric assertions are about."""
    return group(rows, **kwargs).scores


# --- the DO-NOT-FLAG tripwire ----------------------------------------------------------


def test_the_taxonomy_reproduces_the_hand_labelled_fixture():
    fixture = json.loads((FIXTURES / "do_not_flag_labels.json").read_text(encoding="utf-8"))
    mistakes = [
        (entry["title"], entry["label"], classify_do_not_flag(entry["title"]))
        for entry in fixture["labelled"]
        if classify_do_not_flag(entry["title"]) != entry["label"]
    ]
    assert mistakes == []


def test_the_fixture_exercises_every_category_in_both_directions():
    fixture = json.loads((FIXTURES / "do_not_flag_labels.json").read_text(encoding="utf-8"))
    labels = {entry["label"] for entry in fixture["labelled"]}
    assert labels >= {name for name, _ in DO_NOT_FLAG_PATTERNS}
    assert None in labels


def test_only_the_title_is_classified():
    # The design choice the fixture's negatives encode: the same words inside `why` are
    # usually part of a real argument, not a style nit.
    assert classify_do_not_flag("Patch bytes are altered before review") is None
    metrics = only([row(raw=review([finding(
        title="Patch bytes are altered before review",
        why="stripping trailing whitespace changes the indentation of every line",
    )]))])
    assert metrics.do_not_flag_hits == 0


def test_tripwire_rate_counts_matching_findings_over_all_findings():
    metrics = only([row(raw=review([
        finding(title="Imports are not sorted alphabetically"),
        finding(title="Missing type annotations on run_attempt"),
        finding(title="should_fail lets an errored backend through the gate"),
        finding(title="load_cases has no docstring"),
    ]))])
    assert metrics.findings_total == 4
    assert metrics.do_not_flag_hits == 3
    assert metrics.do_not_flag_rate == 0.75
    assert metrics.do_not_flag_by_class == {
        "docs-only": 1, "import-ordering": 1, "missing-annotations": 1,
    }


def test_tripwire_rate_is_not_measured_when_there_are_no_findings():
    # None, not 0.0: nothing was measured, and 0% would read as perfect discipline.
    assert only([row(raw=review([]))]).do_not_flag_rate is None


# --- strict validity --------------------------------------------------------------------


def test_strict_valid_rate_excludes_failed_runs_from_the_denominator():
    rows = [
        row(rep=1, outcome=VALID),
        row(rep=2, outcome=SCHEMA_VIOLATION),
        row(rep=3, outcome=DECODE_FAILURE, raw="I refuse."),
        row(rep=4, outcome=BACKEND_ERROR, raw="", backend_error="codex timed out"),
    ]
    metrics = only(rows)
    assert metrics.runs_scored == 3
    assert metrics.runs_failed == 1
    assert metrics.strict_valid_rate == round(1 / 3, 4)


def test_only_the_last_attempt_of_a_unit_is_scored():
    rows = [
        row(rep=1, attempt=1, outcome=BACKEND_ERROR, raw="", backend_error="boom"),
        row(rep=1, attempt=2, outcome=VALID),
    ]
    assert [r["attempt"] for r in final_attempts(rows)] == [2]
    metrics = only(rows)
    assert metrics.runs_scored == 1
    assert metrics.runs_failed == 0
    assert metrics.strict_valid_rate == 1.0


def test_a_unit_failing_after_its_retry_is_counted_as_failed_not_dropped():
    rows = [
        row(rep=1, attempt=1, outcome=BACKEND_ERROR, raw="", backend_error="boom"),
        row(rep=1, attempt=2, outcome=BACKEND_ERROR, raw="", backend_error="boom"),
    ]
    metrics = only(rows)
    assert (metrics.runs_scored, metrics.runs_failed) == (0, 1)
    assert metrics.strict_valid_rate is None


# --- file:line resolution ----------------------------------------------------------------


def test_a_line_inside_the_file_resolves():
    metrics = only(
        [row(raw=review([finding(file="sample.py", line=3)]))],
        lines={"sample.py": 5},
    )
    assert (metrics.line_bearing, metrics.line_resolved) == (1, 1)
    assert metrics.line_resolved_rate == 1.0


def test_a_line_past_the_end_of_the_file_does_not_resolve():
    metrics = only(
        [row(raw=review([finding(file="sample.py", line=99)]))],
        lines={"sample.py": 5},
    )
    assert (metrics.line_bearing, metrics.line_resolved) == (1, 0)


def test_a_file_absent_at_the_commit_does_not_resolve():
    metrics = only(
        [row(raw=review([finding(file="invented.py", line=1)]))],
        lines={"sample.py": 5},
    )
    assert (metrics.line_bearing, metrics.line_resolved) == (1, 0)


def test_findings_without_a_file_or_line_are_exempt_not_unresolved():
    rows = [row(raw=review([
        finding(file=None, line=None),
        finding(file="sample.py", line=None),
        finding(file=None, line=4),
        finding(file="sample.py", line=1),
    ]))]
    metrics = only(rows, lines={"sample.py": 5})
    assert metrics.findings_total == 4
    assert (metrics.line_bearing, metrics.line_resolved) == (1, 1)


def test_a_non_string_file_is_exempt_rather_than_a_crash():
    # The runtime parser passes `file` through uncoerced, so a schema-violating review —
    # the thing this harness exists to catch — can put an object there. Scoring a whole
    # sweep must not die on one bad field.
    rows = [row(raw=json.dumps({
        "verdict": "needs_fixes", "summary": "s",
        "findings": [
            {"severity": "high", "title": "object in file", "file": {"path": "sample.py"},
             "line": 3, "why": "w", "fix": "f"},
            {"severity": "high", "title": "number in file", "file": 42, "line": 3,
             "why": "w", "fix": "f"},
            {"severity": "high", "title": "list in file", "file": ["sample.py"], "line": 3,
             "why": "w", "fix": "f"},
            {"severity": "high", "title": "a real citation", "file": "sample.py", "line": 3,
             "why": "w", "fix": "f"},
        ],
    }), outcome=SCHEMA_VIOLATION)]
    metrics = only(rows, lines={"sample.py": 5})
    assert metrics.findings_total == 4
    # Only the string citation made a locatable claim.
    assert (metrics.line_bearing, metrics.line_resolved) == (1, 1)


def test_a_seeded_plan_citation_is_exempt_rather_than_unresolved():
    # A plan file exists at no commit, so resolving its citations against the object
    # database would mark every one a hallucination and drag the pooled rate down forever.
    rows = [row(
        case_id="p-001", mode="plan", source="seeded-plan",
        raw=review([
            finding(file="cases/p-001-plan.md", line=12),
            finding(file="cases/p-001-plan.md", line=9999),
        ]),
    )]
    metrics = only(rows, lines={"sample.py": 5})
    assert metrics.findings_total == 2
    assert (metrics.line_bearing, metrics.line_resolved) == (0, 0)
    assert metrics.line_resolved_rate is None


def test_a_diff_case_in_the_same_sweep_is_still_resolved():
    # The exemption is scoped to the plan rows, not to the whole file.
    rows = [
        row(case_id="p-001", mode="plan", source="seeded-plan", rep=1,
            raw=review([finding(file="cases/p-001-plan.md", line=12)])),
        row(case_id="b-001", source="mutant", rep=1,
            raw=review([finding(file="sample.py", line=3)])),
    ]
    metrics = only(rows, lines={"sample.py": 5})
    assert (metrics.line_bearing, metrics.line_resolved) == (1, 1)


def test_line_zero_does_not_resolve():
    metrics = only(
        [row(raw=review([finding(file="sample.py", line=0)]))],
        lines={"sample.py": 5},
    )
    assert (metrics.line_bearing, metrics.line_resolved) == (1, 0)


def test_cited_paths_are_normalized_before_lookup():
    assert normalize_cited_path("./rocket_review/cli.py", Path("/repo")) == "rocket_review/cli.py"
    assert normalize_cited_path("/repo/rocket_review/cli.py", Path("/repo")) == "rocket_review/cli.py"
    assert normalize_cited_path("/elsewhere/cli.py", Path("/repo")) == "/elsewhere/cli.py"


def test_git_line_counter_reads_the_snapshot_not_the_working_tree(git_repo):
    head = git(git_repo, "rev-parse", "HEAD").stdout.strip()
    first = git(git_repo, "rev-parse", "HEAD~1").stdout.strip()
    counter = GitLineCounter(git_repo)
    assert counter(head, "sample.py") == 5
    assert counter(first, "sample.py") == 3
    assert counter(head, "absent.py") is None
    (git_repo / "sample.py").write_text("one line\n", encoding="utf-8")
    assert counter(head, "sample.py") == 5


def test_git_line_counter_refuses_input_git_would_read_as_an_option(git_repo):
    counter = GitLineCounter(git_repo)
    assert counter("HEAD", "sample.py") is None  # not a plain object id
    assert counter("a" * 40, "--output=/tmp/pwned") is None


# --- findings-per-run distribution --------------------------------------------------------


def test_findings_per_run_is_split_by_severity():
    rows = [
        row(rep=1, raw=review([
            finding(severity="critical"), finding(severity="low"), finding(severity="low"),
        ])),
        row(rep=2, raw=review([finding(severity="low")])),
    ]
    metrics = only(rows)
    assert metrics.findings_per_run.mean == 2.0
    assert metrics.findings_per_run.minimum == 1
    assert metrics.findings_per_run.maximum == 3
    assert metrics.findings_per_run.n == 2
    by_severity = metrics.findings_per_run_by_severity
    assert by_severity["critical"].mean == 0.5
    assert by_severity["low"].mean == 1.5
    assert by_severity["high"].maximum == 0


def test_an_invented_severity_lands_in_the_other_bucket():
    # Invented labels are one of the things this harness measures, so they cannot vanish
    # from the per-severity view — counted in the total but in no bucket.
    rows = [row(raw=review([
        finding(severity="critical"), finding(severity="warning"), finding(severity="nit"),
    ]))]
    metrics = only(rows)
    assert metrics.findings_per_run_by_severity["other"].mean == 2.0
    assert metrics.findings_per_run_by_severity["critical"].mean == 1.0


def test_the_severity_buckets_sum_to_the_overall_findings_per_run():
    rows = [
        row(rep=1, raw=review([finding(severity="high"), finding(severity="bogus")])),
        row(rep=2, raw=review([finding(severity="low")])),
    ]
    metrics = only(rows)
    buckets = metrics.findings_per_run_by_severity
    assert sum(buckets[b].mean for b in SEVERITY_BUCKETS) == metrics.findings_per_run.mean


def test_the_veto_number_is_reported_over_clean_controls_separately():
    # Pooling defect cases into critical+high/run answers a different question: a CRITICAL
    # finding on a defect case is the correct answer, not the noise the veto rule bounds.
    rows = [
        row(case_id="b-001", case_is_control=False, rep=1,
            raw=review([finding(severity="critical"), finding(severity="critical")])),
        row(case_id="c-001", case_is_control=True, rep=1,
            raw=review([finding(severity="high")])),
    ]
    metrics = group(rows, lines={"sample.py": 5})
    assert metrics.scores.critical_high_per_run.mean == 1.5
    assert metrics.control_scores is not None
    assert metrics.control_scores.critical_high_per_run.mean == 1.0
    assert metrics.control_scores.runs_scored == 1


def test_a_sweep_with_no_clean_controls_reports_no_control_scores():
    rows = [row(case_id="b-001", case_is_control=False, raw=review([finding()]))]
    assert group(rows, lines={"sample.py": 5}).control_scores is None


def test_critical_and_high_are_reported_together_for_the_veto_rule():
    rows = [
        row(rep=1, raw=review([
            finding(severity="critical"), finding(severity="high"), finding(severity="low"),
        ])),
        row(rep=2, raw=review([finding(severity="medium")])),
    ]
    metrics = only(rows)
    assert metrics.critical_high_per_run.mean == 1.0
    assert metrics.critical_high_per_run.maximum == 2
    assert metrics.critical_high_per_run.minimum == 0


def test_an_empty_distribution_reports_nothing_measured_rather_than_zero():
    # Same rule as the rates: a 0 would read as "measured, and it was zero".
    empty = only([row(outcome=BACKEND_ERROR, raw="", backend_error="boom")]).findings_per_run
    assert empty.n == 0
    assert (empty.mean, empty.median, empty.minimum, empty.maximum) == (None, None, None, None)


# --- per-case breakdown ------------------------------------------------------------------


def test_each_case_is_scored_separately_within_an_arm():
    # The veto rule is per clean-control case, so a pooled mean is not enough: here the
    # pooled critical+high mean is 1.0 on both arms while one control case carries all of
    # the treatment's high-severity output.
    rows = [
        row(case_id="c-001", case_is_control=True, rep=1,
            raw=review([finding(severity="critical")])),
        row(case_id="c-002", case_is_control=True, rep=1,
            raw=review([finding(severity="high")])),
        row(case_id="c-001", case_is_control=True, rep=1, arm="treatment-arm",
            arm_role="treatment", raw=review([
                finding(severity="critical"), finding(severity="high"),
            ])),
        row(case_id="c-002", case_is_control=True, rep=1, arm="treatment-arm",
            arm_role="treatment", raw=review([])),
    ]
    metrics = compute(rows, counting({"sample.py": 5}), Path("/repo"))
    pooled = {m.arm: m.scores.critical_high_per_run.mean for m in metrics}
    assert pooled == {"control-arm": 1.0, "treatment-arm": 1.0}

    per_case = {
        (m.arm, c.case_id): c.scores.critical_high_per_run.mean
        for m in metrics for c in m.per_case
    }
    assert per_case == {
        ("control-arm", "c-001"): 1.0, ("control-arm", "c-002"): 1.0,
        ("treatment-arm", "c-001"): 2.0, ("treatment-arm", "c-002"): 0.0,
    }


def test_the_per_case_breakdown_carries_the_control_flag():
    rows = [
        row(case_id="b-001", case_is_control=False, raw=review([finding()])),
        row(case_id="c-001", case_is_control=True, rep=2, raw=review([])),
    ]
    per_case = group(rows, lines={"sample.py": 5}).per_case
    assert [(c.case_id, c.is_control) for c in per_case] == [
        ("b-001", False), ("c-001", True),
    ]


def test_an_a_a_run_scores_every_row_and_reports_two_groups():
    # The same arm in both roles is how the noise floor gets measured. Keying units or
    # groups on the arm name alone collapsed each pair into one, silently discarding half
    # the sweep and labelling the survivor "control".
    rows = [
        row(case_id=case_id, arm="current", arm_role=role, rep=rep,
            raw=review([finding()]))
        for case_id in ("c-001", "c-002")
        for rep in (1, 2)
        for role in ("control", "treatment")
    ]
    assert len(final_attempts(rows)) == len(rows) == 8

    metrics = compute(rows, counting({"sample.py": 5}), Path("/repo"))
    assert [(m.arm_role, m.arm, m.scores.runs_scored) for m in metrics] == [
        ("control", "current", 4), ("treatment", "current", 4),
    ]


def test_two_arms_sharing_a_directory_name_stay_separate():
    # --control current --treatment /tmp/experiment/current: same basename, different text.
    rows = [
        row(arm="current", arm_role="control", arm_hash="aaa", raw=review([finding()])),
        row(arm="current", arm_role="treatment", arm_hash="bbb",
            raw=review([finding(), finding()])),
    ]
    metrics = compute(rows, counting({"sample.py": 5}), Path("/repo"))
    assert [(m.arm_role, m.scores.findings_total) for m in metrics] == [
        ("control", 1), ("treatment", 2),
    ]


def test_a_row_predating_the_control_flag_is_reported_as_unlabelled():
    stale = row()
    del stale["case_is_control"]
    assert group([stale]).per_case[0].is_control is None


def test_a_cases_failed_runs_are_visible_in_its_own_breakdown():
    rows = [
        row(case_id="b-001", case_is_control=False, rep=1, raw=review([finding()])),
        row(case_id="c-001", case_is_control=True, rep=1, outcome=BACKEND_ERROR,
            raw="", backend_error="codex timed out"),
    ]
    per_case = {c.case_id: c.scores for c in group(rows, lines={"sample.py": 5}).per_case}
    assert (per_case["b-001"].runs_scored, per_case["b-001"].runs_failed) == (1, 0)
    assert (per_case["c-001"].runs_scored, per_case["c-001"].runs_failed) == (0, 1)


# --- grouping and IO -----------------------------------------------------------------------


def test_arms_and_backends_are_scored_separately():
    rows = [
        row(arm="control-arm", arm_role="control", raw=review([finding()])),
        row(arm="treatment-arm", arm_role="treatment", rep=2,
            raw=review([finding(), finding()])),
    ]
    metrics = compute(rows, counting({"sample.py": 5}), Path("/repo"))
    assert [(m.arm, m.arm_role, m.scores.findings_total) for m in metrics] == [
        ("control-arm", "control", 1), ("treatment-arm", "treatment", 2),
    ]


def test_a_results_file_round_trips_through_load_and_compute(tmp_path, capsys):
    path = tmp_path / "paired.jsonl"
    path.write_text(
        json.dumps({"type": "header", "runs": 1}) + "\n"
        + json.dumps(row(raw=review([finding(title="Imports are not sorted")]))) + "\n",
        encoding="utf-8",
    )
    header, rows = load_jsonl(path)
    assert header["runs"] == 1
    assert len(rows) == 1
    assert main([str(path), "--repo", str(tmp_path), "--json"]) == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported[0]["scores"]["do_not_flag_by_class"] == {"import-ordering": 1}
    assert reported[0]["per_case"][0]["case_id"] == "c-001"


def test_a_corrupt_results_line_is_reported_with_its_line_number(tmp_path):
    path = tmp_path / "paired.jsonl"
    path.write_text('{"type": "header"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="paired.jsonl:2"):
        load_jsonl(path)


def test_a_results_file_with_no_rows_is_an_error(tmp_path, capsys):
    path = tmp_path / "paired.jsonl"
    path.write_text('{"type": "header"}\n', encoding="utf-8")
    assert main([str(path)]) == 1
    assert "no run rows" in capsys.readouterr().err


def test_the_text_report_names_every_group(tmp_path, capsys):
    path = tmp_path / "paired.jsonl"
    path.write_text(
        json.dumps(row(arm="alpha", raw=review([finding()]))) + "\n"
        + json.dumps(row(arm="beta", arm_role="treatment", raw=review([]))) + "\n",
        encoding="utf-8",
    )
    assert main([str(path), "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "codex / control (alpha)" in out
    assert "codex / treatment (beta)" in out
    assert "DO-NOT-FLAG tripwire" in out
    assert "critical+high/run" in out
    assert "clean controls only" in out
    assert "per case:" in out
    assert "c-001 (control)" in out


def test_the_text_report_survives_a_group_with_no_scorable_runs(tmp_path, capsys):
    path = tmp_path / "paired.jsonl"
    path.write_text(
        json.dumps(row(outcome=BACKEND_ERROR, raw="", backend_error="boom")) + "\n",
        encoding="utf-8",
    )
    assert main([str(path), "--repo", str(tmp_path)]) == 0
    assert "no runs" in capsys.readouterr().out
