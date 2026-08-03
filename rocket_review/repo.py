"""The path-trust gate, in a module every route can reach without closing an import cycle.

`config` imports `rocket_review.backends`, and a backend applies this same rule to the
paths it extracts, so neither `cli` nor `config` is importable from a backend — `api ->
config -> backends -> api` would close a cycle. This module imports nothing first-party at
all, so it can be imported from anywhere, and `cli` and `config` take the rule from here
rather than carrying a second copy: one rule, one implementation.
"""

import subprocess
import sys
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path

# Bounds git/gh preflight calls so a hung credential helper or network fetch
# can't stall a CI gate indefinitely; backend runs have their own longer timeout.
SUBPROCESS_TIMEOUT = 300

#: Bounds the gate's own git calls, which is a different budget from the preflight's: the
#: gate runs inside a backend worker with the user's --timeout already ticking, so it must
#: not be able to spend five minutes of a five-second run. A read that has not answered in
#: this long is treated as "nothing is tracked", which refuses.
GATE_TIMEOUT = 10

#: How many paths go into one `ls-tree` command line. The count comes from a doc's links or
#: a diff's prose, so nothing the caller controls bounds it; ARG_MAX does.
QUERY_CHUNK = 200


def run_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a git/gh command with a timeout and total decoding.

    Diffs legitimately contain non-UTF8 bytes (files in other encodings), so
    decode with errors="replace" rather than letting a stray byte raise.
    """
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"Error: {' '.join(cmd)} timed out after {SUBPROCESS_TIMEOUT}s", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: {cmd[0]} not found on PATH", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        # E2BIG above all: a command line built from user data can outgrow ARG_MAX, and an
        # OSError here would otherwise surface as a traceback rather than a refusal.
        print(f"Error: could not run {cmd[0]}: {e}", file=sys.stderr)
        sys.exit(1)


def capture(cmd: list[str], timeout: int = GATE_TIMEOUT) -> subprocess.CompletedProcess | None:
    """Run a git command, or None if it could not be run or did not answer in time.

    run_capture's non-exiting sibling, and the only one the gate may use: the gate is
    reachable from a backend worker thread, where sys.exit raises SystemExit — a
    BaseException the fan-out's `except Exception` handlers do not catch. One slow ls-tree
    would surface out of `future.result()` and take down every backend's result, not just
    the one that asked. Returning None instead leaves the caller to fail closed.
    """
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=None)
def case_folds(root: Path) -> bool:
    """Whether this filesystem folds case, asked of the checkout itself.

    Every git root holds a .git, so whether `.GIT` is the *same directory* is the filesystem
    answering. Identity rather than mere existence: a repository is free to carry a path
    literally spelled `.GIT`, and that is a different directory rather than proof of folding.
    It matters because resolve() does not canonicalise case: on macOS a link to `readme.md`
    opens the tracked `README.md` and has to count as tracked, while on Linux those are two
    different files and only one of them is the repository's.
    """
    try:
        return (root / ".GIT").samefile(root / ".git")
    except OSError:
        return False


def tracked_key(relative: str, folds: bool) -> str:
    return relative.lower() if folds else relative


@lru_cache(maxsize=None)
def tracked_files(root: Path) -> frozenset[str]:
    """Every path the repository at `root` carries at HEAD, repo-relative and fold-keyed.

    `ls-tree HEAD`, not `ls-files`: the index would let a stray `git add .env` widen what a
    repository may offer, while HEAD is what the repository actually carries. A repo with no
    commits has no HEAD, git fails, and nothing is tracked — which refuses, as it should.

    The whole tree, which is what `tracked` falls back to when a case-folding filesystem
    puts a path git's byte-for-byte pathspec matching cannot find. Strings rather than Paths
    because a monorepo's HEAD tree is large and this set only ever answers "is this one in
    it".

    Cached for the process, which is one rr run: HEAD cannot move under a run that never
    writes, and a checkout replaced at the same path mid-run would need a second rr in the
    same interpreter. An embedder calling main() twice around such a change would see the
    first answer — clear_caches() is the release valve, and the test suite pulls it.
    """
    result = capture(["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", "HEAD"])
    if result is None or result.returncode != 0:
        return frozenset()
    folds = case_folds(root)
    return frozenset(tracked_key(entry, folds) for entry in result.stdout.split("\0") if entry)


#: root -> repo-relative path -> whether HEAD carries it. Answers, not trees: asking git
#: about the handful of paths in hand beats materialising a monorepo's HEAD to answer one
#: membership question. Cleared by clear_caches() alongside the lru_caches beside it.
_tracked: dict[Path, dict[str, bool]] = {}


def _tracked_exactly(root: Path, relatives: Sequence[str]) -> set[str]:
    """Which of `relatives` git reports at HEAD, matched byte-for-byte.

    `--` so a path that looks like a revision is read as a path, and chunked so a doc with
    thousands of links cannot outgrow ARG_MAX. A chunk git refuses answers for none of its
    paths, which refuses — the same direction every other failure here takes.
    """
    found: set[str] = set()
    for start in range(0, len(relatives), QUERY_CHUNK):
        chunk = relatives[start:start + QUERY_CHUNK]
        result = capture([
            "git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *chunk,
        ])
        if result is None or result.returncode != 0:
            continue
        found.update(entry for entry in result.stdout.split("\0") if entry)
    return found


def tracked(root: Path, relatives: Iterable[str]) -> frozenset[str]:
    """Which of `relatives` the repository at `root` carries at HEAD.

    Asks git about the paths in hand rather than listing the whole tree, because a review
    names a handful of files and a monorepo's HEAD holds hundreds of thousands. Answers are
    remembered per root, so a path asked about twice costs one query.

    A case-folding filesystem is the one thing a scoped query cannot answer: git matches
    pathspecs byte-for-byte, while on macOS `readme.md` opens the tracked `README.md` and
    has to count as tracked. Only a path the scoped query missed falls back to the whole
    tree, which is what every path used to cost.
    """
    wanted = tuple(dict.fromkeys(relatives))
    memo = _tracked.setdefault(root, {})
    unknown = tuple(name for name in wanted if name not in memo)
    if unknown:
        found = _tracked_exactly(root, unknown)
        missed = [name for name in unknown if name not in found]
        if missed and case_folds(root):
            whole = tracked_files(root)
            found.update(name for name in missed if tracked_key(name, True) in whole)
        for name in unknown:
            memo[name] = name in found
    return frozenset(name for name in wanted if memo[name])


def clear_caches() -> None:
    """Forget every answer this module remembers about the filesystem and about HEAD.

    One release valve for all three caches: production reads each checkout once per run, so
    only a test that commits between two reads — or an embedder calling main() twice around
    a changed checkout — needs them dropped, and needing to remember which three is how one
    gets missed.
    """
    tracked_files.cache_clear()
    case_folds.cache_clear()
    _tracked.clear()


def inside_dot_git(path: Path) -> bool:
    """Whether a path enters a .git directory, under the spellings that reach the same file.

    Path.resolve() does not canonicalise case, so an exact ".git" comparison is bypassed by
    ".GIT/config" on the case-insensitive filesystems macOS and Windows default to — where
    the file that then opens is the real one. Win32 additionally strips trailing dots and
    spaces while resolving, so ".git." and ".git " land there too. Repository metadata is
    never a standards doc: it is local machine state a clone does not control, credentialed
    remote URLs above all, and a doc is copied into the prompt verbatim.
    """
    return any(part.lower().rstrip(". ") == ".git" for part in path.parts)


def find_git_root(start: Path) -> Path | None:
    """The checkout `start` is in, or None outside one.

    A worktree's or submodule's .git is a file rather than a directory, hence exists()
    over is_dir().
    """
    start = start.resolve()
    return next((d for d in [start, *start.parents] if (d / ".git").exists()), None)


def is_repository_metadata(resolved: Path, base: Path) -> bool:
    """Whether a resolved path lands in the checkout's own .git, whatever it is called.

    inside_dot_git covers every spelling of the name; this covers the spelling that
    never says it — a symlink whose target sits in an external gitdir, where neither the
    name nor the resolved path carries a `.git` component at all. Asked of the checkout the
    path came from, since that is whose metadata it would be.
    """
    root = find_git_root(base)
    if root is None:
        return False
    try:
        gitdir = (root / ".git").resolve()
    except (OSError, ValueError):
        return False
    return resolved == gitdir or resolved.is_relative_to(gitdir)


def resolve_doc_paths(
    paths: Iterable[Path], *, user_named: bool, base: Path
) -> list[Path | None]:
    """The one gate, asked of several paths at once: one answer per input, in order.

    Every route — a path a config named, an auto-discovered doc, a markdown link out of any
    doc, a path typed on the command line, a path a diff or a PR description happened to
    mention — comes through here, and the answer is the *resolved* path, which is what
    callers must carry onwards: a doc read through a symlink has its links judged from where
    the file really is, not from where the link sat.

    `user_named` is the user's own word: a --docs/--llms path, or one written in their own
    user config. It reads whatever it points at, with .git kept as a footgun check.

    Everything else was chosen by a repository — a project config is repository content, an
    auto-discovered doc is whichever file the repo put in the pattern's way, a link out of a
    doc is written by whoever wrote that doc, and text under review is the repository's word
    too. Those must be tracked by the repository, inside `base` (the directory they came
    from), and outside .git. Outside a checkout there is nothing to track, so confinement to
    `base` is what remains.

    Batched because only the tracked half costs anything: confinement is pure path work, so
    the paths that reach git are already filtered and one query answers all of them.
    """
    answers: list[Path | None] = []
    #: (index into answers, resolved path, repo-relative key) for the paths still in play
    #: once every check but tracking has passed.
    pending: list[tuple[int, Path, str]] = []
    base_dir: Path | None = None
    root: Path | None = None
    for path in paths:
        index = len(answers)
        answers.append(None)
        # Both spellings, because either can be the one that reaches metadata: `.git/config`
        # resolves away when .git is a symlink to an external gitdir, and a symlink elsewhere
        # in the path resolves *into* .git without ever naming it.
        if inside_dot_git(path if path.is_absolute() else base / path):
            continue
        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            continue
        if inside_dot_git(resolved) or is_repository_metadata(resolved, base):
            continue
        if user_named:
            answers[index] = resolved
            continue
        if base_dir is None:
            base_dir = base.resolve()
            root = find_git_root(base_dir)
        if not resolved.is_relative_to(base_dir):
            continue
        if root is None:
            answers[index] = resolved
            continue
        inside = root.resolve()
        if not resolved.is_relative_to(inside):
            continue
        pending.append((index, resolved, resolved.relative_to(inside).as_posix()))
    if pending and root is not None:
        carried = tracked(root, [relative for _, _, relative in pending])
        for index, resolved, relative in pending:
            if relative in carried:
                answers[index] = resolved
    return answers


def resolve_doc_path(path: Path, *, user_named: bool, base: Path) -> Path | None:
    """The gate for a single path: the resolved path, or None if rr must not read it."""
    return resolve_doc_paths([path], user_named=user_named, base=base)[0]
