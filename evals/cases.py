"""Eval case manifests: what to review, at which snapshot, and what defect is hidden in it.

One YAML file per case under `evals/cases/`. A case is loaded, validated strictly, then
*materialized* — turned into a working directory plus the `rr` arguments that make `rr`
review it the way a developer would have. See `materialize` for why each source type is
handled differently.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

CASES_DIR = Path(__file__).resolve().parent / "cases"

MODES = ("diff", "code", "plan")
SOURCES = ("mutant", "merged-pr", "seeded-plan")

TOP_LEVEL_KEYS = {"id", "mode", "source", "repo_commit", "diff", "path", "defect"}
DEFECT_KEYS = {"class", "file", "span", "expected"}


class CaseError(Exception):
    """A manifest does not describe a runnable case."""


@dataclass(frozen=True)
class Defect:
    #: `class` in the manifest; the taxonomy label recall is aggregated by.
    defect_class: str
    file: str
    #: Inclusive line range in the *materialized* case that a correct finding must overlap.
    span: tuple[int, int]
    expected: str


@dataclass(frozen=True)
class Case:
    id: str
    mode: str
    source: str
    repo_commit: str
    #: Patch applied on top of `repo_commit` for `mutant` cases, relative to `root`.
    diff: str | None
    #: File handed to rr for `code`/`plan` cases, relative to `root`.
    path: str | None
    #: Absent on clean controls — that absence is what makes a case a control.
    defect: Defect | None
    manifest_path: Path

    @property
    def is_control(self) -> bool:
        return self.defect is None

    @property
    def root(self) -> Path:
        """What `diff` and `path` are relative to: the parent of the manifest directory.

        For the shipped corpus that is `evals/`, so a manifest reads `cases/b-001.patch`
        and its artefacts sit beside it. Deriving it from the manifest's own location
        rather than a module constant is what lets a corpus live outside this repo.
        """
        return self.manifest_path.parent.parent


@dataclass(frozen=True)
class Materialized:
    """Where to run rr for a case, and with which source arguments."""

    cwd: Path
    rr_args: list[str]
    #: Set when a throwaway git worktree was created and must be torn down afterwards.
    worktree: Path | None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CaseError(message)


def _parse_defect(raw: object, where: str) -> Defect:
    _require(isinstance(raw, dict), f"{where}: defect must be a mapping")
    assert isinstance(raw, dict)
    unknown = set(raw) - DEFECT_KEYS
    _require(not unknown, f"{where}: unknown defect keys: {', '.join(sorted(unknown))}")
    for key in ("class", "file", "expected"):
        _require(
            isinstance(raw.get(key), str) and raw[key].strip() != "",
            f"{where}: defect.{key} must be a non-empty string",
        )
    span = raw.get("span")
    _require(
        isinstance(span, list) and len(span) == 2
        and all(isinstance(n, int) for n in span),
        f"{where}: defect.span must be a two-element [start, end] line range",
    )
    assert isinstance(span, list)
    start, end = span
    _require(
        1 <= start <= end,
        f"{where}: defect.span must satisfy 1 <= start <= end, got {span}",
    )
    return Defect(
        defect_class=raw["class"], file=raw["file"],
        span=(start, end), expected=raw["expected"],
    )


def load_case(path: Path) -> Case:
    """Parse and validate one manifest. Every rejection names the file and the field."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise CaseError(f"{path}: could not read manifest: {e}") from e
    _require(isinstance(raw, dict), f"{path}: manifest must be a YAML mapping")
    assert isinstance(raw, dict)

    unknown = set(raw) - TOP_LEVEL_KEYS
    _require(not unknown, f"{path}: unknown keys: {', '.join(sorted(unknown))}")
    for key in ("id", "mode", "source", "repo_commit"):
        _require(
            isinstance(raw.get(key), str) and raw[key].strip() != "",
            f"{path}: {key} is required and must be a non-empty string",
        )
    _require(raw["mode"] in MODES, f"{path}: mode must be one of {', '.join(MODES)}")
    _require(raw["source"] in SOURCES, f"{path}: source must be one of {', '.join(SOURCES)}")
    _require(
        raw["id"] == path.stem,
        f"{path}: id {raw['id']!r} must match the file name {path.stem!r} — the id keys "
        "every result row, so a mismatch makes rows untraceable to their manifest",
    )

    diff, case_path = raw.get("diff"), raw.get("path")
    if raw["source"] == "mutant":
        _require(
            isinstance(diff, str) and not case_path,
            f"{path}: a mutant case needs `diff` (a patch path) and no `path`",
        )
    elif raw["source"] == "merged-pr":
        _require(
            not diff and not case_path,
            f"{path}: a merged-pr case is reviewed at repo_commit; drop `diff`/`path`",
        )
    else:
        _require(
            isinstance(case_path, str) and not diff,
            f"{path}: a seeded-plan case needs `path` (the artifact to review) and no `diff`",
        )

    defect = _parse_defect(raw["defect"], str(path)) if raw.get("defect") is not None else None
    return Case(
        id=raw["id"], mode=raw["mode"], source=raw["source"],
        repo_commit=raw["repo_commit"], diff=diff, path=case_path,
        defect=defect, manifest_path=path,
    )


def load_cases(directory: Path, only: set[str] | None = None) -> list[Case]:
    manifests = sorted(directory.glob("*.yaml"))
    if not manifests:
        raise CaseError(f"no *.yaml case manifests under {directory}")
    cases = [load_case(m) for m in manifests]
    ids = [c.id for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    _require(not duplicates, f"{directory}: duplicate case ids: {', '.join(duplicates)}")
    if only is not None:
        unknown = only - set(ids)
        _require(not unknown, f"unknown case id(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in only]
    return cases


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


def materialize(case: Case, repo: Path, workdir: Path) -> Materialized:
    """Stage a case so `rr` reviews it the way the developer who hit it would have.

    Mutant cases get a detached worktree at `repo_commit` with the patch applied to the
    working tree, and are reviewed with `--diff`. That is the faithful shape: the agentic
    backends are told to run `git diff HEAD` and can navigate the whole snapshot around the
    change, exactly as on a real uncommitted edit. Feeding the patch on stdin instead would
    hand every backend the same bytes but strip the repository context that `rr`'s primary
    mode depends on, so a prompt change affecting repo navigation would not show up.

    Merged-PR cases need no worktree: a commit is immutable, so `--commit <oid>` in the
    repo itself is already reproducible, and it exercises rr's `git show` path.

    Seeded plans and standalone code files are reviewed from the checkout as ordinary file
    arguments; `repo_commit` is provenance for them rather than a checkout target, since
    the artifact under review is not a repository snapshot.
    """
    if case.source == "mutant":
        assert case.diff is not None
        patch = (case.root / case.diff).resolve()
        if not patch.is_file():
            raise CaseError(f"{case.id}: patch {patch} not found")
        worktree = workdir / f"case-{case.id}"
        added = _git(repo, "worktree", "add", "--detach", str(worktree), case.repo_commit)
        if added.returncode != 0:
            raise CaseError(
                f"{case.id}: could not create a worktree at {case.repo_commit}: "
                f"{added.stderr.strip()}"
            )
        applied = _git(worktree, "apply", "--whitespace=nowarn", str(patch))
        if applied.returncode != 0:
            remove_worktree(repo, worktree)
            raise CaseError(
                f"{case.id}: patch {case.diff} does not apply at {case.repo_commit}: "
                f"{applied.stderr.strip()}"
            )
        return Materialized(cwd=worktree, rr_args=["--diff"], worktree=worktree)

    if case.source == "merged-pr":
        return Materialized(cwd=repo, rr_args=["--commit", case.repo_commit], worktree=None)

    assert case.path is not None
    artifact = (case.root / case.path).resolve()
    if not artifact.is_file():
        raise CaseError(f"{case.id}: artifact {artifact} not found")
    return Materialized(cwd=repo, rr_args=[str(artifact)], worktree=None)


def remove_worktree(repo: Path, worktree: Path) -> None:
    """Tear down a staged worktree, saying so when it could not be torn down.

    Never raises: this runs in a `finally` alongside other cleanups, and one stuck
    worktree must not stop the rest. But it must not be silent either — a failed removal
    leaves admin state behind that only a manual `git worktree prune` clears.
    """
    # --force because the mutant patch leaves the worktree dirty by construction.
    removed = _git(repo, "worktree", "remove", "--force", str(worktree))
    if removed.returncode != 0:
        print(
            f"Warning: could not remove worktree {worktree}: {removed.stderr.strip()}\n"
            f"         once it is gone, run `git -C {repo} worktree prune`.",
            file=sys.stderr,
        )
