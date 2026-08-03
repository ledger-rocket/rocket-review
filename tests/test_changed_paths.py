"""What every review source says it touched.

`ReviewJob.changed_paths` is carried for all sources and read by none of them yet, so these
pin the data itself: which paths each source yields, in which form, and that gathering them
neither invents an entry nor makes a review do anything else differently.
"""

import io
import subprocess
import sys
import types

import pytest

from rocket_review.backends.base import ReviewJob
from rocket_review.cli import main
from rocket_review.prompts import build_agent_prompt


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t.io", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )


def new_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    return repo


def run_with_backend(monkeypatch, argv, backend="codex", stdin_text=None):
    """Drive main() with a fake backend that records the ReviewJob it was handed."""
    captured = {}

    def review(job):
        captured["job"] = job
        return "OK REVIEW"

    monkeypatch.setattr(
        "rocket_review.cli.BACKENDS", {backend: types.SimpleNamespace(review=review)}
    )
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    if stdin_text is None:
        monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    else:
        monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: True)
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.setattr("sys.argv", ["rr", *argv, "--backend", backend])
    try:
        main()
    except SystemExit as e:
        assert e.code in (None, 0), f"rr exited {e.code}"
    return captured["job"]


# codex takes the agentic branch (the CLI only preflights and hands over a git command);
# opencode takes the materialized branch (the CLI captures the diff itself). Both are
# review sources of the same kind and must report the same paths.
BOTH_DIFF_BRANCHES = pytest.mark.parametrize("backend", ["codex", "opencode"])


@BOTH_DIFF_BRANCHES
def test_diff_source_lists_every_changed_path(tmp_path, monkeypatch, backend):
    repo = new_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src/keep.py").write_text("one\n")
    (repo / "src/gone.py").write_text("two\n")
    (repo / "src/untouched.py").write_text("three\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    (repo / "src/keep.py").write_text("edited\n")
    (repo / "src/gone.py").unlink()
    (repo / "src/new.py").write_text("new\n")
    git(repo, "add", "src/new.py")
    monkeypatch.chdir(repo)

    job = run_with_backend(monkeypatch, ["--diff"], backend=backend)

    # A deletion and a staged addition are both part of `git diff HEAD`; a file the working
    # tree never touched is not.
    assert job.changed_paths == ["src/gone.py", "src/keep.py", "src/new.py"]


def test_staged_source_lists_only_the_staged_paths(tmp_path, monkeypatch):
    repo = new_repo(tmp_path)
    (repo / "a.py").write_text("a\n")
    (repo / "b.py").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    (repo / "a.py").write_text("unstaged edit\n")
    (repo / "b.py").write_text("staged edit\n")
    git(repo, "add", "b.py")
    monkeypatch.chdir(repo)

    job = run_with_backend(monkeypatch, ["--staged"])

    # The paths follow the same ref arguments the review itself uses, so an unstaged edit
    # that --staged does not review is not one of them.
    assert job.changed_paths == ["b.py"]


@BOTH_DIFF_BRANCHES
def test_commit_source_lists_both_sides_of_a_rename_and_a_deletion(
    tmp_path, monkeypatch, backend
):
    repo = new_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src/a.py").write_text("a\n")
    (repo / "src/b.py").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "mv", "src/a.py", "src/renamed.py")
    git(repo, "rm", "-q", "src/b.py")
    (repo / "src/c.py").write_text("c\n")
    git(repo, "add", "src/c.py")
    git(repo, "commit", "-qm", "second")
    monkeypatch.chdir(repo)

    job = run_with_backend(monkeypatch, ["--commit", "HEAD"], backend=backend)

    assert job.changed_paths == ["src/a.py", "src/b.py", "src/c.py", "src/renamed.py"]


def test_root_commit_source_lists_its_files(tmp_path, monkeypatch):
    repo = new_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "dir with spaces").mkdir()
    (repo / "src/a.py").write_text("a\n")
    (repo / "src/b.py").write_text("b\n")
    (repo / "dir with spaces/my file.py").write_text("spaced\n")
    (repo / "café.py").write_text("non-ascii\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    monkeypatch.chdir(repo)

    job = run_with_backend(monkeypatch, ["--commit", "HEAD"])

    paths = job.changed_paths
    # A root commit has no parent to diff against; without asking for it explicitly the
    # commit reads as touching nothing.
    assert len(paths) == 4
    assert "src/a.py" in paths and "src/b.py" in paths
    assert "dir with spaces/my file.py" in paths
    # The non-ascii name arrives as a path, not as git's C-quoted rendering of one. Its
    # exact spelling is the filesystem's business (macOS and Linux normalize differently),
    # so this asserts the form rather than the bytes.
    other = [p for p in paths
             if p not in {"src/a.py", "src/b.py", "dir with spaces/my file.py"}]
    assert len(other) == 1
    assert not other[0].startswith('"') and "\\" not in other[0]
    assert other[0].endswith(".py")


def test_a_merge_commit_lists_what_its_combined_diff_shows(tmp_path, monkeypatch):
    repo = new_repo(tmp_path)
    (repo / "shared.py").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "checkout", "-qb", "side")
    (repo / "side_only.py").write_text("s\n")
    (repo / "shared.py").write_text("a\nside\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "side")
    git(repo, "checkout", "-q", "-")
    (repo / "main_only.py").write_text("m\n")
    (repo / "shared.py").write_text("a\nmain\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "main")
    subprocess.run(["git", "merge", "--no-ff", "side", "-m", "merge"],
                   cwd=repo, capture_output=True)  # conflicts, by construction
    (repo / "shared.py").write_text("a\nboth\n")
    git(repo, "add", "shared.py")
    git(repo, "commit", "-qm", "merge")
    monkeypatch.chdir(repo)

    job = run_with_backend(monkeypatch, ["--commit", "HEAD"])

    # `git show` puts a merge's combined diff in front of the reviewer — what the merge
    # settled differently from every parent. The paths are that same diff's, so the list
    # and the review describe one thing. A merge that resolved nothing shows nothing and
    # reports nothing, which is the honest pair.
    assert job.changed_paths == ["shared.py"]


def test_a_merge_that_resolved_nothing_reports_nothing(tmp_path, monkeypatch):
    repo = new_repo(tmp_path)
    (repo / "base.py").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "checkout", "-qb", "side")
    (repo / "side_only.py").write_text("s\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "side")
    git(repo, "checkout", "-q", "-")
    (repo / "main_only.py").write_text("m\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "main")
    git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge")
    monkeypatch.chdir(repo)

    job = run_with_backend(monkeypatch, ["--commit", "HEAD"])

    # `git show` prints this merge's message and no diff — it settled nothing its parents
    # had not already settled. Reporting the files either branch touched would name paths
    # the review is never shown. The empty list and the empty diff are the same statement.
    assert job.changed_paths == []


def test_working_tree_paths_are_read_after_the_diff_they_describe(tmp_path, monkeypatch):
    from rocket_review import cli

    repo = new_repo(tmp_path)
    (repo / "a.py").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    (repo / "a.py").write_text("edited\n")
    monkeypatch.chdir(repo)

    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(cmd, *args, **kwargs):
        if isinstance(cmd, list):
            calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", recording_run)

    job = run_with_backend(monkeypatch, ["--diff"], backend="opencode")

    assert job.changed_paths == ["a.py"]
    # A working tree moves under a review that is only ever a snapshot of it. Nothing here
    # can freeze it, so the two reads are put next to each other and the paths are taken
    # after the diff they claim to describe — the narrowest window available.
    snapshot = next(i for i, c in enumerate(calls) if "diff" in c and "--name-only" not in c)
    names = next(i for i, c in enumerate(calls) if "--name-only" in c)
    assert names > snapshot


STDIN_PATCH = """\
diff --git a/src/added.py b/src/added.py
new file mode 100644
--- /dev/null
+++ b/src/added.py
@@ -0,0 +1 @@
+added
diff --git a/src/modified.py b/src/modified.py
--- a/src/modified.py
+++ b/src/modified.py
@@ -1 +1 @@
-old
+new
diff --git a/src/deleted.py b/src/deleted.py
deleted file mode 100644
--- a/src/deleted.py
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""


def test_stdin_patch_lists_each_touched_file(monkeypatch):
    job = run_with_backend(monkeypatch, [], stdin_text=STDIN_PATCH)

    # A patch is read for the paths it carries, in the order it carries them — a deleted
    # file among them, since deleting a file is touching it. /dev/null is the post-image of
    # a deletion, not a path in the change, and must not become one in any spelling.
    assert job.changed_paths == ["src/added.py", "src/modified.py", "src/deleted.py"]
    assert not any("dev/null" in p for p in job.changed_paths)


QUOTED_PATCH = (
    "diff --git a/dir with spaces/my file.py b/dir with spaces/my file.py\n"
    "--- a/dir with spaces/my file.py\t\n"
    "+++ b/dir with spaces/my file.py\t\n"
    "@@ -1 +1 @@\n-x\n+y\n"
    'diff --git "a/we\\"ird name.py" "b/we\\"ird name.py"\n'
    '--- "a/we\\"ird name.py"\t\n'
    '+++ "b/we\\"ird name.py"\t\n'
    "@@ -1 +1 @@\n-x\n+y\n"
    'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
    '--- "a/caf\\303\\251.py"\n'
    '+++ "b/caf\\303\\251.py"\n'
    "@@ -1 +1 @@\n-x\n+y\n"
)


def test_stdin_patch_keeps_spaced_and_quoted_paths_whole(monkeypatch):
    job = run_with_backend(monkeypatch, [], stdin_text=QUOTED_PATCH)

    # git writes a space-bearing path plain (with a trailing tab so the header stays
    # parseable) and C-quotes one carrying a quote or a non-ascii byte. Each is one path,
    # and it is the path git meant.
    assert job.changed_paths == [
        "dir with spaces/my file.py",
        'we"ird name.py',
        "café.py",
    ]


MALFORMED_QUOTED_PATCH = (
    'diff --git "a/\\777bad.py" "b/\\777bad.py"\n'
    '--- "a/\\777bad.py"\n'
    '+++ "b/\\777bad.py"\n'
    "@@ -1 +1 @@\n-x\n+y\n"
    "diff --git a/src/fine.py b/src/fine.py\n"
    "--- a/src/fine.py\n"
    "+++ b/src/fine.py\n"
    "@@ -1 +1 @@\n-x\n+y\n"
)


def test_an_unreadable_quoted_path_does_not_break_the_review(monkeypatch):
    job = run_with_backend(monkeypatch, [], stdin_text=MALFORMED_QUOTED_PATCH)

    # A patch is untrusted text — a fork's PR, a pipe from anywhere. An escape naming no
    # byte is not a path, and it is not a crash either: the rest of the patch still
    # reports, and every entry kept is a real, non-empty string.
    assert "src/fine.py" in job.changed_paths
    assert all(isinstance(p, str) and p for p in job.changed_paths)
    assert len(job.changed_paths) <= 2


HUNK_BODY_PATCH = """\
diff --git a/src/real.py b/src/real.py
--- a/src/real.py
+++ b/src/real.py
@@ -1,2 +1,3 @@
 context
+++ b/not-a-file.py
 more context
"""


def test_a_hunk_body_line_is_not_a_changed_path(monkeypatch):
    job = run_with_backend(monkeypatch, [], stdin_text=HUNK_BODY_PATCH)

    # Adding a line that itself begins "++ " renders as a "+++ " line inside the hunk. Only
    # the header pair names a file.
    assert job.changed_paths == ["src/real.py"]


HEADER_SHAPED_HUNK_PATCH = """\
diff --git a/src/real.py b/src/real.py
--- a/src/real.py
+++ b/src/real.py
@@ -1,3 +1,3 @@
 context
--- a/fake-old.py
+++ b/fake-new.py
 trailing context
"""


def test_a_header_shaped_pair_inside_a_hunk_is_not_a_changed_path(monkeypatch):
    job = run_with_backend(monkeypatch, [], stdin_text=HEADER_SHAPED_HUNK_PATCH)

    # The adversarial shape: a removed line beginning "-- " and an added line beginning
    # "++ " render as a complete "--- "/"+++ " header pair *inside* the hunk. Position in
    # the line is not what makes a header, so nothing but the hunk's own arithmetic can
    # tell these apart — and only what really changed may be reported.
    assert job.changed_paths == ["src/real.py"]


BINARY_PATCH = """\
diff --git a/src/a.py b/src/a.py
index 7898192..6178079 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-a
+b
diff --git a/src/blob.bin b/src/blob.bin
index 6772730..81442ca 100644
Binary files a/src/blob.bin and b/src/blob.bin differ
"""


def test_a_binary_change_is_a_changed_path(monkeypatch):
    job = run_with_backend(monkeypatch, [], stdin_text=BINARY_PATCH)

    # A binary file's change carries no hunk and no "+++" line at all — it is announced in
    # one prose sentence. It is still a file this review touches.
    assert job.changed_paths == ["src/a.py", "src/blob.bin"]


BACKSLASH_NAME_PATCH = (
    'diff --git "a/weird\\\\" "b/weird\\\\"\n'
    '--- "a/weird\\\\"\n'
    '+++ "b/weird\\\\"\n'
    "@@ -1 +1 @@\n-x\n+y\n"
)


def test_a_backslash_terminated_name_is_still_a_changed_path(monkeypatch):
    job = run_with_backend(monkeypatch, [], stdin_text=BACKSLASH_NAME_PATCH)

    # The closing quote of a C-quoted name is preceded by the escaped backslash that ends
    # the name itself. Finding the wrong terminator loses the file entirely.
    assert job.changed_paths == ["weird\\"]


TAB_IN_NAME_PATCH = (
    'diff --git "a/we\\tird.py" "b/we\\tird.py"\n'
    '--- "a/we\\tird.py"\n'
    '+++ "b/we\\tird.py"\n'
    "@@ -1 +1 @@\n-a\n+b\n"
)


def test_a_tab_in_a_filename_does_not_truncate_the_path(monkeypatch):
    job = run_with_backend(monkeypatch, [], stdin_text=TAB_IN_NAME_PATCH)

    # A tab is a legal character in a filename and the only separator the report has, so a
    # path carrying one arrives in a field that still contains tabs. Cutting at the wrong
    # one invents a path that was never in the patch.
    assert job.changed_paths == ["we\tird.py"]


NO_PREFIX_PATCH = """\
diff --git src/deep/a.py src/deep/a.py
--- src/deep/a.py
+++ src/deep/a.py
@@ -1 +1 @@
-x
+y
"""


def test_a_no_prefix_patch_keeps_its_whole_path(monkeypatch):
    job = run_with_backend(monkeypatch, [], stdin_text=NO_PREFIX_PATCH)

    # `git diff --no-prefix` writes no a//b/, so stripping a leading component off these
    # headers would silently rename the file to one a directory up.
    assert job.changed_paths == ["src/deep/a.py"]


DUPLICATE_PATCH = """\
diff --git a/src/same.py b/src/same.py
--- a/src/same.py
+++ b/src/same.py
@@ -1 +1 @@
-one
+two
diff --git a/src/same.py b/src/same.py
--- a/src/same.py
+++ b/src/same.py
@@ -9 +9 @@
-nine
+ten
"""


def test_a_path_appearing_twice_is_listed_once(monkeypatch):
    job = run_with_backend(monkeypatch, [], stdin_text=DUPLICATE_PATCH)

    assert job.changed_paths == ["src/same.py"]


def test_pr_source_lists_the_patch_paths_and_not_the_description(monkeypatch):
    body_patch = (
        "Fixes the thing. For reference the original patch was:\n"
        "--- a/docs/from-the-body.md\n"
        "+++ b/docs/from-the-body.md\n"
    )
    monkeypatch.setattr(
        "rocket_review.cli.get_pr_content",
        lambda pr_ref, repo=None: (f"PR #7: title\n\n{body_patch}", STDIN_PATCH),
    )

    job = run_with_backend(monkeypatch, ["--pr", "7"])

    # The patch is what is under review; prose quoting a patch is not part of the change.
    assert job.changed_paths == ["src/added.py", "src/modified.py", "src/deleted.py"]


def test_file_arguments_are_carried_as_given(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("a\n")
    (tmp_path / "plan.md").write_text("# plan\n")
    monkeypatch.chdir(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess call: {args}")

    # File arguments are the user's own words about files that are simply there. Nothing
    # about them is worth asking a process, and rr reviews files outside a checkout.
    monkeypatch.setattr("rocket_review.cli.run_capture", forbidden)
    monkeypatch.setattr("rocket_review.cli.subprocess.run", forbidden)

    job = run_with_backend(monkeypatch, ["src/a.py", "plan.md"])

    assert job.changed_paths == ["src/a.py", "plan.md"]


def test_prose_on_stdin_yields_no_paths(monkeypatch):
    job = run_with_backend(
        monkeypatch, [], stdin_text="# Plan\n\nStep one: decide the thing.\n"
    )

    # Text that is not a patch names no paths. Nothing determinable is an empty list,
    # never an error and never a guess.
    assert job.changed_paths == []


def test_a_review_survives_a_discovery_that_cannot_run(monkeypatch, capsys):
    monkeypatch.setattr(
        "rocket_review.cli.get_pr_content",
        lambda pr_ref, repo=None: ("PR #7: title", STDIN_PATCH),
    )
    monkeypatch.setenv("PATH", "")  # nothing to exec, so discovery cannot answer

    job = run_with_backend(monkeypatch, ["--pr", "7"])

    # Which paths a review touches is decoration on a review that is otherwise fine.
    # Losing it costs the list and one note — never the review, and never the exit code.
    assert job.changed_paths == []
    assert "could not determine" in capsys.readouterr().err


def test_path_discovery_never_exits_when_git_is_missing(tmp_path, monkeypatch):
    from rocket_review import cli

    repo = new_repo(tmp_path)
    (repo / "a.py").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    oid = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                         text=True).stdout.strip()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", "")

    # SystemExit is what run_capture does for a missing binary, and it is not available to
    # discovery: these answer with an empty list or they answer with nothing at all.
    assert cli.git_diff_changed_paths(False) == []
    assert cli.commit_changed_paths(oid) == []


def _job(**kw):
    defaults = dict(mode="diff", content="diff --git a b", docs_content=None, extra=None,
                    commit=None, pr=False, git_cmd=None, model=None)
    defaults.update(kw)
    return ReviewJob(**defaults)


def test_changed_paths_default_to_a_fresh_empty_list():
    first, second = _job(), _job()

    assert first.changed_paths == [] and second.changed_paths == []
    assert first.changed_paths is not second.changed_paths


def test_changed_paths_do_not_reach_the_prompt():
    prompt = build_agent_prompt(_job(changed_paths=["ZZ-UNIQUE-MARKER.py"]))

    # Carried and unused: this task adds the data, and no review may read it yet.
    assert "ZZ-UNIQUE-MARKER" not in prompt
