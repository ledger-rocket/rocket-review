"""What the api backend may read because the reviewed text named it.

A diff, a PR description or a standards doc is repository content, so a path it happens to
mention is the repository's word and not the user's: the file must be tracked at HEAD,
resolve inside the checkout, and never be repository metadata — the same rule, from the
same implementation, that every standards doc passes. Outside a checkout there is nothing
to track and confinement to the working directory is what remains.
"""

import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rocket_review import cli, repo
from rocket_review.backends import api, base

ENV_SECRET = "SECRET-env-DEADBEEF"
GITDIR_SECRET = "SECRET-gitdir-C0FFEE"
OUTSIDE_SECRET = "SECRET-outside-B4DF00D"
GIT_IDENTITY = ("-c", "user.email=t@t", "-c", "user.name=t")


def make_repo(tmp_path: Path) -> Path:
    """A checkout carrying a tracked file, plus a gitignored secret that is not its content."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / ".gitignore").write_text("secrets/\n")
    (repo / "kept.py").write_text("KEPT = 1\n")
    (repo / "secrets").mkdir()
    (repo / "secrets" / ".env").write_text(f"AWS_SECRET_ACCESS_KEY={ENV_SECRET}\n")
    carry(repo, ".gitignore", "kept.py")
    return repo


def carry(repo: Path, *names: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-f", "--", *names],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), *GIT_IDENTITY, "commit", "-qm", "carry"],
                   check=True, capture_output=True)


class _FakeOpenAI:
    """Captures what the backend actually sends, which is where a leak would show up."""

    last_create_kwargs: dict | None = None

    def __init__(self, **kwargs):
        pass

    class models:
        @staticmethod
        def list():
            return []

    class responses:
        @staticmethod
        def create(**kwargs):
            _FakeOpenAI.last_create_kwargs = kwargs
            return type("R", (), {"output_text": "ok", "status": "completed", "output": []})()


def install_fake_openai(monkeypatch) -> None:
    import sys

    _FakeOpenAI.last_create_kwargs = None
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=_FakeOpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(api, "_load_env_file", lambda: None)


def sent_to_the_api(monkeypatch, content: str, **kwargs) -> str:
    """The user message the api backend would put on the wire for `content`."""
    install_fake_openai(monkeypatch)
    api._call_openai(content, "instructions", "gpt-5.6-terra", None, **kwargs)
    return _FakeOpenAI.last_create_kwargs["input"]


def review_job(content: str, **kwargs) -> base.ReviewJob:
    return base.ReviewJob(
        mode="diff", content=content, docs_content=None, extra=None, commit=None,
        pr=True, git_cmd=None, model="gpt-5.6-terra", **kwargs,
    )


def test_a_gitignored_secret_named_in_the_reviewed_text_never_reaches_the_api(
    tmp_path, monkeypatch
):
    # The leak: the reviewed text is the repository's word, so naming a path in it must not
    # be enough to have the file read and shipped.
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    sent = sent_to_the_api(monkeypatch, "the diff touches `secrets/.env` and `kept.py`")
    assert ENV_SECRET not in sent
    assert "KEPT = 1" in sent  # and the tracked file it also named is still attached


def test_a_path_inside_dot_git_named_in_the_reviewed_text_is_never_read(tmp_path, monkeypatch):
    # Repository metadata is local machine state — credentialed remote URLs above all — and
    # it is never repository content, so it fails on that count as well as on tracking.
    repo = make_repo(tmp_path)
    (repo / ".git" / "rr-probe.txt").write_text(GITDIR_SECRET)
    monkeypatch.chdir(repo)
    out = api.extract_referenced_files("the hook lives in `.git/rr-probe.txt`")
    assert GITDIR_SECRET not in out


def test_a_tracked_file_named_in_the_reviewed_text_is_still_extracted(tmp_path, monkeypatch):
    # The over-blocking guard: this is the whole feature, and it has to survive the gate.
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    out = api.extract_referenced_files("see `kept.py`")
    assert "KEPT = 1" in out
    assert "=== kept.py ===" in out


def test_a_tracked_file_is_extracted_when_named_from_a_subdirectory(tmp_path, monkeypatch):
    # rr is run from wherever the developer stands; a path resolved from there still has to
    # be judged against the checkout that tracks it.
    repo = make_repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("MOD = 1\n")
    carry(repo, "pkg/mod.py")
    monkeypatch.chdir(repo / "pkg")
    out = api.extract_referenced_files("see `mod.py`")
    assert "MOD = 1" in out


def test_a_staged_but_uncommitted_file_is_not_extracted(tmp_path, monkeypatch):
    # Deliberate, and the same answer the docs gate gives: HEAD is what the repository
    # carries, and the index would let a stray `git add` widen what it may offer. The file's
    # content is already in the diff under review anyway.
    repo = make_repo(tmp_path)
    (repo / "staged.py").write_text("STAGED = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "staged.py"], check=True, capture_output=True)
    monkeypatch.chdir(repo)
    out = api.extract_referenced_files("see `staged.py`")
    assert "STAGED = 1" not in out


def test_outside_a_checkout_extraction_stays_confined_to_the_working_directory(
    tmp_path, monkeypatch
):
    # No checkout, nothing to track: today's rule — inside the working directory — is all
    # that is left, and it is unchanged.
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "notes.md").write_text("LOOSE = 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "other.md").write_text("OUTSIDE = 1\n")
    monkeypatch.chdir(loose)
    monkeypatch.setattr(api, "_get_repo_root", lambda: loose.resolve())
    out = api.extract_referenced_files("see `notes.md` and `../outside/other.md`")
    assert "LOOSE = 1" in out
    assert "OUTSIDE = 1" not in out


# --- the checkout is the boundary, and `base` is what draws it ------------------------


def test_a_parent_escape_from_inside_a_checkout_is_refused(tmp_path, monkeypatch):
    # `base` is the repo root, so `../` out of it lands outside the confinement — and the
    # file is not the repository's to offer even though it sits one directory away.
    repo_dir = make_repo(tmp_path)
    (tmp_path / "neighbour.md").write_text(f"{OUTSIDE_SECRET}\n")
    monkeypatch.chdir(repo_dir)
    out = api.extract_referenced_files("see `../neighbour.md` and `kept.py`")
    assert OUTSIDE_SECRET not in out
    assert "KEPT = 1" in out


def test_a_tracked_symlink_pointing_out_of_the_checkout_is_refused(tmp_path, monkeypatch):
    # Tracking the *link* is not tracking what it opens. The gate answers on the resolved
    # path, so a tracked name cannot be used to launder a file from outside the repository.
    repo_dir = make_repo(tmp_path)
    (tmp_path / "elsewhere.md").write_text(f"{OUTSIDE_SECRET}\n")
    (repo_dir / "link.md").symlink_to(tmp_path / "elsewhere.md")
    carry(repo_dir, "link.md")
    monkeypatch.chdir(repo_dir)
    repo.clear_caches()
    out = api.extract_referenced_files("see `link.md`")
    assert OUTSIDE_SECRET not in out


# --- a different repository's text is not this checkout's word ------------------------


def test_a_foreign_repos_pr_attaches_nothing_even_when_the_path_is_tracked_here(
    tmp_path, monkeypatch, capsys
):
    # --repo names another repository. "Does the repository track it" would then be asked of
    # the wrong repository, and a path that repository's PR happens to mention would be
    # answered with this checkout's file. No gate can make that safe, so nothing is attached.
    repo_dir = make_repo(tmp_path)
    monkeypatch.chdir(repo_dir)
    install_fake_openai(monkeypatch)
    api.review(review_job("the remote PR touches `kept.py`", foreign_repo=True))
    sent = _FakeOpenAI.last_create_kwargs["input"]
    assert "KEPT = 1" not in sent
    assert "REFERENCED PROJECT FILES" not in sent
    assert "--repo names a different repository" in capsys.readouterr().err


def test_a_local_pr_still_attaches_a_tracked_file(tmp_path, monkeypatch):
    # `--pr N` without `--repo` reviews a PR against this checkout's own remote, which is
    # this repository's word — the over-blocking guard for the rule above.
    repo_dir = make_repo(tmp_path)
    monkeypatch.chdir(repo_dir)
    install_fake_openai(monkeypatch)
    api.review(review_job("the PR touches `kept.py`"))
    assert "KEPT = 1" in _FakeOpenAI.last_create_kwargs["input"]


# --- the gate runs in a worker thread, on the caller's clock ---------------------------


def test_the_gates_capture_returns_none_rather_than_exiting_when_a_command_hangs():
    # The real thing, not a stand-in: run_capture would print and sys.exit here.
    assert repo.capture([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1) is None


def test_the_gates_capture_returns_none_when_the_binary_is_missing(tmp_path):
    assert repo.capture([str(tmp_path / "no-such-git"), "rev-parse"]) is None


def test_a_timed_out_tracked_read_refuses_instead_of_exiting(tmp_path, monkeypatch):
    # sys.exit inside a backend worker raises SystemExit, which is a BaseException the
    # fan-out's handlers do not catch. The gate's own capture must fail closed instead.
    repo_dir = make_repo(tmp_path)
    monkeypatch.chdir(repo_dir)
    repo.clear_caches()
    monkeypatch.setattr(repo, "capture", lambda *a, **k: None)
    assert api.extract_referenced_files("see `kept.py`") == ""


def test_a_timed_out_gate_does_not_take_the_other_backend_down(tmp_path, monkeypatch):
    # The consequence that matters: one slow ls-tree must cost its own backend's attachments
    # and nothing else. A SystemExit here would surface out of future.result() and the whole
    # fan-out — including a concurrent backend's finished review — would be lost.
    repo_dir = make_repo(tmp_path)
    monkeypatch.chdir(repo_dir)
    repo.clear_caches()
    install_fake_openai(monkeypatch)
    monkeypatch.setattr(repo, "capture", lambda *a, **k: None)
    other = types.SimpleNamespace(review=lambda job: "OTHER REVIEW")
    monkeypatch.setitem(cli.BACKENDS, "codex", other)

    job = review_job("the diff touches `kept.py`")
    base.begin_fanout()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(cli.run_one, name, None, job, None) for name in ("api", "codex")
        ]
        results = [f.result() for f in futures]

    assert [name for name, _, _, _ in results] == ["api", "codex"]
    assert results[1][2] == "OTHER REVIEW"  # the other backend's result survived
    assert results[0][3] is None  # and the api backend still produced a review
    assert "KEPT = 1" not in _FakeOpenAI.last_create_kwargs["input"]


def test_extraction_is_skipped_when_the_timeout_leaves_less_than_the_gates_own_budget(
    tmp_path, monkeypatch, capsys
):
    # The gate's git call is bounded but not deducted from --timeout, so a run with less than
    # that left would spend the remainder deciding what to attach.
    repo_dir = make_repo(tmp_path)
    monkeypatch.chdir(repo_dir)
    sent = sent_to_the_api(
        monkeypatch, "the diff touches `kept.py`", timeout=repo.GATE_TIMEOUT - 1,
    )
    assert "KEPT = 1" not in sent
    assert f"under {repo.GATE_TIMEOUT}s of --timeout is left" in capsys.readouterr().err


# --- refusals say so ------------------------------------------------------------------


def test_a_withheld_local_file_is_reported_once_on_stderr(tmp_path, monkeypatch, capsys):
    repo_dir = make_repo(tmp_path)
    monkeypatch.chdir(repo_dir)
    api.extract_referenced_files("see `secrets/.env` and `kept.py`")
    err = capsys.readouterr().err
    assert "1 local file named in the reviewed text not attached" in err
    assert "the repository tracks it" in err


def test_a_path_the_checkout_does_not_have_is_not_reported_as_withheld(
    tmp_path, monkeypatch, capsys
):
    # Ordinary prose names files that are simply not here; calling those "withheld" would
    # make the note fire on every review and mean nothing when it mattered.
    repo_dir = make_repo(tmp_path)
    monkeypatch.chdir(repo_dir)
    api.extract_referenced_files("compare with `webpack.config.js` upstream")
    assert "not attached" not in capsys.readouterr().err


def test_a_candidate_carrying_a_nul_does_not_cost_the_others_their_attachment(
    tmp_path, monkeypatch
):
    # A diff is decoded with errors="replace", so a path with an embedded NUL can reach the
    # pattern; resolving it raises, and outside the gate that exception took every other
    # candidate's attachment with it.
    repo_dir = make_repo(tmp_path)
    monkeypatch.chdir(repo_dir)
    out = api.extract_referenced_files("see `bad\x00name.py` and `kept.py`")
    assert "KEPT = 1" in out


# --- the fold probe is an identity question, not an existence one ----------------------


def test_a_repository_carrying_a_literal_dot_git_directory_does_not_read_as_case_folding(
    tmp_path,
):
    # A repository is free to carry a path spelled `.GIT`. On a case-sensitive filesystem
    # that is a different directory, and reading it as "the filesystem folds" would key the
    # tracked set case-insensitively — quietly widening what a differently-cased name reaches.
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    try:
        (root / ".GIT").mkdir()
    except FileExistsError:
        pytest.skip("case-folding filesystem: .GIT and .git cannot be distinct here")
    repo.clear_caches()
    assert repo.case_folds(root) is False


def test_the_fold_probe_reports_folding_where_the_filesystem_really_folds(tmp_path):
    # The other half, as an observation rather than a constant: where the two names are one
    # directory, they are the same inode and the probe says so.
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    repo.clear_caches()
    assert repo.case_folds(root) is (root / ".GIT").exists()


def test_submodule_content_is_not_this_repositorys_to_offer(tmp_path, monkeypatch):
    # `ls-tree -r` stops at a gitlink rather than descending, so a path inside a submodule is
    # not tracked *by this repository* and is refused — fail-closed, and the same answer the
    # docs gate gives. SECURITY.md says so, which makes it load-bearing.
    inner = tmp_path / "inner"
    inner.mkdir()
    subprocess.run(["git", "init", "-q", str(inner)], check=True, capture_output=True)
    (inner / "secret.txt").write_text(f"{OUTSIDE_SECRET}\n")
    carry(inner, "secret.txt")
    outer = make_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(outer), "-c", "protocol.file.allow=always",
         *GIT_IDENTITY, "submodule", "add", "-q", str(inner), "sub"],
        check=True, capture_output=True,
    )
    carry(outer, ".gitmodules", "sub")
    monkeypatch.chdir(outer)
    repo.clear_caches()
    assert (outer / "sub" / "secret.txt").is_file()  # it is right there on disk
    out = api.extract_referenced_files("see `sub/secret.txt` and `kept.py`")
    assert OUTSIDE_SECRET not in out
    assert "KEPT = 1" in out
