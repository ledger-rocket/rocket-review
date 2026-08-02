"""Manifest validation and case materialization, against a throwaway repository.

The shipped corpus is also checked here, but only in ways that cost nothing: manifests are
read against the git object database at their `repo_commit`, never checked out. Whether a
mutant is a *real* defect is a different and much more expensive question, answered
on demand by `verify_cases.py`; what CI enforces is that the answer is recorded.
"""

import re
import subprocess
from pathlib import Path

import pytest
from cases import CASES_DIR, Case, CaseError, load_case, load_cases, materialize, remove_worktree
from conftest import git
from eval_common import REPO_ROOT

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

SHIPPED = load_cases(CASES_DIR)

#: Which corpus a case belongs to is carried by its id prefix, and the prefix has to agree
#: with the manifest: B is the recall corpus (defect mutants), C the false-positive corpus
#: (clean controls), P the plan set (either).
CORPUS_PREFIXES = {"b": "mutant", "c": "merged-pr", "p": "seeded-plan"}


def blob(commit: str, path: str) -> str | None:
    """The file's content at `commit`, or None if it does not exist there."""
    shown = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{commit}:{path}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return shown.stdout if shown.returncode == 0 else None


def patch_line_delta(patch: str) -> int:
    """How many lines the patch adds to the file, net of what it removes."""
    added = sum(1 for line in patch.splitlines() if line.startswith("+") and line[:3] != "+++")
    removed = sum(1 for line in patch.splitlines() if line.startswith("-") and line[:3] != "---")
    return added - removed


def defect_file_length(case: Case) -> int:
    """Lines in the file the defect sits in, as the case materializes it.

    A mutant's file is its `repo_commit` blob plus whatever the patch does to its length;
    a plan is a standalone artifact on disk. Both are read without checking anything out.
    """
    assert case.defect is not None
    if case.source == "mutant":
        assert case.diff is not None
        original = blob(case.repo_commit, case.defect.file)
        assert original is not None
        patch = (case.root / case.diff).read_text(encoding="utf-8")
        return len(original.splitlines()) + patch_line_delta(patch)
    return len((case.root / case.defect.file).read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize("case", SHIPPED, ids=lambda c: c.id)
def test_every_shipped_manifest_is_internally_consistent(case: Case):
    assert CORPUS_PREFIXES[case.id.split("-")[0]] == case.source
    # rev-parse alone would happily resolve a branch name or an abbreviation; a case has
    # to name one immutable object, or two runs of the same case are not the same case.
    resolved = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet",
         "--end-of-options", f"{case.repo_commit}^{{commit}}"],
        capture_output=True, text=True,
    )
    assert resolved.returncode == 0, f"{case.id}: repo_commit is not a commit in this repo"
    assert resolved.stdout.strip() == case.repo_commit, f"{case.id}: repo_commit is not a full oid"

    if case.path is not None:
        assert (case.root / case.path).is_file(), f"{case.id}: artifact {case.path} is missing"

    if case.defect is not None:
        start, end = case.defect.span
        assert 1 <= start <= end
        assert end <= defect_file_length(case), (
            f"{case.id}: defect.span ends past the end of {case.defect.file}"
        )


@pytest.mark.parametrize("case", [c for c in SHIPPED if c.source == "mutant"], ids=lambda c: c.id)
def test_every_mutant_carries_a_well_formed_patch_and_its_kill_proof(case: Case):
    assert case.diff is not None
    patch = (case.root / case.diff).read_text(encoding="utf-8")
    # Parsed, not applied: applying needs a worktree per case, which is verify_cases.py's
    # job. This catches a truncated or hand-mangled patch, which is the failure CI can see.
    assert patch.strip(), f"{case.id}: patch is empty"
    touched = re.findall(r"^--- a/(.+)$", patch, re.MULTILINE)
    assert touched, f"{case.id}: patch has no ---/+++ file headers"
    assert re.search(r"^@@ -\d+(,\d+)? \+\d+(,\d+)? @@", patch, re.MULTILINE), (
        f"{case.id}: patch has no hunk header"
    )
    assert any(
        line[:1] in "+-" and line[:3] not in ("+++", "---") for line in patch.splitlines()
    ), f"{case.id}: patch has headers but no changed lines"

    assert case.defect is not None, f"{case.id}: a mutant with no defect is a mislabelled control"
    # The manifest's file is what recall is scored against, so it must be one the patch
    # actually mutates — otherwise every finding on this case is scored against the wrong file.
    assert case.defect.file in touched, f"{case.id}: defect.file is not touched by the patch"
    for path in touched:
        assert blob(case.repo_commit, path) is not None, (
            f"{case.id}: {path} does not exist at repo_commit"
        )

    assert case.killed_by, (
        f"{case.id}: no killed_by — run `python evals/verify_cases.py --write`. An "
        "unproven mutant may be equivalent, and scoring recall on one measures nothing."
    )
    for node in case.killed_by:
        test_file = node.split("::")[0]
        assert blob(case.repo_commit, test_file) is not None, (
            f"{case.id}: killed_by names {test_file}, which does not exist at repo_commit"
        )


@pytest.mark.parametrize(
    "case", [c for c in SHIPPED if c.source == "merged-pr"], ids=lambda c: c.id,
)
def test_every_merged_pr_case_is_a_clean_control(case: Case):
    # The absence of a defect block is the entire definition of a clean control, and the
    # veto rule reads it. A merged-pr case that grew one would silently leave the corpus.
    assert case.is_control, f"{case.id}: a merged-pr case must carry no defect block"


def test_the_shipped_corpus_covers_every_source_and_both_labels():
    assert {c.source for c in SHIPPED} == {"mutant", "merged-pr", "seeded-plan"}
    assert {c.mode for c in SHIPPED} == {"diff", "code", "plan"}
    assert any(c.is_control for c in SHIPPED) and any(not c.is_control for c in SHIPPED)


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


def test_killed_by_is_parsed_as_node_ids(tmp_path):
    text = MUTANT.format(oid="abc1234") + "killed_by:\n  - tests/test_x.py::test_y\n"
    case = load_case(write_manifest(tmp_path, "m-001", text))
    assert case.killed_by == ("tests/test_x.py::test_y",)


def test_killed_by_defaults_to_empty_rather_than_none(tmp_path):
    case = load_case(write_manifest(tmp_path, "m-001", MUTANT.format(oid="abc1234")))
    assert case.killed_by == ()


def test_duplicate_killed_by_node_ids_are_rejected(tmp_path):
    node = "tests/test_x.py::test_y"
    text = MUTANT.format(oid="abc1234") + f"killed_by:\n  - {node}\n  - {node}\n"
    with pytest.raises(CaseError, match="duplicate node ids"):
        load_case(write_manifest(tmp_path, "m-001", text))


def test_a_scalar_killed_by_is_rejected(tmp_path):
    text = MUTANT.format(oid="abc1234") + "killed_by: tests/test_x.py::test_y\n"
    with pytest.raises(CaseError, match="must be a list"):
        load_case(write_manifest(tmp_path, "m-001", text))


def test_killed_by_on_a_non_mutant_is_rejected(tmp_path):
    text = (
        "id: c-001\nmode: diff\nsource: merged-pr\nrepo_commit: abc1234\n"
        "killed_by:\n  - tests/test_x.py::test_y\n"
    )
    with pytest.raises(CaseError, match="no patch for a test to kill"):
        load_case(write_manifest(tmp_path, "c-001", text))


def test_a_code_mode_mutant_without_a_defect_is_rejected(tmp_path):
    text = MUTANT.format(oid="abc1234").replace("mode: diff", "mode: code")
    text = text[:text.index("defect:")]
    with pytest.raises(CaseError, match="cannot omit `defect`"):
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


def test_a_code_mode_mutant_is_reviewed_as_its_patched_file(tmp_path, git_repo, head_oid):
    write_mutant_patch(tmp_path, git_repo, head_oid)
    text = MUTANT.format(oid=head_oid).replace("mode: diff", "mode: code")
    case = load_case(write_manifest(tmp_path, "m-001", text))
    staged = materialize(case, git_repo, tmp_path / "work")
    try:
        # Repo-relative, so a finding cites the same path the manifest and tier1's
        # resolver use rather than a throwaway worktree's absolute path.
        assert staged.rr_args == ["sample.py"]
        assert (staged.cwd / "sample.py").is_file()
        assert "if not label" not in (staged.cwd / "sample.py").read_text(encoding="utf-8")
    finally:
        remove_worktree(git_repo, staged.worktree)


def test_a_successful_teardown_says_nothing(tmp_path, git_repo, head_oid, capsys):
    write_mutant_patch(tmp_path, git_repo, head_oid)
    case = load_case(write_manifest(tmp_path, "m-001", MUTANT.format(oid=head_oid)))
    staged = materialize(case, git_repo, tmp_path / "work")
    capsys.readouterr()
    remove_worktree(git_repo, staged.worktree)
    assert capsys.readouterr().err == ""


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
