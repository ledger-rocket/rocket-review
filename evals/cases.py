"""Eval case manifests: what to review, at which snapshot, and what defect is hidden in it.

One YAML file per case under `evals/cases/`. A case is loaded, validated strictly, then
*materialized* — turned into a working directory plus the `rr` arguments that make `rr`
review it the way a developer would have. See `materialize` for why each source type is
handled differently.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

CASES_DIR = Path(__file__).resolve().parent / "cases"

OID_PATTERN = re.compile(r"[0-9a-f]{40}")

MODES = ("diff", "code", "plan")
SOURCES = ("mutant", "merged-pr", "seeded-plan")

TOP_LEVEL_KEYS = {"id", "mode", "source", "repo_commit", "diff", "path", "defect", "killed_by"}
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
    #: Test node ids that fail with `diff` applied and pass without it — the admission
    #: proof for a defect mutant, written by `verify_cases.py` rather than by hand. An
    #: empty tuple means unproven: a mutant no test distinguishes may be an equivalent
    #: mutant, and scoring recall on one would measure nothing.
    killed_by: tuple[str, ...]
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
    #: The staged directory that must be torn down after the review, or None when the case
    #: is reviewed without isolation (seeded plans and standalone files).
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


def _parse_killed_by(raw: object, where: str) -> tuple[str, ...]:
    _require(isinstance(raw, list), f"{where}: killed_by must be a list of test node ids")
    assert isinstance(raw, list)
    _require(
        all(isinstance(n, str) and n.strip() != "" for n in raw),
        f"{where}: killed_by entries must be non-empty strings",
    )
    _require(len(set(raw)) == len(raw), f"{where}: killed_by contains duplicate node ids")
    return tuple(raw)


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
    # A full lowercase oid, never an abbreviation or a symbolic name. `HEAD` or `main`
    # resolve to something different on every checkout and every day, which would make a
    # result row's "snapshot" unreproducible; an abbreviation can go ambiguous as the repo
    # grows. tier1 also refuses to resolve anything that is not a plain oid, so a manifest
    # written with one would silently score every citation unlocatable.
    _require(
        bool(OID_PATTERN.fullmatch(raw["repo_commit"])),
        f"{path}: repo_commit must be a full 40-character lowercase commit id, "
        f"got {raw['repo_commit']!r}",
    )
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
    _require(
        not (raw["source"] == "mutant" and raw["mode"] == "code" and defect is None),
        f"{path}: a code-mode mutant is reviewed as the file its defect names, so it "
        "cannot omit `defect`",
    )
    killed_by = (
        _parse_killed_by(raw["killed_by"], str(path)) if raw.get("killed_by") is not None
        else ()
    )
    _require(
        not (killed_by and raw["source"] != "mutant"),
        f"{path}: killed_by is a mutant's admission proof; a {raw['source']} case has "
        "no patch for a test to kill",
    )
    return Case(
        id=raw["id"], mode=raw["mode"], source=raw["source"],
        repo_commit=raw["repo_commit"], diff=diff, path=case_path,
        defect=defect, killed_by=killed_by, manifest_path=path,
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


def _clone_at(repo: Path, dst: Path, commit: str, case_id: str) -> None:
    """Create an isolated shallow clone of `repo` whose history ends at `commit`.

    `--depth=2` exactly: depth 1 would seal but drop the parent commit, and a case
    reviewed with `git show <oid>` needs that parent to render the reviewed diff — at
    depth 1 the whole tree would read as additions, describing a different review from
    the one the case performs. The source is a file:// URL so no alternative clone's
    refs or objects (including anything committed after `commit`) leak into the staged
    snapshot's view.
    """
    init = _git(repo, "init", "-q", str(dst))
    if init.returncode != 0:
        raise CaseError(
            f"{case_id}: could not initialise an isolated snapshot for {commit}: "
            f"{init.stderr.strip()}"
        )
    try:
        fetched = _git(
            dst, "fetch", "-q", "--depth=2", repo.resolve().as_uri(), commit,
        )
        if fetched.returncode != 0:
            raise CaseError(
                f"{case_id}: commit {commit} could not be fetched into an isolated "
                f"snapshot: {fetched.stderr.strip()}"
            )
        checked = _git(dst, "checkout", "-q", "--detach", "FETCH_HEAD")
        if checked.returncode != 0:
            raise CaseError(
                f"{case_id}: could not check out {commit} in the isolated snapshot: "
                f"{checked.stderr.strip()}"
            )
    except CaseError:
        shutil.rmtree(dst, ignore_errors=True)
        raise


def materialize(case: Case, repo: Path, workdir: Path) -> Materialized:
    """Stage a case so `rr` reviews it the way the developer who hit it would have.

    Every snapshot-based case — mutant and merged-pr alike — is materialized into its own
    isolated shallow clone of `repo`, detached at `repo_commit`, taken two commits deep.
    The clone, not the live checkout, is what a backend runs in, so nothing committed in
    `repo` after `repo_commit` is readable from it; an agentic backend told to run
    `git diff HEAD` or `git show <oid>` sees only the snapshot the case pins and its
    parent. Two commits deep is what lets `git show <oid>` render the reviewed diff — at
    one commit deep the parent is missing and the whole tree reads as additions.

    A `diff`-mode mutant leaves the patch uncommitted in the staged clone, which is the
    faithful shape: the backends are told to run `git diff HEAD` and can navigate the whole
    snapshot around the change, exactly as on a real uncommitted edit. Feeding the patch on
    stdin instead would hand every backend the same bytes but strip the repository context
    that `rr`'s primary mode depends on, so a prompt change affecting repo navigation would
    not show up.

    A `code`-mode mutant is the same snapshot reviewed as a whole file — `rr <defect.file>`
    — which is what asks whether the defect is still found without a diff pointing at it.
    There the patch is *committed* inside the staged clone first: a backend under codex's
    read-only sandbox may still run `git diff HEAD`, and an uncommitted mutation would hand
    it the very diff this mode exists to withhold. Committing makes that diff empty, so the
    two modes differ in what the reviewer can see and not only in which prompt ran. The
    path is passed repo-relative so findings cite the same path the manifest and `tier1`'s
    resolver use; an absolute path into a throwaway snapshot would resolve against nothing.

    A merged-pr case is the same isolated clone reviewed with `--commit <oid>`, which
    exercises rr's `git show` path against a fixed snapshot rather than against whatever
    `main` has advanced to.

    Seeded plans and standalone code files are reviewed from the checkout as ordinary file
    arguments; `repo_commit` is provenance for them rather than a checkout target, since
    the artifact under review is not a repository snapshot.
    """
    if case.source in ("mutant", "merged-pr"):
        staged = workdir / f"case-{case.id}"
        _clone_at(repo, staged, case.repo_commit, case.id)
        if case.source == "merged-pr":
            return Materialized(
                cwd=staged, rr_args=["--commit", case.repo_commit], worktree=staged,
            )
        assert case.diff is not None
        patch = (case.root / case.diff).resolve()
        if not patch.is_file():
            shutil.rmtree(staged, ignore_errors=True)
            raise CaseError(f"{case.id}: patch {patch} not found")
        applied = _git(staged, "apply", "--whitespace=nowarn", str(patch))
        if applied.returncode != 0:
            shutil.rmtree(staged, ignore_errors=True)
            raise CaseError(
                f"{case.id}: patch {case.diff} does not apply at {case.repo_commit}: "
                f"{applied.stderr.strip()}"
            )
        if case.mode == "code":
            assert case.defect is not None  # load_case rejects a code mutant without one
            # Identity is supplied inline: the staged snapshot is thrown away, and a
            # machine running this may have no git user configured at all.
            committed = _git(
                staged,
                "-c", "user.email=evals@invalid", "-c", "user.name=rocket-review evals",
                "commit", "--quiet", "--all", "--message", f"eval case {case.id}",
            )
            if committed.returncode != 0:
                shutil.rmtree(staged, ignore_errors=True)
                raise CaseError(
                    f"{case.id}: could not commit the patch in the snapshot, so "
                    f"`git diff HEAD` would still expose it: {committed.stderr.strip()}"
                )
            return Materialized(cwd=staged, rr_args=[case.defect.file], worktree=staged)
        return Materialized(cwd=staged, rr_args=["--diff"], worktree=staged)

    assert case.path is not None
    artifact = (case.root / case.path).resolve()
    if not artifact.is_file():
        raise CaseError(f"{case.id}: artifact {artifact} not found")
    return Materialized(cwd=repo, rr_args=[str(artifact)], worktree=None)


def remove_worktree(repo: Path, worktree: Path) -> None:
    """Tear down a staged directory, saying so when it could not be torn down.

    Never raises: this runs in a `finally` alongside other cleanups, and one stuck
    directory must not stop the rest. But it must not be silent either — a failed removal
    leaves a clone behind that would leak one per case per sweep.
    """
    try:
        # A linked worktree carries a `.git` *file* pointing into the parent repo's admin
        # state, so it must be deregistered as well as deleted — an isolated clone (`git
        # init`) carries a whole `.git` directory and only needs the directory gone.
        if (worktree / ".git").is_file():
            removed = _git(repo, "worktree", "remove", "--force", str(worktree))
            if removed.returncode != 0:
                raise RuntimeError(removed.stderr.strip())
        else:
            shutil.rmtree(worktree)
    except Exception as e:
        print(
            f"Warning: could not remove worktree {worktree}: {e}\n"
            f"         once it is gone, run `git -C {repo} worktree prune`.",
            file=sys.stderr,
        )


def verify_repo_commits(cases: list[Case], repo: Path) -> None:
    """Fail before any spend if a manifest names a commit this repository does not have.

    A missing commit surfaces late and expensively otherwise: a merged-pr case only finds
    out when `rr --commit` fails inside a billed run, and a corpus written against a fork
    or a rewritten history fails one case at a time instead of all at once.
    """
    missing: list[str] = []
    for case in cases:
        resolved = _git(
            repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
            f"{case.repo_commit}^{{commit}}",
        )
        if resolved.returncode != 0 or resolved.stdout.strip() != case.repo_commit:
            missing.append(f"{case.id} ({case.repo_commit})")
    _require(
        not missing,
        f"repo_commit not found in {repo}: {', '.join(missing)}. Fetch the history these "
        "cases were written against, or point --repo at the right checkout.",
    )
