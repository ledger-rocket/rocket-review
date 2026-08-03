"""The path-trust gate, in a module every route can reach without closing an import cycle.

`config` imports `rocket_review.backends`, and a backend applies this same rule to the
paths it extracts, so neither `cli` nor `config` is importable from a backend — `api ->
config -> backends -> api` would close a cycle. This module imports nothing first-party at
all, so it can be imported from anywhere, and `cli` and `config` take the rule from here
rather than carrying a second copy: one rule, one implementation.
"""

import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# Bounds git/gh preflight calls so a hung credential helper or network fetch
# can't stall a CI gate indefinitely; backend runs have their own longer timeout.
SUBPROCESS_TIMEOUT = 300


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


@lru_cache(maxsize=None)
def case_folds(root: Path) -> bool:
    """Whether this filesystem folds case, asked of the checkout itself.

    Every git root holds a .git, so a .GIT that also exists is the filesystem answering.
    It matters because resolve() does not canonicalise case: on macOS a link to `readme.md`
    opens the tracked `README.md` and has to count as tracked, while on Linux those are two
    different files and only one of them is the repository's.
    """
    return (root / ".GIT").exists()


def tracked_key(relative: str, folds: bool) -> str:
    return relative.lower() if folds else relative


@lru_cache(maxsize=None)
def tracked_files(root: Path) -> frozenset[str]:
    """Every path the repository at `root` carries at HEAD, repo-relative.

    `ls-tree HEAD`, not `ls-files`: the index would let a stray `git add .env` widen what a
    repository may offer, while HEAD is what the repository actually carries. A repo with no
    commits has no HEAD, git fails, and nothing is tracked — which refuses, as it should.

    Listing the whole tree once, rather than asking about each path, is also what keeps a doc
    with thousands of links from ever building a command line: no path is passed to git at
    all. Strings rather than Paths because a monorepo's HEAD tree is large and this set only
    ever answers "is this one in it".

    Cached for the process, which is one rr run: HEAD cannot move under a run that never
    writes, and a checkout replaced at the same path mid-run would need a second rr in the
    same interpreter. An embedder calling main() twice around such a change would see the
    first answer — cache_clear() is the release valve, and the test suite pulls it.
    """
    result = run_capture(["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", "HEAD"])
    if result.returncode != 0:
        return frozenset()
    folds = case_folds(root)
    return frozenset(tracked_key(entry, folds) for entry in result.stdout.split("\0") if entry)


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


def resolve_doc_path(path: Path, *, user_named: bool, base: Path) -> Path | None:
    """The one gate every docs path passes: the resolved path, or None if rr must not read it.

    Every route — a path a config named, an auto-discovered doc, a markdown link out of any
    doc, a path typed on the command line — comes through here, and the answer is the
    *resolved* path, which is what callers must carry onwards: a doc read through a symlink
    has its links judged from where the file really is, not from where the link sat.

    `user_named` is the user's own word: a --docs/--llms path, or one written in their own
    user config. It reads whatever it points at, with .git kept as a footgun check.

    Everything else was chosen by a repository — a project config is repository content, an
    auto-discovered doc is whichever file the repo put in the pattern's way, and a link out
    of a doc is written by whoever wrote that doc. Those must be tracked by the repository,
    inside `base` (the directory they came from), and outside .git. Outside a checkout there
    is nothing to track, so confinement to `base` is what remains.
    """
    # Both spellings, because either can be the one that reaches metadata: `.git/config`
    # resolves away when .git is a symlink to an external gitdir, and a symlink elsewhere in
    # the path resolves *into* .git without ever naming it.
    if inside_dot_git(path if path.is_absolute() else base / path):
        return None
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return None
    if inside_dot_git(resolved) or is_repository_metadata(resolved, base):
        return None
    if user_named:
        return resolved
    base_dir = base.resolve()
    if not resolved.is_relative_to(base_dir):
        return None
    root = find_git_root(base_dir)
    if root is None:
        return resolved
    inside = root.resolve()
    if not resolved.is_relative_to(inside):
        return None
    relative = resolved.relative_to(inside).as_posix()
    return resolved if tracked_key(relative, case_folds(root)) in tracked_files(root) else None
