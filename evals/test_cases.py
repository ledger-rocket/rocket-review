"""Manifest validation and case materialization, against a throwaway repository."""

from pathlib import Path

import pytest
from cases import CASES_DIR, CaseError, load_case, load_cases, materialize, remove_worktree
from conftest import git

MUTANT = """\
id: m-001
mode: diff
source: mutant
diff: cases/m-001.patch
repo_commit: {oid}
defect:
  class: dropped-null-check
  file: sample.py
  span: [3, 4]
  expected: the empty-label guard is gone, so rank('') raises instead of returning 0
"""


def write_manifest(root: Path, name: str, text: str) -> Path:
    cases = root / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    path = cases / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def write_mutant_patch(root: Path, git_repo: Path, oid: str) -> None:
    """Produce a patch that drops the empty-label guard from the repo at `oid`."""
    git(git_repo, "checkout", "-q", oid, "--", "sample.py")
    text = (git_repo / "sample.py").read_text(encoding="utf-8")
    (git_repo / "sample.py").write_text(
        text.replace("    if not label:\n        return 0\n", ""), encoding="utf-8"
    )
    patch = git(git_repo, "diff").stdout
    git(git_repo, "checkout", "-q", "--", "sample.py")
    (root / "cases").mkdir(parents=True, exist_ok=True)
    (root / "cases" / "m-001.patch").write_text(patch, encoding="utf-8")


# --- the shipped corpus ---------------------------------------------------------------


def test_shipped_manifests_load():
    cases = load_cases(CASES_DIR)
    assert {c.id for c in cases} == {"b-001", "c-001", "p-001"}
    assert {c.source for c in cases} == {"mutant", "merged-pr", "seeded-plan"}


def test_shipped_defect_case_points_at_a_real_patch():
    case = next(c for c in load_cases(CASES_DIR) if c.id == "b-001")
    assert case.defect is not None
    assert (case.root / case.diff).is_file()
    assert case.defect.span == (114, 117)


def test_shipped_control_case_declares_no_defect():
    case = next(c for c in load_cases(CASES_DIR) if c.id == "c-001")
    assert case.is_control


# --- validation ------------------------------------------------------------------------


def test_id_must_match_the_file_name(tmp_path):
    path = write_manifest(tmp_path, "other", MUTANT.format(oid="abc1234"))
    with pytest.raises(CaseError, match="must match the file name"):
        load_case(path)


def test_unknown_top_level_key_is_rejected(tmp_path):
    path = write_manifest(tmp_path, "m-001", MUTANT.format(oid="abc1234") + "notes: hi\n")
    with pytest.raises(CaseError, match="unknown keys: notes"):
        load_case(path)


def test_unknown_mode_is_rejected(tmp_path):
    text = MUTANT.format(oid="abc1234").replace("mode: diff", "mode: vibes")
    with pytest.raises(CaseError, match="mode must be one of"):
        load_case(write_manifest(tmp_path, "m-001", text))


def test_mutant_without_a_patch_is_rejected(tmp_path):
    text = MUTANT.format(oid="abc1234").replace("diff: cases/m-001.patch\n", "")
    with pytest.raises(CaseError, match="mutant case needs `diff`"):
        load_case(write_manifest(tmp_path, "m-001", text))


def test_merged_pr_with_a_patch_is_rejected(tmp_path):
    text = MUTANT.format(oid="abc1234").replace("source: mutant", "source: merged-pr")
    with pytest.raises(CaseError, match="reviewed at repo_commit"):
        load_case(write_manifest(tmp_path, "m-001", text))


def test_defect_span_must_be_an_ordered_pair(tmp_path):
    text = MUTANT.format(oid="abc1234").replace("span: [3, 4]", "span: [9, 4]")
    with pytest.raises(CaseError, match="1 <= start <= end"):
        load_case(write_manifest(tmp_path, "m-001", text))


def test_defect_span_must_be_two_integers(tmp_path):
    text = MUTANT.format(oid="abc1234").replace("span: [3, 4]", 'span: "3-4"')
    with pytest.raises(CaseError, match="two-element"):
        load_case(write_manifest(tmp_path, "m-001", text))


def test_unknown_defect_key_is_rejected(tmp_path):
    text = MUTANT.format(oid="abc1234") + "  severity: high\n"
    with pytest.raises(CaseError, match="unknown defect keys: severity"):
        load_case(write_manifest(tmp_path, "m-001", text))


def test_unknown_case_id_filter_is_rejected(tmp_path):
    write_manifest(tmp_path, "m-001", MUTANT.format(oid="abc1234"))
    with pytest.raises(CaseError, match="unknown case id"):
        load_cases(tmp_path / "cases", {"m-002"})


def test_empty_directory_is_rejected(tmp_path):
    (tmp_path / "cases").mkdir()
    with pytest.raises(CaseError, match="no \\*.yaml case manifests"):
        load_cases(tmp_path / "cases")


# --- materialization -------------------------------------------------------------------


def test_mutant_is_staged_as_a_patched_worktree(tmp_path, git_repo, head_oid):
    write_mutant_patch(tmp_path, git_repo, head_oid)
    case = load_case(write_manifest(tmp_path, "m-001", MUTANT.format(oid=head_oid)))
    staged = materialize(case, git_repo, tmp_path / "work")
    try:
        assert staged.rr_args == ["--diff"]
        assert staged.worktree == staged.cwd
        # The defect is present in the working tree and absent from HEAD: that difference
        # is exactly what `rr --diff` puts in front of the backend.
        assert "if not label" not in (staged.cwd / "sample.py").read_text(encoding="utf-8")
        assert git(staged.cwd, "rev-parse", "HEAD").stdout.strip() == head_oid
        assert "sample.py" in git(staged.cwd, "diff", "--name-only").stdout
    finally:
        remove_worktree(git_repo, staged.worktree)


def test_a_patch_that_does_not_apply_fails_loudly_and_leaves_no_worktree(
    tmp_path, git_repo, head_oid,
):
    (tmp_path / "cases").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cases" / "m-001.patch").write_text(
        "--- a/sample.py\n+++ b/sample.py\n@@ -1,1 +1,1 @@\n-nothing like the file\n+x\n",
        encoding="utf-8",
    )
    case = load_case(write_manifest(tmp_path, "m-001", MUTANT.format(oid=head_oid)))
    with pytest.raises(CaseError, match="does not apply"):
        materialize(case, git_repo, tmp_path / "work")
    assert "case-m-001" not in git(git_repo, "worktree", "list").stdout


def test_merged_pr_is_reviewed_in_place_at_its_commit(tmp_path, git_repo, head_oid):
    text = (
        f"id: c-001\nmode: diff\nsource: merged-pr\nrepo_commit: {head_oid}\n"
    )
    case = load_case(write_manifest(tmp_path, "c-001", text))
    staged = materialize(case, git_repo, tmp_path / "work")
    assert staged.worktree is None
    assert staged.cwd == git_repo
    assert staged.rr_args == ["--commit", head_oid]


def test_seeded_plan_is_reviewed_as_a_file_argument(tmp_path, git_repo, head_oid):
    (tmp_path / "cases").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cases" / "p-001-plan.md").write_text("# Plan\n", encoding="utf-8")
    text = (
        f"id: p-001\nmode: plan\nsource: seeded-plan\nrepo_commit: {head_oid}\n"
        "path: cases/p-001-plan.md\n"
    )
    case = load_case(write_manifest(tmp_path, "p-001", text))
    staged = materialize(case, git_repo, tmp_path / "work")
    assert staged.worktree is None
    assert staged.rr_args == [str(tmp_path / "cases" / "p-001-plan.md")]


def test_a_missing_artifact_is_rejected(tmp_path, git_repo, head_oid):
    text = (
        f"id: p-001\nmode: plan\nsource: seeded-plan\nrepo_commit: {head_oid}\n"
        "path: cases/absent.md\n"
    )
    case = load_case(write_manifest(tmp_path, "p-001", text))
    with pytest.raises(CaseError, match="not found"):
        materialize(case, git_repo, tmp_path / "work")
