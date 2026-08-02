"""The adjudication scorer on synthetic sweeps: deterministic, backend-free, no git.

Every number here comes from a pre-registered rule in evals/README.md, so each test names
the rule it pins rather than the code path it walks.
"""

import json
from collections import Counter
from pathlib import Path

import pytest
import yaml
from adjudicate import (
    CASE_INVALIDATED,
    CERTIFIED,
    CERTIFYING_CLASS_MIN_CASES,
    EXIT_DRAFTED,
    EXIT_USAGE,
    FALSE_POSITIVE,
    MATCHES_DEFECT,
    NOT_CERTIFIED,
    PROTOCOL_MIN_REPS,
    REAL_UNRELATED,
    VETOED,
    AdjudicationError,
    IncompleteAdjudication,
    certifying_representative,
    compute,
    load_adjudications,
    main,
    mutation_of,
    print_report,
    removed_cases,
)
from cases import CASES_DIR, load_cases
from strict_validator import BACKEND_ERROR, VALID

SWEEP = "0123456789abcdef0123456789abcdef"
COMMIT = "a" * 40
ARMS = {"control": "before", "treatment": "after"}
BACKEND = "codex"
DEFECT_FILE = "rocket_review/models.py"
SPAN = (10, 20)


# --- building a synthetic sweep --------------------------------------------------------


def write_case(directory: Path, case_id: str, **fields) -> None:
    manifest = {"id": case_id, "mode": "diff", "source": "merged-pr", "repo_commit": COMMIT}
    manifest.update(fields)
    (directory / f"{case_id}.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )


def defect_case(directory: Path, case_id: str, defect_class="dropped-guard", **fields) -> None:
    write_case(
        directory, case_id, source="mutant", diff=f"cases/{case_id}.patch",
        defect={
            "class": defect_class, "file": DEFECT_FILE, "span": list(SPAN),
            "expected": "the guard is gone",
        },
        **fields,
    )


def corpus(tmp_path: Path, build) -> Path:
    directory = tmp_path / "cases"
    directory.mkdir(exist_ok=True)
    build(directory)
    return directory


def finding(severity="high", title="A finding", file=DEFECT_FILE, line=15,
            why="because", fix="fix it") -> dict:
    return {"severity": severity, "title": title, "file": file, "line": line,
            "why": why, "fix": fix}


def on_defect(**overrides) -> dict:
    """A finding that already satisfies rules 1 and 2, so only rule 3 is left to a human."""
    return finding(title="The guard no longer excludes anything", **overrides)


def review(findings: list[dict]) -> str:
    return json.dumps({
        "verdict": "needs_fixes" if findings else "approve",
        "summary": "synthetic", "findings": findings,
    })


def row(case_id: str, arm_role: str, rep: int, findings: list[dict] | None = None,
        is_control: bool = False, **overrides) -> dict:
    base = {
        "sweep_id": SWEEP, "case_id": case_id, "mode": "diff",
        "source": "merged-pr" if is_control else "mutant", "repo_commit": COMMIT,
        "case_is_control": is_control, "arm": ARMS[arm_role], "arm_role": arm_role,
        "arm_hash": "h", "backend": BACKEND, "requested_model": "m", "rep": rep,
        "order_index": 0, "attempt": 1, "command": ["rr"], "cwd": "/repo",
        "exit_code": 0, "duration_s": 1.0, "raw": review(findings or []),
        "outcome": VALID, "errors": [], "excerpt": "", "bare_json": True,
        "backend_error": None, "started_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def pair(case_id: str, rep: int, control: list[dict], treatment: list[dict],
         is_control: bool = False, backend: str = BACKEND) -> list[dict]:
    return [
        row(case_id, "control", rep, control, is_control=is_control, backend=backend),
        row(case_id, "treatment", rep, treatment, is_control=is_control, backend=backend),
    ]


def defect_rows(case_id: str, reps: int, control_hits: int, treatment_hits: int,
                backend: str = BACKEND) -> list[dict]:
    """One repetition per rep; the first `hits` of each arm land a finding on the defect."""
    return [
        r
        for rep in range(1, reps + 1)
        for r in pair(
            case_id, rep,
            [on_defect()] if rep <= control_hits else [],
            [on_defect()] if rep <= treatment_hits else [],
            backend=backend,
        )
    ]


def control_rows(case_id: str, reps: int, control_noise: int, treatment_noise: int,
                 backend: str = BACKEND) -> list[dict]:
    """A clean control whose arms each raise a fixed number of HIGH findings per run."""
    return [
        r
        for rep in range(1, reps + 1)
        for r in pair(
            case_id, rep,
            [finding(title=f"noise {i}") for i in range(control_noise)],
            [finding(title=f"noise {i}") for i in range(treatment_noise)],
            is_control=True, backend=backend,
        )
    ]


def call(case_id: str, arm_role: str, rep: int, decision: str, index: int = 0,
         rationale: str = "recorded call", backend: str = BACKEND) -> dict:
    return {
        "case_id": case_id, "backend": backend, "arm": ARMS[arm_role], "arm_role": arm_role,
        "rep": rep, "finding_index": index, "decision": decision, "rationale": rationale,
    }


def adjudicate_all(rows: list[dict], decision: str) -> list[dict]:
    """One call per finding of every row, which is what the completeness gate demands."""
    return [
        call(r["case_id"], r["arm_role"], r["rep"], decision, index, backend=r["backend"])
        for r in rows
        for index in range(len(json.loads(r["raw"])["findings"]))
    ]


def adjudicate_split(rows: list[dict]) -> list[dict]:
    """The ordinary shape of a sweep's calls: rule 3 on defect cases, noise on controls."""
    return (
        adjudicate_all([r for r in rows if not r["case_is_control"]], MATCHES_DEFECT)
        + adjudicate_all([r for r in rows if r["case_is_control"]], FALSE_POSITIVE)
    )


def write_results(tmp_path: Path, rows: list[dict], sweep_id: str = SWEEP) -> Path:
    path = tmp_path / "paired.jsonl"
    lines = [json.dumps({"type": "header", "sweep_id": sweep_id})]
    lines += [json.dumps(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_adjudications(tmp_path: Path, decisions: list[dict], sweep_id: str = SWEEP,
                        name: str = "adjudications.yaml", **extra) -> Path:
    path = tmp_path / name
    document = {"sweep_id": sweep_id, "decisions": decisions}
    document.update(extra)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def score(tmp_path: Path, build, rows: list[dict], decisions: list[dict]):
    cases_dir = corpus(tmp_path, build)
    cases = {c.id: c for c in load_cases(cases_dir)}
    adjudications = load_adjudications(write_adjudications(tmp_path, decisions))
    return compute(rows, cases, adjudications, tmp_path, tmp_path / "paired.jsonl")


def one_defect_class(directory: Path, count: int = 5, defect_class="dropped-guard") -> None:
    for n in range(1, count + 1):
        defect_case(directory, f"b-{100 + n}", defect_class=defect_class)


def class_of(cert, defect_class: str):
    return next(c for c in cert.backends[0].classes if c.defect_class == defect_class)


def final_verdict(out: str) -> str:
    """The report's last line is the verdict; its reasons are indented under it."""
    return next(
        line for line in reversed(out.splitlines())
        if line in (CERTIFIED, NOT_CERTIFIED, VETOED)
    )


# --- repetition aggregation: the majority rule ------------------------------------------


@pytest.mark.parametrize(
    ("reps", "hits", "found"),
    [
        (5, 3, True),    # the protocol n: 3 of 5 is the bar the rule names
        (5, 2, False),
        (5, 5, True),
        (5, 0, False),
        # Even n, which a failed repetition produces: strictly more than half, so an even
        # split is not detection.
        (4, 2, False),
        (4, 3, True),
        (2, 1, False),
        (1, 1, True),
    ],
)
def test_a_defect_is_found_only_on_a_strict_majority_of_scored_repetitions(
    tmp_path, reps, hits, found,
):
    rows = defect_rows("b-101", reps, control_hits=0, treatment_hits=hits)
    cert = score(
        tmp_path, lambda d: defect_case(d, "b-101"), rows,
        adjudicate_all(rows, MATCHES_DEFECT),
    )
    entry = class_of(cert, "dropped-guard").per_case[0]
    assert (entry.treatment_hits, entry.treatment_reps) == (hits, reps)
    assert entry.treatment_found is found


def test_a_failed_repetition_shrinks_the_denominator_for_both_arms(tmp_path):
    # Four complete repetitions plus a fifth the treatment never completed: the whole
    # repetition leaves both arms, so 2 of 4 is an even split and not a majority.
    rows = defect_rows("b-101", 4, control_hits=0, treatment_hits=2)
    rows += pair("b-101", 5, [], [on_defect()])
    rows[-1].update(outcome=BACKEND_ERROR, raw="", backend_error="codex timed out")
    cert = score(
        tmp_path, lambda d: defect_case(d, "b-101"), rows,
        adjudicate_all(rows[:-1], MATCHES_DEFECT),
    )
    entry = class_of(cert, "dropped-guard").per_case[0]
    assert (entry.treatment_reps, entry.treatment_hits, entry.treatment_found) == (4, 2, False)
    assert [p.rep for p in cert.incomplete_pairs] == [5]


# --- twin dedup -------------------------------------------------------------------------


def twinned(directory: Path) -> None:
    defect_case(directory, "b-101")
    # Same patch, reviewed as a whole file: one bug expressed twice, not two bugs.
    write_case(
        directory, "b-201", mode="code", source="mutant", diff="cases/b-101.patch",
        defect={"class": "dropped-guard", "file": DEFECT_FILE, "span": list(SPAN),
                "expected": "the guard is gone"},
    )


def test_two_cases_sharing_a_patch_are_one_mutation(tmp_path):
    cases = load_cases(corpus(tmp_path, twinned))
    assert len({mutation_of(c) for c in cases}) == 1
    assert certifying_representative(cases).id == "b-101"


def test_the_shipped_corpus_dedups_to_the_class_counts_the_gate_is_argued_from():
    # The README's class table, and the ≥5 that makes `dropped-guard` the one certifying
    # class, are counts of distinct mutants. This pins the scorer's own dedup to them, so a
    # new twin or a re-labelled mutant cannot quietly move the bar.
    cases = [c for c in load_cases(CASES_DIR) if c.defect is not None]
    groups: dict[str, list] = {}
    for case in cases:
        groups.setdefault(mutation_of(case), []).append(case)
    representatives = {certifying_representative(g).id for g in groups.values()}
    assert sorted(c.id for c in cases if c.id not in representatives) == [
        "b-012", "b-013", "b-014",
    ]
    counts = Counter(
        c.defect.defect_class for c in cases if c.id in representatives
    )
    assert [name for name, n in counts.items() if n >= CERTIFYING_CLASS_MIN_CASES] == [
        "dropped-guard",
    ]
    assert counts["dropped-guard"] == 5


def twin_promotion_corpus(directory: Path) -> None:
    """Five dropped-guard mutants, one of which is also expressed as a code-mode twin."""
    one_defect_class(directory, 5)
    write_case(
        directory, "b-201", mode="code", source="mutant", diff="cases/b-101.patch",
        defect={"class": "dropped-guard", "file": DEFECT_FILE, "span": list(SPAN),
                "expected": "the guard is gone"},
    )
    write_case(directory, "c-101")


def twin_promotion_sweep() -> list[dict]:
    rows = defect_rows("b-101", 5, control_hits=0, treatment_hits=1)
    rows += defect_rows("b-102", 5, 0, 0)
    rows += defect_rows("b-103", 5, 0, 5)
    rows += defect_rows("b-104", 5, 5, 5)
    rows += defect_rows("b-105", 5, 5, 5)
    rows += defect_rows("b-201", 5, 0, 5)
    return rows + control_rows("c-101", 5, control_noise=0, treatment_noise=0)


def test_invalidating_a_representative_does_not_promote_its_twin(tmp_path):
    # The exploit this rule closes: with invalidation applied case-by-case, removing the
    # diff-mode representative leaves its code-mode twin alone in the mutation group, where
    # it is elected representative. The class keeps its five-case count and quietly gains
    # the twin's numbers — enough here to turn NOT CERTIFIED into CERTIFIED on one call.
    rows = twin_promotion_sweep()
    baseline = adjudicate_all(rows, MATCHES_DEFECT)
    before = score(tmp_path, twin_promotion_corpus, rows, baseline)
    entry = class_of(before, "dropped-guard")
    assert (entry.cases, entry.control_found, entry.treatment_found) == (5, 2, 3)
    assert (entry.criterion_met, before.verdict) == (False, NOT_CERTIFIED)
    assert [t.case_id for t in before.backends[0].twins] == ["b-201"]

    invalidating = [
        call("b-101", "treatment", 1, CASE_INVALIDATED) if d["case_id"] == "b-101"
        else d
        for d in baseline
    ]
    after = score(tmp_path, twin_promotion_corpus, rows, invalidating)
    entry = class_of(after, "dropped-guard")
    assert [c.case_id for c in entry.per_case] == ["b-102", "b-103", "b-104", "b-105"]
    assert (entry.cases, entry.certifying) == (4, False)
    assert after.verdict == NOT_CERTIFIED
    # The twin goes with its mutation rather than surviving as mode-sensitivity data.
    assert after.removed_cases == ["b-101", "b-201"]
    assert after.invalidated_cases == ["b-101"]
    assert after.backends[0].twins == []


def test_invalidation_closes_over_the_mutation_not_the_case(tmp_path):
    cases = {c.id: c for c in load_cases(corpus(tmp_path, twinned))}
    assert removed_cases(cases, frozenset({"b-101"})) == {"b-101", "b-201"}
    assert removed_cases(cases, frozenset({"b-201"})) == {"b-101", "b-201"}
    assert removed_cases(cases, frozenset()) == frozenset()


def test_a_code_mode_twin_is_reported_but_never_counted_toward_a_class(tmp_path):
    rows = defect_rows("b-101", 5, control_hits=0, treatment_hits=5)
    rows += defect_rows("b-201", 5, control_hits=0, treatment_hits=5)
    cert = score(tmp_path, twinned, rows, adjudicate_all(rows, MATCHES_DEFECT))
    entry = class_of(cert, "dropped-guard")
    assert (entry.cases, [c.case_id for c in entry.per_case]) == (1, ["b-101"])
    twins = cert.backends[0].twins
    assert [(t.case_id, t.treatment_found) for t in twins] == [("b-201", True)]


# --- the veto ----------------------------------------------------------------------------


def controls_only(directory: Path) -> None:
    write_case(directory, "c-101")
    write_case(directory, "c-102")


def veto_of(tmp_path, rows):
    cert = score(tmp_path, controls_only, rows, adjudicate_all(rows, FALSE_POSITIVE))
    return cert.backends[0].veto


def test_a_controls_high_severity_finding_is_either_noise_or_an_invalidation(tmp_path):
    # The README leaves no third answer: confirmed real means the case was never a clean
    # control, so recording it as a genuine-but-unrelated finding would park a real bug in
    # the corpus as neither noise nor a removal.
    rows = control_rows("c-101", 2, control_noise=0, treatment_noise=1)
    rows += control_rows("c-102", 2, control_noise=0, treatment_noise=0)
    decisions = [call("c-101", "treatment", rep, REAL_UNRELATED) for rep in (1, 2)]
    with pytest.raises(AdjudicationError, match="record case-invalidated"):
        score(tmp_path, controls_only, rows, decisions)


def test_a_real_high_severity_finding_on_a_control_removes_the_case(tmp_path):
    rows = control_rows("c-101", 2, control_noise=0, treatment_noise=1)
    rows += control_rows("c-102", 2, control_noise=0, treatment_noise=0)
    decisions = [call("c-101", "treatment", 1, CASE_INVALIDATED)]
    cert = score(tmp_path, controls_only, rows, decisions)
    assert cert.invalidated_cases == ["c-101"]
    assert [c.case_id for c in cert.backends[0].veto.per_case] == ["c-102"]


def test_the_per_case_bound_allows_exactly_half_a_finding_more(tmp_path):
    # 1 extra false positive over 2 repetitions is +0.5 — the bound is "by no more than".
    rows = control_rows("c-101", 2, control_noise=0, treatment_noise=0)
    rows[1]["raw"] = review([finding(title="noise")])
    rows += control_rows("c-102", 2, control_noise=0, treatment_noise=0)
    veto = veto_of(tmp_path, rows)
    assert [(c.case_id, float(c.delta), c.within_bound) for c in veto.per_case] == [
        ("c-101", 0.5, True), ("c-102", 0.0, True),
    ]
    assert veto.per_case_ok is True
    # It still fails the aggregate condition, which allows no increase at all.
    assert (veto.aggregate_ok, veto.holds) == (False, False)


def test_a_quarter_finding_past_the_bound_is_already_over(tmp_path):
    # 3 extra false positives over 4 repetitions is +0.75: the smallest overshoot this
    # corpus can express, and the bound is not a rounded-off "about half a finding".
    rows = control_rows("c-101", 4, control_noise=0, treatment_noise=0)
    for index in (1, 3, 5):
        rows[index]["raw"] = review([finding(title="noise")])
    rows += control_rows("c-102", 4, control_noise=0, treatment_noise=0)
    veto = veto_of(tmp_path, rows)
    assert [(c.case_id, float(c.delta), c.within_bound) for c in veto.per_case] == [
        ("c-101", 0.75, False), ("c-102", 0.0, True),
    ]
    assert (veto.per_case_ok, veto.holds) == (False, False)


def test_a_per_case_increase_beyond_the_bound_trips_the_veto(tmp_path):
    rows = control_rows("c-101", 4, control_noise=1, treatment_noise=3)
    rows += control_rows("c-102", 4, control_noise=4, treatment_noise=0)
    veto = veto_of(tmp_path, rows)
    # The aggregate improves — 5/8 against 4/8 — and the per-case rule still refuses it.
    assert (float(veto.control_mean), float(veto.treatment_mean)) == (2.5, 1.5)
    assert veto.aggregate_ok is True
    assert [(c.case_id, c.within_bound) for c in veto.per_case] == [
        ("c-101", False), ("c-102", True),
    ]
    assert veto.holds is False


def test_the_aggregate_condition_allows_no_increase_at_all(tmp_path):
    rows = control_rows("c-101", 4, control_noise=1, treatment_noise=1)
    rows += control_rows("c-102", 4, control_noise=0, treatment_noise=0)
    rows[1]["raw"] = review([finding(title="noise 0"), finding(title="extra")])
    veto = veto_of(tmp_path, rows)
    assert [c.within_bound for c in veto.per_case] == [True, True]
    assert (float(veto.control_mean), float(veto.treatment_mean)) == (0.5, 0.625)
    assert (veto.aggregate_ok, veto.holds) == (False, False)


def test_a_sweep_with_no_clean_control_cannot_show_the_veto_holds(tmp_path):
    rows = defect_rows("b-101", 5, control_hits=0, treatment_hits=5)
    cert = score(
        tmp_path, lambda d: defect_case(d, "b-101"), rows,
        adjudicate_all(rows, MATCHES_DEFECT),
    )
    assert cert.verdict == VETOED
    assert "cannot be shown to hold" in " ".join(cert.reasons)


def test_a_medium_finding_on_a_control_is_outside_the_veto_entirely(tmp_path):
    rows = control_rows("c-101", 2, control_noise=0, treatment_noise=0)
    rows[1]["raw"] = review([finding(severity="medium", title="a nit")])
    # Nothing is recorded for it and the sweep still scores: the veto reads CRITICAL and
    # HIGH only, so a medium finding needs no call and contributes to no number.
    cert = score(tmp_path, controls_only, rows, [])
    veto = cert.backends[0].veto
    assert (veto.treatment_total, veto.holds) == (0, True)


# --- case invalidation --------------------------------------------------------------------


def test_invalidating_a_defect_case_removes_it_from_its_class_denominator(tmp_path):
    rows = [r for n in range(1, 6) for r in defect_rows(f"b-{100 + n}", 5, 0, 5)]
    rows += control_rows("c-101", 5, control_noise=0, treatment_noise=0)

    def build(directory):
        one_defect_class(directory, 5)
        write_case(directory, "c-101")

    decisions = adjudicate_all(rows, MATCHES_DEFECT)
    full = score(tmp_path, build, rows, decisions)
    assert (class_of(full, "dropped-guard").cases, full.verdict) == (5, CERTIFIED)

    # One mutant turns out not to be a defect. The class drops to four independent cases,
    # which is below the bar, so nothing in the sweep can certify any more.
    kept = [d for d in decisions if d["case_id"] != "b-105"]
    kept.append(call("b-105", "treatment", 1, CASE_INVALIDATED))
    reduced = score(tmp_path, build, rows, kept)
    entry = class_of(reduced, "dropped-guard")
    assert (entry.cases, entry.certifying) == (4, False)
    assert reduced.verdict == NOT_CERTIFIED
    assert reduced.invalidated_cases == ["b-105"]


def test_an_invalidated_control_leaves_both_veto_conditions(tmp_path):
    rows = control_rows("c-101", 2, control_noise=0, treatment_noise=2)
    rows += control_rows("c-102", 2, control_noise=1, treatment_noise=1)
    decisions = [call("c-101", "treatment", 1, CASE_INVALIDATED)]
    decisions += [call("c-102", role, rep, FALSE_POSITIVE)
                  for role in ARMS for rep in (1, 2)]
    veto = score(tmp_path, controls_only, rows, decisions).backends[0].veto
    assert [c.case_id for c in veto.per_case] == ["c-102"]
    assert (veto.control_runs, veto.treatment_runs) == (2, 2)
    assert veto.holds is True


# --- the success criterion ------------------------------------------------------------------


def class_sweep(cases: int, control_found: int, treatment_found: int,
                reps: int = 5) -> list[dict]:
    """A class of `cases` independent mutants, plus a clean control the veto can read."""
    rows = []
    for n in range(1, cases + 1):
        rows += defect_rows(
            f"b-{100 + n}", reps,
            control_hits=reps if n <= control_found else 0,
            treatment_hits=reps if n <= treatment_found else 0,
        )
    return rows + control_rows("c-101", reps, control_noise=0, treatment_noise=0)


def class_corpus(cases: int):
    def build(directory: Path) -> None:
        one_defect_class(directory, cases)
        write_case(directory, "c-101")
    return build


def criterion(tmp_path, cases, control_found, treatment_found, reps=5):
    rows = class_sweep(cases, control_found, treatment_found, reps)
    cert = score(tmp_path, class_corpus(cases), rows, adjudicate_all(rows, MATCHES_DEFECT))
    return cert, class_of(cert, "dropped-guard")


def test_both_conditions_must_hold(tmp_path):
    # 4 -> 5 is +25% relative but only +1 defect, and the absolute half refuses it.
    cert, entry = criterion(tmp_path, cases=5, control_found=4, treatment_found=5)
    assert (entry.absolute_gain, float(entry.relative_gain)) == (1, 0.25)
    assert (entry.criterion_met, cert.verdict) == (False, NOT_CERTIFIED)


def test_a_two_defect_gain_on_a_wide_class_still_needs_twenty_percent(tmp_path):
    # +2 defects out of 13 cases, off a baseline of 11: 18.2% relative, so the ratio half
    # refuses what the absolute half would have allowed.
    cert, entry = criterion(tmp_path, cases=13, control_found=11, treatment_found=13)
    assert entry.absolute_gain == 2
    assert round(float(entry.relative_gain), 4) == 0.1818
    assert (entry.criterion_met, cert.verdict) == (False, NOT_CERTIFIED)


def test_exactly_twenty_percent_with_two_defects_clears_the_bar(tmp_path):
    cert, entry = criterion(tmp_path, cases=13, control_found=10, treatment_found=12)
    assert (entry.absolute_gain, float(entry.relative_gain)) == (2, 0.2)
    assert (entry.criterion_met, cert.verdict) == (True, CERTIFIED)


def test_a_zero_baseline_is_judged_on_the_absolute_condition_alone(tmp_path):
    cert, entry = criterion(tmp_path, cases=5, control_found=0, treatment_found=2)
    assert entry.relative_gain is None
    assert (entry.criterion_met, cert.verdict) == (True, CERTIFIED)


def test_a_zero_baseline_still_needs_two_defects(tmp_path):
    cert, entry = criterion(tmp_path, cases=5, control_found=0, treatment_found=1)
    assert (entry.absolute_gain, entry.relative_gain) == (1, None)
    assert (entry.criterion_met, cert.verdict) == (False, NOT_CERTIFIED)


def test_a_sweep_below_protocol_depth_cannot_certify_however_good_the_ratio(tmp_path):
    # At n=1 a strict majority is one run, so the majority rule degenerates into exactly
    # the any-run rule it exists to reject. The recall arithmetic still clears both halves
    # of the criterion; the depth gate is what refuses it.
    cert, entry = criterion(
        tmp_path, cases=13, control_found=10, treatment_found=12, reps=1,
    )
    assert (entry.absolute_gain, float(entry.relative_gain)) == (2, 0.2)
    assert (entry.criterion_met, entry.min_reps) == (True, 1)
    assert (entry.certifying, cert.verdict) == (False, NOT_CERTIFIED)
    assert f"below the protocol n>={PROTOCOL_MIN_REPS}" in " ".join(cert.reasons)


def test_one_shallow_case_takes_its_whole_class_out_of_the_verdict(tmp_path):
    rows = [r for n in range(1, 5) for r in defect_rows(f"b-{100 + n}", 5, 0, 5)]
    rows += defect_rows("b-105", 4, 0, 4)
    rows += control_rows("c-101", 5, control_noise=0, treatment_noise=0)
    cert = score(
        tmp_path, class_corpus(5), rows, adjudicate_all(rows, MATCHES_DEFECT),
    )
    entry = class_of(cert, "dropped-guard")
    assert (entry.cases, entry.treatment_found, entry.criterion_met) == (5, 5, True)
    assert (entry.min_reps, entry.certifying) == (4, False)
    assert cert.verdict == NOT_CERTIFIED


def test_shallow_clean_controls_cannot_certify_but_still_block(tmp_path):
    def build(directory):
        one_defect_class(directory, 5)
        write_case(directory, "c-101")

    defects = [r for n in range(1, 6) for r in defect_rows(f"b-{100 + n}", 5, 0, 5)]

    quiet = defects + control_rows("c-101", 2, control_noise=0, treatment_noise=0)
    cert = score(tmp_path, build, quiet, adjudicate_all(quiet, MATCHES_DEFECT))
    assert class_of(cert, "dropped-guard").criterion_met is True
    assert cert.backends[0].veto.holds is True
    assert cert.backends[0].veto.at_protocol_depth is False
    assert cert.verdict == NOT_CERTIFIED
    assert "measured too thinly to certify against" in " ".join(cert.reasons)

    # Blocking is not gated on depth: two repetitions are enough to show harm, and
    # refusing to act on that would be the wrong direction to fail in.
    noisy = defects + control_rows("c-101", 2, control_noise=0, treatment_noise=3)
    cert = score(tmp_path, build, noisy, adjudicate_split(noisy))
    assert cert.verdict == VETOED


def test_a_class_below_five_cases_is_reported_and_cannot_certify(tmp_path):
    rows = [r for n in range(1, 4) for r in defect_rows(f"b-{100 + n}", 5, 0, 5)]
    rows += control_rows("c-101", 5, control_noise=0, treatment_noise=0)

    def build(directory):
        one_defect_class(directory, 3, defect_class="swallowed-error")
        write_case(directory, "c-101")

    cert = score(tmp_path, build, rows, adjudicate_all(rows, MATCHES_DEFECT))
    entry = class_of(cert, "swallowed-error")
    assert (entry.cases, entry.treatment_found, entry.criterion_met) == (3, 3, True)
    assert entry.certifying is False
    assert cert.verdict == NOT_CERTIFIED


# --- the completeness gate -------------------------------------------------------------------


def test_an_unadjudicated_control_high_finding_refuses_the_verdict(tmp_path):
    rows = control_rows("c-101", 2, control_noise=0, treatment_noise=1)
    rows += control_rows("c-102", 2, control_noise=0, treatment_noise=0)
    decisions = [call("c-101", "treatment", 1, FALSE_POSITIVE)]
    with pytest.raises(IncompleteAdjudication) as excinfo:
        score(tmp_path, controls_only, rows, decisions)
    assert [(m.case_id, m.rep, m.arm_role) for m in excinfo.value.missing] == [
        ("c-101", 2, "treatment"),
    ]


def test_an_unadjudicated_finding_on_the_defects_own_lines_refuses_the_verdict(tmp_path):
    rows = defect_rows("b-101", 1, control_hits=0, treatment_hits=1)
    rows += control_rows("c-101", 1, control_noise=0, treatment_noise=0)

    def build(directory):
        defect_case(directory, "b-101")
        write_case(directory, "c-101")

    with pytest.raises(IncompleteAdjudication) as excinfo:
        score(tmp_path, build, rows, [])
    assert [m.case_id for m in excinfo.value.missing] == ["b-101"]


def test_a_finding_elsewhere_in_a_defect_case_needs_no_call(tmp_path):
    rows = pair("b-101", 1, [], [finding(file="rocket_review/cli.py", line=3)])
    rows += control_rows("c-101", 1, control_noise=0, treatment_noise=0)

    def build(directory):
        defect_case(directory, "b-101")
        write_case(directory, "c-101")

    cert = score(tmp_path, build, rows, [])
    assert class_of(cert, "dropped-guard").per_case[0].treatment_found is False


def test_an_invalidated_case_needs_no_further_calls(tmp_path):
    rows = control_rows("c-101", 2, control_noise=0, treatment_noise=3)
    rows += control_rows("c-102", 2, control_noise=0, treatment_noise=0)
    cert = score(
        tmp_path, controls_only, rows, [call("c-101", "treatment", 1, CASE_INVALIDATED)],
    )
    assert cert.invalidated_cases == ["c-101"]


# --- artifact validation ------------------------------------------------------------------


def load_bad(tmp_path, document: str):
    path = tmp_path / "bad.yaml"
    path.write_text(document, encoding="utf-8")
    return load_adjudications(path)


def test_a_decision_outside_the_enum_is_refused(tmp_path):
    with pytest.raises(AdjudicationError, match="decision must be one of"):
        load_bad(tmp_path, yaml.safe_dump({
            "sweep_id": SWEEP,
            "decisions": [call("b-101", "control", 1, "looks-right")],
        }))


def test_a_decision_without_a_rationale_is_refused(tmp_path):
    entry = call("b-101", "control", 1, MATCHES_DEFECT)
    entry["rationale"] = "   "
    with pytest.raises(AdjudicationError, match="rationale is required"):
        load_bad(tmp_path, yaml.safe_dump({"sweep_id": SWEEP, "decisions": [entry]}))


def test_a_multi_line_rationale_is_refused(tmp_path):
    entry = call("b-101", "control", 1, MATCHES_DEFECT)
    entry["rationale"] = "line one\nline two"
    with pytest.raises(AdjudicationError, match="one line"):
        load_bad(tmp_path, yaml.safe_dump({"sweep_id": SWEEP, "decisions": [entry]}))


def test_two_calls_on_one_finding_are_refused(tmp_path):
    entry = call("b-101", "control", 1, MATCHES_DEFECT)
    with pytest.raises(AdjudicationError, match="one finding, one call"):
        load_bad(tmp_path, yaml.safe_dump({"sweep_id": SWEEP, "decisions": [entry, entry]}))


def test_a_missing_key_names_itself(tmp_path):
    entry = call("b-101", "control", 1, MATCHES_DEFECT)
    del entry["rep"]
    with pytest.raises(AdjudicationError, match="missing keys: rep"):
        load_bad(tmp_path, yaml.safe_dump({"sweep_id": SWEEP, "decisions": [entry]}))


def test_an_artifact_without_a_sweep_id_is_refused(tmp_path):
    with pytest.raises(AdjudicationError, match="sweep_id is required"):
        load_bad(tmp_path, yaml.safe_dump({"decisions": []}))


def test_a_decision_naming_no_run_is_refused(tmp_path):
    rows = control_rows("c-101", 1, control_noise=0, treatment_noise=0)
    rows += control_rows("c-102", 1, control_noise=0, treatment_noise=0)
    with pytest.raises(AdjudicationError, match="no such run in the results file"):
        score(tmp_path, controls_only, rows, [call("c-101", "control", 9, FALSE_POSITIVE)])


def test_a_finding_index_past_the_end_of_a_review_is_refused(tmp_path):
    rows = control_rows("c-101", 1, control_noise=1, treatment_noise=0)
    rows += control_rows("c-102", 1, control_noise=0, treatment_noise=0)
    decisions = [call("c-101", "control", 1, FALSE_POSITIVE, index=0),
                 call("c-101", "control", 1, FALSE_POSITIVE, index=4)]
    with pytest.raises(AdjudicationError, match="there is no #4"):
        score(tmp_path, controls_only, rows, decisions)


def test_matching_a_defect_the_finding_does_not_even_cite_is_refused(tmp_path):
    rows = pair("b-101", 1, [], [finding(file="rocket_review/cli.py", line=3)])
    with pytest.raises(AdjudicationError, match="fails rule 1 or 2"):
        score(
            tmp_path, lambda d: defect_case(d, "b-101"), rows,
            [call("b-101", "treatment", 1, MATCHES_DEFECT)],
        )


def test_matching_a_defect_outside_its_span_is_refused(tmp_path):
    rows = pair("b-101", 1, [], [on_defect(line=SPAN[1] + 1)])
    with pytest.raises(AdjudicationError, match="fails rule 1 or 2"):
        score(
            tmp_path, lambda d: defect_case(d, "b-101"), rows,
            [call("b-101", "treatment", 1, MATCHES_DEFECT)],
        )


def test_a_finding_with_no_line_leaves_rule_two_to_the_human(tmp_path):
    rows = pair("b-101", 1, [], [on_defect(line=None)])
    rows += control_rows("c-101", 1, control_noise=0, treatment_noise=0)

    def build(directory):
        defect_case(directory, "b-101")
        write_case(directory, "c-101")

    cert = score(
        tmp_path, build, rows, [call("b-101", "treatment", 1, MATCHES_DEFECT)],
    )
    assert class_of(cert, "dropped-guard").per_case[0].treatment_found is True


def test_a_false_positive_on_a_defect_case_is_refused(tmp_path):
    rows = pair("b-101", 1, [], [on_defect()])
    with pytest.raises(AdjudicationError, match="record real-unrelated"):
        score(
            tmp_path, lambda d: defect_case(d, "b-101"), rows,
            [call("b-101", "treatment", 1, FALSE_POSITIVE)],
        )


def test_invalidating_a_defect_case_needs_a_finding_about_the_defect(tmp_path):
    # Removing a case is the largest thing one call can do to the arithmetic, so the claim
    # that the seeded defect is not a defect has to be made on a finding about it.
    rows = pair("b-101", 1, [], [finding(file="rocket_review/cli.py", line=3)])
    with pytest.raises(AdjudicationError, match="case-invalidated but the finding fails"):
        score(
            tmp_path, lambda d: defect_case(d, "b-101"), rows,
            [call("b-101", "treatment", 1, CASE_INVALIDATED)],
        )


def test_matching_a_defect_on_a_clean_control_is_refused(tmp_path):
    rows = control_rows("c-101", 1, control_noise=0, treatment_noise=1)
    rows += control_rows("c-102", 1, control_noise=0, treatment_noise=0)
    with pytest.raises(AdjudicationError, match="which has no defect"):
        score(tmp_path, controls_only, rows, [call("c-101", "treatment", 1, MATCHES_DEFECT)])


def test_the_corpus_and_the_results_must_agree_about_what_is_a_control(tmp_path):
    rows = control_rows("c-101", 1, control_noise=0, treatment_noise=0)
    results = write_results(tmp_path, rows)
    adjudications = write_adjudications(tmp_path, [])
    cases_dir = corpus(tmp_path, lambda d: defect_case(d, "c-101"))
    assert main([str(results), "--adjudications", str(adjudications),
                 "--cases", str(cases_dir), "--repo", str(tmp_path)]) == 3


def test_a_results_file_holding_two_sweeps_is_refused(tmp_path, capsys):
    rows = control_rows("c-101", 1, control_noise=0, treatment_noise=0)
    rows += [row("c-102", "control", 1, is_control=True, sweep_id="other")]
    results = write_results(tmp_path, rows)
    adjudications = write_adjudications(tmp_path, [])
    cases_dir = corpus(tmp_path, controls_only)
    assert main([str(results), "--adjudications", str(adjudications),
                 "--cases", str(cases_dir), "--repo", str(tmp_path)]) == 3
    assert "cannot make one" in capsys.readouterr().err


def test_an_artifact_for_another_sweep_is_refused(tmp_path, capsys):
    rows = control_rows("c-101", 1, control_noise=0, treatment_noise=0)
    results = write_results(tmp_path, rows)
    adjudications = write_adjudications(tmp_path, [], sweep_id="b" * 32)
    cases_dir = corpus(tmp_path, controls_only)
    assert main([str(results), "--adjudications", str(adjudications),
                 "--cases", str(cases_dir), "--repo", str(tmp_path)]) == 3
    assert "is for sweep" in capsys.readouterr().err


# --- the cross-arm consistency warning ------------------------------------------------------


def test_identical_finding_text_called_differently_across_arms_is_flagged(tmp_path, capsys):
    rows = pair("b-101", 1, [on_defect()], [on_defect()])
    rows += control_rows("c-101", 1, control_noise=0, treatment_noise=0)

    def build(directory):
        defect_case(directory, "b-101")
        write_case(directory, "c-101")

    decisions = [call("b-101", "control", 1, REAL_UNRELATED),
                 call("b-101", "treatment", 1, MATCHES_DEFECT)]
    cert = score(tmp_path, build, rows, decisions)
    assert [(c.title, c.by_role) for c in cert.cross_arm] == [
        ("The guard no longer excludes anything",
         {"control": [REAL_UNRELATED], "treatment": [MATCHES_DEFECT]}),
    ]
    # Flagged, never blocking: the same words can be right about one diff and wrong about
    # another, so this is a prompt to go and read them.
    assert cert.verdict == NOT_CERTIFIED
    print_report(cert)
    assert "adjudicated differently across arms" in capsys.readouterr().out


def test_the_same_call_in_both_arms_is_not_flagged(tmp_path):
    rows = pair("b-101", 1, [on_defect()], [on_defect()])
    rows += control_rows("c-101", 1, control_noise=0, treatment_noise=0)

    def build(directory):
        defect_case(directory, "b-101")
        write_case(directory, "c-101")

    cert = score(tmp_path, build, rows, adjudicate_split(rows))
    assert cert.cross_arm == []


# --- end to end through the CLI ---------------------------------------------------------


def run_cli(tmp_path, rows, decisions, build, *extra):
    results = write_results(tmp_path, rows)
    adjudications = write_adjudications(tmp_path, decisions)
    cases_dir = corpus(tmp_path, build)
    return main([str(results), "--adjudications", str(adjudications),
                 "--cases", str(cases_dir), "--repo", str(tmp_path), *extra])


def certification_sweep(treatment_noise: int) -> list[dict]:
    """Five dropped-guard cases where treatment finds two more, plus two clean controls."""
    rows = []
    for n in range(1, 6):
        rows += defect_rows(
            f"b-{100 + n}", 5,
            control_hits=5 if n <= 2 else 0, treatment_hits=5 if n <= 4 else 0,
        )
    rows += control_rows("c-101", 5, control_noise=1, treatment_noise=treatment_noise)
    rows += control_rows("c-102", 5, control_noise=1, treatment_noise=1)
    return rows


def certification_corpus(directory: Path) -> None:
    one_defect_class(directory, 5)
    write_case(directory, "c-101")
    write_case(directory, "c-102")


def test_a_clean_improvement_certifies(tmp_path, capsys):
    rows = certification_sweep(treatment_noise=1)
    assert run_cli(tmp_path, rows, adjudicate_split(rows), certification_corpus) == 0
    out = capsys.readouterr().out
    assert final_verdict(out) == CERTIFIED
    assert "dropped-guard [certifying, 5 independent cases at n>=5]" in out
    assert "control 2/5  treatment 4/5  +2 (+100.0%)  criterion met" in out
    assert "veto holds" in out


def test_the_same_improvement_bought_with_noise_is_vetoed(tmp_path, capsys):
    # Identical recall; the treatment now invents four extra high-severity findings per
    # run on one clean control, which is exactly the trade the veto refuses.
    rows = certification_sweep(treatment_noise=5)
    assert run_cli(tmp_path, rows, adjudicate_split(rows), certification_corpus) == 2
    out = capsys.readouterr().out
    assert final_verdict(out) == VETOED
    # The success criterion is still met, which is the point: a veto is not a tie-break.
    assert "criterion met" in out
    assert "veto TRIPPED" in out
    assert "exceeds control 1.000 by more than 0.500" in out


# --- more than one backend ----------------------------------------------------------------


def second_backend(certifies: bool, noisy: bool) -> list[dict]:
    """The same five-case class under a second backend, tuned to certify or not."""
    rows = []
    for n in range(1, 6):
        rows += defect_rows(
            f"b-{100 + n}", 5, control_hits=5 if n <= 2 else 0,
            treatment_hits=5 if n <= (4 if certifies else 3) else 0, backend="claude",
        )
    rows += control_rows("c-101", 5, 1, 5 if noisy else 1, backend="claude")
    return rows + control_rows("c-102", 5, 1, 1, backend="claude")


def test_a_veto_on_one_backend_vetoes_the_whole_sweep(tmp_path):
    rows = certification_sweep(treatment_noise=1) + second_backend(
        certifies=True, noisy=True,
    )
    cert = score(tmp_path, certification_corpus, rows, adjudicate_split(rows))
    assert [(b.backend, b.verdict) for b in cert.backends] == [
        ("claude", VETOED), ("codex", CERTIFIED),
    ]
    assert cert.verdict == VETOED
    assert all(r.startswith("claude: ") for r in cert.reasons)


def test_certification_needs_every_backend_to_certify(tmp_path):
    rows = certification_sweep(treatment_noise=1) + second_backend(
        certifies=False, noisy=False,
    )
    cert = score(tmp_path, certification_corpus, rows, adjudicate_split(rows))
    assert [(b.backend, b.verdict) for b in cert.backends] == [
        ("claude", NOT_CERTIFIED), ("codex", CERTIFIED),
    ]
    # A prompt ships to both at once, so one backend getting sharper while the other does
    # not is not an improvement to rr.
    assert cert.verdict == NOT_CERTIFIED
    assert all(r.startswith("claude: ") for r in cert.reasons)


def test_two_arms_in_one_role_for_one_backend_are_refused(tmp_path):
    rows = control_rows("c-101", 1, control_noise=0, treatment_noise=0)
    rows += [row("c-101", "control", 2, is_control=True, arm="another-control"),
             row("c-101", "treatment", 2, is_control=True)]
    with pytest.raises(AdjudicationError, match="exactly one control arm"):
        score(tmp_path, controls_only, rows, [])


def test_the_json_report_carries_the_same_verdict(tmp_path, capsys):
    rows = certification_sweep(treatment_noise=1)
    assert run_cli(
        tmp_path, rows, adjudicate_split(rows), certification_corpus, "--json",
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == CERTIFIED
    assert report["backends"][0]["veto"]["holds"] is True
    classes = report["backends"][0]["classes"]
    assert [(c["defect_class"], c["control_found"], c["treatment_found"]) for c in classes] == [
        ("dropped-guard", 2, 4),
    ]


def test_pending_drafts_every_call_the_rules_need_and_nothing_else(tmp_path, capsys):
    rows = certification_sweep(treatment_noise=1)
    results = write_results(tmp_path, rows)
    cases_dir = corpus(tmp_path, certification_corpus)
    assert main([str(results), "--pending", "--cases", str(cases_dir),
                 "--repo", str(tmp_path)]) == EXIT_DRAFTED
    draft = yaml.safe_load(capsys.readouterr().out)
    assert draft["sweep_id"] == SWEEP
    assert len(draft["decisions"]) == len(adjudicate_split(rows))
    assert {d["decision"] for d in draft["decisions"]} == {"TODO"}

    # A filled-in draft scores; the TODO placeholder does not, so the skeleton can never
    # become a default.
    for entry in draft["decisions"]:
        entry["decision"] = (
            MATCHES_DEFECT if entry["case_id"].startswith("b-") else FALSE_POSITIVE
        )
        entry["rationale"] = "read against defect.expected"
    filled = write_adjudications(tmp_path, draft["decisions"], name="filled.yaml")
    assert main([str(results), "--adjudications", str(filled), "--cases", str(cases_dir),
                 "--repo", str(tmp_path)]) == 0


def test_pending_skips_what_is_already_recorded(tmp_path, capsys):
    rows = control_rows("c-101", 2, control_noise=1, treatment_noise=1)
    rows += control_rows("c-102", 2, control_noise=0, treatment_noise=0)
    results = write_results(tmp_path, rows)
    recorded = write_adjudications(
        tmp_path, [call("c-101", "control", 1, FALSE_POSITIVE)],
    )
    cases_dir = corpus(tmp_path, controls_only)
    assert main([str(results), "--pending", "--adjudications", str(recorded),
                 "--cases", str(cases_dir), "--repo", str(tmp_path)]) == EXIT_DRAFTED
    draft = yaml.safe_load(capsys.readouterr().out)
    assert [(d["case_id"], d["arm_role"], d["rep"]) for d in draft["decisions"]] == [
        ("c-101", "control", 2), ("c-101", "treatment", 1), ("c-101", "treatment", 2),
    ]


def test_an_incomplete_adjudication_lists_what_is_missing_and_yields_no_verdict(
    tmp_path, capsys,
):
    rows = certification_sweep(treatment_noise=1)
    assert run_cli(tmp_path, rows, [], certification_corpus) == 3
    captured = capsys.readouterr()
    assert "still need a recorded decision" in captured.err
    assert "b-101 codex control(before) rep1 #0" in captured.err
    assert captured.out == ""


def test_a_sweep_that_lost_every_repetition_yields_no_verdict(tmp_path, capsys):
    rows = control_rows("c-101", 1, control_noise=0, treatment_noise=0)
    rows[1].update(outcome=BACKEND_ERROR, raw="", backend_error="codex timed out")
    assert run_cli(tmp_path, rows, [], controls_only) == 3
    assert "nothing to certify" in capsys.readouterr().err


def test_adjudications_are_required_unless_drafting(tmp_path, capsys):
    results = write_results(tmp_path, control_rows("c-101", 1, 0, 0))
    assert main([str(results)]) == 3
    assert "--adjudications is required" in capsys.readouterr().err


def test_a_usage_error_never_lands_on_a_verdict_code(tmp_path):
    # argparse exits 2 by default, and 2 is VETOED. A caller gating on status would read a
    # mistyped flag as a blocked prompt change.
    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path / "paired.jsonl"), "--not-a-flag"])
    assert excinfo.value.code == EXIT_USAGE
    assert excinfo.value.code not in (0, 1, 2, 3)


def test_drafting_does_not_exit_on_a_verdict_code(tmp_path, capsys):
    rows = control_rows("c-101", 1, control_noise=1, treatment_noise=0)
    results = write_results(tmp_path, rows)
    cases_dir = corpus(tmp_path, controls_only)
    status = main([str(results), "--pending", "--cases", str(cases_dir),
                   "--repo", str(tmp_path)])
    assert (status, EXIT_DRAFTED) == (4, 4)
    assert status not in (0, 1, 2)
    assert yaml.safe_load(capsys.readouterr().out)["decisions"]
