"""The repository's own gates: .githooks/pre-push, scripts/check and scripts/review-prepush.

Every tool a gate calls (just, uv, uvx, rr) is a stub here that records its arguments, so
the suite pins what each gate runs and in which order, and never runs a real review or a
real install.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".githooks" / "pre-push"
CHECK = REPO_ROOT / "scripts" / "check"
REVIEW = REPO_ROOT / "scripts" / "review-prepush"
ZERO = "0" * 40
IDENTITY = ["-c", "user.email=t@t.io", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def git(repo, *args):
    return subprocess.run(
        ["git", *IDENTITY, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"write {name}")
    return git(repo, "rev-parse", "HEAD")


def new_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "checkout", "-q", "-b", "trunk")
    return repo


def stub(bin_dir, name, body):
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def base_env(tmp_path, bin_dir, **extra):
    """A minimal environment: the stubs first on PATH, no GIT_* of the process running pytest."""
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C"}
    env.update(extra)
    return env


# --- .githooks/pre-push ---------------------------------------------------------------


@pytest.fixture
def hook(tmp_path):
    """A repository with two commits and a `just` stub that records each gate it runs."""
    repo = new_repo(tmp_path)
    first = commit(repo, "a.txt", "a\n")
    head = commit(repo, "a.txt", "b\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "just.calls"

    def just_does(preflight="", review=""):
        """The `just` stub: record the call and GIT_DIR ("-" for none), then run the action."""
        stub(bin_dir, "just", (
            f'echo "$* GIT_DIR=${{GIT_DIR:--}}" >> {calls}\n'
            f'case "$1" in preflight) {preflight or ":"} ;; review-prepush) {review or ":"} ;; esac\n'
        ))

    def push(*local):
        """Run the hook as git does, with one stdin line per pushed ref."""
        lines = "".join(
            f"refs/heads/b{i} {sha} refs/heads/b{i} {ZERO}\n" for i, sha in enumerate(local)
        )
        return subprocess.run(
            [str(HOOK), "origin", "git@example:x.git"],
            cwd=repo, input=lines, capture_output=True, text=True, check=False,
            env=base_env(tmp_path, bin_dir, GIT_DIR=str(repo / ".git")),
        )

    def just_calls():
        return calls.read_text() if calls.exists() else ""

    just_does()
    return type("Hook", (), {
        "repo": repo, "first": first, "head": head, "push": staticmethod(push),
        "just_does": staticmethod(just_does), "calls": staticmethod(just_calls),
    })


BOTH_GATES = "preflight GIT_DIR=-\nreview-prepush GIT_DIR=-\n"


def test_a_push_of_head_runs_the_preflight_then_the_review_without_git_dir(hook):
    """a push of HEAD from a clean tree runs `just preflight`, then `just review-prepush`"""
    done = hook.push(hook.head)
    assert done.returncode == 0, done.stderr
    assert hook.calls() == BOTH_GATES


def test_a_failed_preflight_stops_the_push_before_the_review(hook):
    """a preflight that fails ends the hook with its own status, and no review runs"""
    hook.just_does(preflight="exit 3")
    done = hook.push(hook.head)
    assert done.returncode == 3, done.stderr
    assert hook.calls() == "preflight GIT_DIR=-\n"


def test_a_failed_review_stops_the_push(hook):
    """a review that refuses ends the hook with the review's own status"""
    hook.just_does(review="exit 2")
    done = hook.push(hook.head)
    assert done.returncode == 2, done.stderr
    assert hook.calls() == BOTH_GATES


def test_a_push_of_another_commit_is_refused(hook):
    """a push that sends a commit other than HEAD stops before the gates"""
    done = hook.push(hook.first)
    assert done.returncode == 1
    assert f"sends {hook.first}, not HEAD {hook.head}" in done.stderr
    assert hook.calls() == ""


def test_a_tree_with_changes_is_refused(hook):
    """an untracked file is a change the push does not send, so no gate runs"""
    (hook.repo / "new.txt").write_text("x\n")
    done = hook.push(hook.head)
    assert done.returncode == 1
    assert "pre-push: the tree has changes before the gates" in done.stderr
    assert hook.calls() == ""


def test_a_ref_deletion_runs_the_gates(hook):
    """a deletion sends no commit, so its all-zero object is not compared with HEAD"""
    done = hook.push(ZERO)
    assert done.returncode == 0, done.stderr
    assert hook.calls() == BOTH_GATES


def test_a_push_of_several_refs_checks_each_line(hook):
    """HEAD and a deletion pass; one other commit among them stops the push"""
    done = hook.push(hook.head, ZERO)
    assert done.returncode == 0, done.stderr
    done = hook.push(hook.head, hook.first)
    assert done.returncode == 1
    assert f"sends {hook.first}, not HEAD {hook.head}" in done.stderr
    assert hook.calls() == BOTH_GATES


def test_a_git_status_that_fails_is_refused(hook):
    """a git status that fails says nothing about the tree, so no gate runs"""
    # Fails if the hook reads only the output of git status: an unreadable index prints
    # nothing on stdout, so the tree reads as clean and the gates run.
    (hook.repo / ".git" / "index").write_bytes(b"DIRC")
    done = hook.push(hook.head)
    assert done.returncode == 1, done.stderr
    assert "pre-push: git status failed before the gates" in done.stderr
    assert hook.calls() == ""


def test_a_tree_changed_during_the_gates_is_refused(hook):
    """a tree that changes while a gate runs stops the push after the gates"""
    hook.just_does(review=f"echo c > {hook.repo / 'a.txt'}")
    done = hook.push(hook.head)
    assert done.returncode == 1, done.stderr
    assert "pre-push: the tree has changes after the gates" in done.stderr
    assert hook.calls() == BOTH_GATES


def test_head_moved_during_the_gates_is_refused(hook):
    """a HEAD that moves while a gate runs stops the push after the gates"""
    hook.just_does(review="git -c user.email=t@t.io -c user.name=t commit -q --allow-empty -m c")
    done = hook.push(hook.head)
    assert done.returncode == 1, done.stderr
    assert f"pre-push: HEAD moved from {hook.head} to " in done.stderr


# --- scripts/check --------------------------------------------------------------------

CI_STEPS = ["lint", "types", "test", "build", "yaml", "audit"]


def run_check(tmp_path, *args, uvx_fails_on=None):
    """Run scripts/check with uv and uvx as stubs; return the result and the recorded calls.

    The uv stub makes the venv's bin/ and the wheel the script then runs and installs, and
    every program it makes records its own call too. The script's mktemp directory is shown
    as <tmp>, so the calls compare exactly. With ``uvx_fails_on``, a uvx call whose
    arguments hold that text exits 4.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "check.calls"
    record = f'echo "$(basename "$0") $*" >> {calls}\n'
    fail = f'case "$*" in *{uvx_fails_on}*) exit 4 ;; esac\n' if uvx_fails_on else ""
    stub(bin_dir, "uvx", record + fail)
    stub(bin_dir, "uv", record + (
        'for last; do :; done\n'
        'case "$1" in\n'
        '  venv) mkdir -p "$last/bin"\n'
        '        for b in pytest python; do\n'
        f'          printf \'#!/bin/sh\\necho "%s $*" >> {calls}\\n\' "$b" > "$last/bin/$b"\n'
        '          chmod +x "$last/bin/$b"\n'
        '        done ;;\n'
        '  build) mkdir -p "$3" && : > "$3/rocket_review-0-py3-none-any.whl" ;;\n'
        'esac\n'
    ))
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir(exist_ok=True)
    done = subprocess.run(
        [str(CHECK), *args], capture_output=True, text=True, check=False,
        env=base_env(tmp_path, bin_dir, TMPDIR=str(tmpdir)),
    )
    text = calls.read_text() if calls.exists() else ""
    return done, re.sub(re.escape(str(tmpdir)) + r"/tmp\.\w+", "<tmp>", text), tmpdir


def test_an_unknown_argument_is_refused(tmp_path):
    """scripts/check takes no argument, --quick, or one step name, and refuses any other"""
    for args in (["--quik"], [""], ["lint", "lint"], ["--quick", "lint"], ["check"]):
        done, calls, _ = run_check(tmp_path, *args)
        assert done.returncode == 2, args
        assert done.stderr.startswith("usage: scripts/check"), args
        assert calls == "", args


def test_quick_runs_lint_only(tmp_path):
    """--quick, the justfile's quick-gate, runs ruff check and stops"""
    done, calls, _ = run_check(tmp_path, "--quick")
    assert done.returncode == 0, done.stderr
    assert calls == "uvx ruff@0.14.0 check .\n"


def test_no_argument_runs_every_step_in_ci_order(tmp_path):
    """no argument runs the six CI gates in CI's order, and removes its temporary directory"""
    done, calls, tmpdir = run_check(tmp_path)
    assert done.returncode == 0, done.stderr
    assert re.findall(r"^== check (\w+)$", done.stdout, re.M) == CI_STEPS
    assert calls == (
        "uvx ruff@0.14.0 check .\n"
        "uvx --with openai mypy@1.18.2 rocket_review/\n"
        "uv venv --quiet <tmp>/test\n"
        "uv pip install --quiet --python <tmp>/test/bin/python -e .[dev]\n"
        "pytest -q\n"
        "uv build --out-dir <tmp>/dist\n"
        "uv venv --quiet <tmp>/smoke\n"
        "uv pip install --quiet --python <tmp>/smoke/bin/python "
        "<tmp>/dist/rocket_review-0-py3-none-any.whl\n"
        "python -c import rocket_review.cli, rocket_review.models, rocket_review.prompts, "
        "rocket_review.backends\n"
        "uvx yamllint@1.37.1 .\n"
        "uv venv --quiet <tmp>/audit\n"
        "uv pip install --quiet --python <tmp>/audit/bin/python -e .[dev]\n"
        "uv pip freeze --python <tmp>/audit/bin/python --exclude-editable\n"
        "uvx pip-audit@2.10.1 --strict --no-deps -s osv --requirement <tmp>/audit-requirements.txt\n"
    )
    assert list(tmpdir.iterdir()) == []


def test_a_failed_step_stops_the_run(tmp_path):
    """the first gate that fails ends the run with its status, and no later gate runs"""
    done, calls, _ = run_check(tmp_path, uvx_fails_on="mypy")
    assert done.returncode == 4, done.stderr
    assert re.findall(r"^== check (\w+)$", done.stdout, re.M) == ["lint", "types"]
    assert calls == "uvx ruff@0.14.0 check .\nuvx --with openai mypy@1.18.2 rocket_review/\n"


def test_ci_runs_each_gate_through_scripts_check():
    """each CI gate job runs one `scripts/check <step>`, and together they run every step"""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    jobs = workflow["jobs"]
    gates = [name for name in jobs if name != "ci-success"]
    assert sorted(jobs["ci-success"]["needs"]) == sorted(gates)
    steps = []
    for name in gates:
        runs = [s["run"] for s in jobs[name]["steps"] if "run" in s]
        assert len(runs) == 1, name
        match = re.fullmatch(r"scripts/check (\w+)", runs[0])
        assert match, (name, runs[0])
        steps.append(match.group(1))
    assert sorted(steps) == sorted(CI_STEPS)


def test_the_justfile_gates_call_scripts_check_and_scripts_review_prepush():
    """quick-gate, preflight and review-prepush are the scripts CI and the hook run"""
    recipes = dict(re.findall(r"^([\w-]+):\n    (.+)$", (REPO_ROOT / "justfile").read_text(), re.M))
    assert recipes["quick-gate"] == "scripts/check --quick"
    assert recipes["preflight"] == "scripts/check"
    assert recipes["review-prepush"] == "scripts/review-prepush"


def test_yamllint_skips_ignored_paths(tmp_path):
    """`yamllint .` with the repository's config lints no file under a gitignored directory"""
    shutil.copy(REPO_ROOT / ".yamllint.yaml", tmp_path / ".yamllint.yaml")
    shutil.copy(REPO_ROOT / ".gitignore", tmp_path / ".gitignore")
    bad = "---\na:  1\n"  # two spaces after the colon: an error under the default rules
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "bad.yaml").write_text(bad)

    def lint():
        return subprocess.run([sys.executable, "-m", "yamllint", "."], cwd=tmp_path,
                              capture_output=True, text=True, check=False)

    done = lint()
    assert done.returncode == 0, done.stdout
    # The same file outside the ignored directory fails, so the pass above is the ignore.
    (tmp_path / "bad.yaml").write_text(bad)
    done = lint()
    assert done.returncode == 1
    assert "./bad.yaml" in done.stdout and ".venv" not in done.stdout


# --- scripts/review-prepush -----------------------------------------------------------

GATE_FLAGS = "--json --fail-on high --backend codex,claude --timeout 3300"


@pytest.fixture
def review(tmp_path):
    """A branch one commit beyond origin/main, and an `rr` stub that records argv and stdin."""
    repo = new_repo(tmp_path)
    base = commit(repo, "a.txt", "a\n")
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    commit(repo, "a.txt", "b\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv, stdin = tmp_path / "rr.argv", tmp_path / "rr.stdin"
    stub(bin_dir, "rr", f'echo "$*" > {argv}\ncat > {stdin}\nexit "${{RR_EXIT:-0}}"\n')

    def run(**extra):
        return subprocess.run(
            [str(REVIEW)], cwd=repo, capture_output=True, text=True, check=False,
            env=base_env(tmp_path, bin_dir, **extra),
        )

    def got():
        return (argv.read_text().strip() if argv.exists() else None,
                stdin.read_text() if stdin.exists() else None)

    return type("Review", (), {
        "repo": repo, "base": base, "bin": bin_dir, "run": staticmethod(run),
        "got": staticmethod(got),
    })


def test_review_sends_the_branch_diff_with_the_gate_flags(review):
    """rr gets the diff from the merge base with origin/main to HEAD, and the gate flags"""
    done = review.run()
    assert done.returncode == 0, done.stderr
    assert "review-prepush: rr found no finding at or above high." in done.stderr
    patch = git(review.repo, "diff", review.base, "HEAD") + "\n"
    assert "+b" in patch
    assert review.got() == (GATE_FLAGS, patch)


def test_review_backends_come_from_review_backends(review):
    """REVIEW_BACKENDS names the backends; the rest of the gate flags stay"""
    done = review.run(REVIEW_BACKENDS="claude")
    assert done.returncode == 0, done.stderr
    assert review.got()[0] == "--json --fail-on high --backend claude --timeout 3300"


def test_a_stale_origin_main_gives_a_larger_diff_not_a_smaller_one(review):
    """the hook does not fetch: an origin/main behind a base the branch holds reviews more"""
    # The branch holds a newer main commit (c.txt) that this clone's origin/main has not seen.
    # Fails if the base is HEAD's parent or anything nearer than the merge base.
    git(review.repo, "reset", "-q", "--hard", review.base)
    commit(review.repo, "c.txt", "from a newer main\n")
    commit(review.repo, "a.txt", "b\n")
    done = review.run()
    assert done.returncode == 0, done.stderr
    patch = review.got()[1]
    assert "+from a newer main" in patch and "+b" in patch


def test_review_sends_git_s_own_patch(review, tmp_path):
    """a diff.external helper in the local config does not replace what rr reviews"""
    # Fails without --no-ext-diff: git hands the diff to the helper and rr gets its output.
    helper = stub(tmp_path, "helper", "echo not the patch\n")
    git(review.repo, "config", "diff.external", str(helper))
    done = review.run()
    assert done.returncode == 0, done.stderr
    assert review.got()[1].startswith("diff --git a/a.txt b/a.txt\n")


@pytest.mark.parametrize("rc, says", [
    (2, "rr found a finding at or above high"),
    (1, "the review exited 1, so no review verdict exists"),
    (3, "the review exited 3, so no review verdict exists"),
])
def test_review_refuses_on_every_nonzero_rr_exit(review, rc, says):
    """a finding (2), an rr fault or every backend failed (1), or any other exit refuses"""
    done = review.run(RR_EXIT=str(rc))
    assert done.returncode == rc
    assert f"review-prepush: {says}" in done.stderr


def test_review_without_rr_is_refused(review):
    """no rr on PATH means no review, and that refuses the push"""
    (review.bin / "rr").unlink()
    done = review.run()
    assert done.returncode == 1
    assert "review-prepush: rr is not on PATH" in done.stderr


def test_review_of_no_commits_is_refused(review):
    """HEAD at origin/main has nothing to review, so rr is not asked"""
    git(review.repo, "reset", "-q", "--hard", review.base)
    done = review.run()
    assert done.returncode == 1
    assert "HEAD has no commits beyond origin/main" in done.stderr
    assert review.got() == (None, None)


def test_review_without_origin_main_is_refused(review):
    """no origin/main gives no merge base and no branch diff, so rr is not asked"""
    git(review.repo, "update-ref", "-d", "refs/remotes/origin/main")
    done = review.run()
    assert done.returncode == 1
    assert "HEAD has no merge base with origin/main" in done.stderr
    assert review.got() == (None, None)


def test_the_gates_are_executable():
    """git ignores a hook without the executable bit, and the justfile runs the scripts directly"""
    for path in (HOOK, CHECK, REVIEW):
        assert os.access(path, os.X_OK), path
