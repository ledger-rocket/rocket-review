import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# Grace period between SIGTERM and SIGKILL when tearing a backend down on Ctrl-C.
_TERM_GRACE_SECONDS = 2.0

TIMEOUT = 900

# Backends run inside a ThreadPoolExecutor, so a Ctrl-C (SIGINT) is delivered to the main
# thread — not the workers blocked in communicate(). The main thread calls
# terminate_active_commands() to tear down the children the workers can't reach themselves.
_active_procs: set[subprocess.Popen] = set()
_active_lock = threading.Lock()

# Refuses launches that have not happened yet. Tearing the running backends down is not
# enough on its own: a worker that has been accepted but has not yet reached Popen() is
# invisible to the teardown snapshot, so without this gate it would start a fresh backend
# afterwards and the executor's non-daemon threads would hold the CLI open — still
# billing — for that backend's full timeout.
_interrupted = threading.Event()


def begin_fanout() -> None:
    """Arm a new fan-out. Must run before any backend is submitted.

    The gate latches until cleared, so a run that ends in an interrupt would otherwise
    make every later run in the same process fail before launch. main() runs once per
    process, but tests and in-process embeddings call it repeatedly.
    """
    _interrupted.clear()


def request_interrupt() -> None:
    """Refuse any backend launch from here on. Set this before tearing down."""
    _interrupted.set()


def interrupted() -> bool:
    return _interrupted.is_set()


def _signal_process_group(proc: subprocess.Popen, sig: int) -> None:
    # start_new_session makes the child its own group leader, so its pid IS the group id.
    # Signal that directly rather than re-deriving via getpgid(pid): once the leader exits
    # but a descendant still holds a pipe open, getpgid would raise and the SIGKILL would
    # never reach the descendant — killpg(pid) still delivers while any group member lives.
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # whole group already gone, or not ours to signal


def _group_alive(proc: subprocess.Popen) -> bool:
    try:
        os.killpg(proc.pid, 0)  # signal 0: existence check, delivers nothing
    except (ProcessLookupError, PermissionError):
        return False
    return True


def terminate_active_commands() -> None:
    """Tear down the process group of every backend subprocess currently running.

    Killing the groups here unblocks the workers' communicate() calls so the executor can
    shut down promptly instead of waiting out each backend's timeout after an interrupt.
    SIGTERM first, then SIGKILL anything still alive after a short grace, so a backend (or a
    descendant) that traps SIGTERM can't keep a worker — and thus the whole exit — blocked.
    """
    with _active_lock:
        procs = list(_active_procs)
    for proc in procs:
        _signal_process_group(proc, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while procs and time.monotonic() < deadline:
        procs = [p for p in procs if _group_alive(p)]
        if procs:
            time.sleep(0.05)
    for proc in procs:
        _signal_process_group(proc, signal.SIGKILL)


class BackendError(Exception):
    """A backend failed to produce a review."""


@dataclass
class ReviewJob:
    mode: str
    content: str | None
    docs_content: str | None
    extra: str | None
    commit: str | None
    pr: bool
    git_cmd: str | None
    model: str | None
    json_output: bool = False
    # Free-form reasoning-effort string passed through to the backend; each backend
    # maps it to its own flag and lets invalid values fail loudly downstream.
    effort: str | None = None
    # Per-backend subprocess timeout in seconds; None means the backend falls back
    # to base.TIMEOUT. High-effort reasoning models can outrun the 900s default.
    timeout: int | None = None
    #: True when the text under review comes from a repository other than this checkout
    #: (`--repo owner/name --pr N`). It decides whether local files may be attached at all:
    #: the trust question is "does the repository that wrote this text carry the file", and
    #: for a foreign repository there is no way to ask it from here.
    foreign_repo: bool = False
    #: What this review touches, in the order git or the patch reports it, deduplicated,
    #: no empty strings. Carried to every backend and read by none yet: it is what a prompt
    #: would need to select content from the languages a review actually touches.
    #:
    #: **These are strings, not paths, and most of them are not this checkout's.** What is
    #: guaranteed depends entirely on where they came from:
    #:
    #: - `--diff`/`--staged` and `--commit` ask git about *this* repository, so those are
    #:   repo-relative and name files that exist here. The two diff settings that would
    #:   change that (diff.relative, diff.renames) are pinned on the command line.
    #: - a patch — `--pr`, or anything piped in — is untrusted text, and its headers say
    #:   whatever their author wrote. `/etc/shadow` and `../../../etc/passwd` are both
    #:   things a patch can put here, verbatim. Nothing resolves, normalises or confines
    #:   them, and for `--pr --repo other/project` they describe a repository that is not
    #:   on this machine at all.
    #: - file arguments are the paths the user typed, which may be absolute or relative to
    #:   wherever they were standing.
    #:
    #: So: anything that later OPENS one of these must put it through
    #: `repo.resolve_doc_path` first, exactly as the docs routes and the api backend's
    #: extraction already do. A path that arrived in a review is not a path rr may read.
    #:
    #: It is also incomplete in ways worth knowing before trusting it as an inventory. It
    #: is a snapshot of a moving tree — for --diff the paths are read next to the diff they
    #: describe, but nothing freezes the working tree between the two reads. A merge commit
    #: reports its combined diff, so a merge that resolved nothing reports nothing, exactly
    #: as `git show` displays nothing for it; a combined diff piped in reports only the
    #: files its own headers name, and not those whose names git had to quote. A patch
    #: source names a rename only by its destination, where git names both sides. And a
    #: patch is read whole or not at all: one header git will not parse costs every path in
    #: it, not just its own.
    #:
    #: default_factory gives each ReviewJob its own list; the fan-out's per-backend jobs
    #: come from dataclasses.replace, which copies the reference, so every backend in one
    #: run shares this list. Nothing may mutate it.
    changed_paths: list[str] = field(default_factory=list)


def format_duration(seconds: int) -> str:
    """Render a second count as whole minutes when it divides evenly, else seconds."""
    if seconds != 0 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} second" if seconds == 1 else f"{seconds} seconds"


def run_command(cmd: list[str], *, stdin: str | None = None, timeout: int = TIMEOUT) -> str:
    # start_new_session puts the child in its own process group so a fan-out backend
    # that itself spawns children (a CLI shelling out to a model runner) can be torn
    # down as a whole. Without it, a Ctrl-C leaves the group orphaned and running until
    # the timeout while the ThreadPoolExecutor blocks on communicate().
    # Check the gate, launch, and register as one step under the lock that
    # terminate_active_commands() snapshots under. What the pairing buys is exclusion
    # against the snapshot, not against the gate: request_interrupt() takes no lock, so an
    # interrupt can still be raised mid-section — the teardown that follows it just blocks
    # here until registration lands, and so it still sees this process. Every launch is
    # therefore either refused at the check or present in the snapshot. Split across two
    # acquisitions, one could slip between the two and outlive the interrupt.
    with _active_lock:
        if _interrupted.is_set():
            raise BackendError("interrupted before launch")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # errors="replace": backend output may quote non-UTF8 bytes from reviewed
                # files; a stray byte must not turn a finished review into a decode crash.
                text=True, encoding="utf-8", errors="replace",
                start_new_session=True,
            )
        except FileNotFoundError:
            raise BackendError(f"{cmd[0]} not found on PATH")
        _active_procs.add(proc)

    def _terminate() -> None:
        _signal_process_group(proc, signal.SIGTERM)
        try:
            proc.communicate(timeout=5)  # reap; drains pipes so nothing deadlocks
        except subprocess.TimeoutExpired:
            _signal_process_group(proc, signal.SIGKILL)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # group SIGKILLed; don't block the gate waiting on a wedged reap

    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate()
        raise BackendError(f"{cmd[0]} timed out after {format_duration(timeout)}")
    except BaseException:
        # Any other exit — a KeyboardInterrupt that lands here, an OSError from
        # communicate(), anything — must not strand the child running until the timeout.
        _terminate()
        raise
    finally:
        with _active_lock:
            _active_procs.discard(proc)
    if proc.returncode != 0:
        raise BackendError(
            f"{cmd[0]} failed (exit {proc.returncode}): {err.strip()}"
        )
    return out


def write_prompt_file(prompt: str) -> Path:
    # File indirection instead of argv keeps large prompts under ARG_MAX.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, prefix="rr-prompt-", encoding="utf-8",
    ) as f:
        f.write(prompt)
        return Path(f.name)
