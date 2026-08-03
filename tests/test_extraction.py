"""What the api backend may read because the reviewed text named it.

A diff, a PR description or a standards doc is repository content, so a path it happens to
mention is the repository's word and not the user's: the file must be tracked at HEAD,
resolve inside the checkout, and never be repository metadata — the same rule, from the
same implementation, that every standards doc passes. Outside a checkout there is nothing
to track and confinement to the working directory is what remains.
"""

import subprocess
import types
from pathlib import Path

from rocket_review.backends import api

ENV_SECRET = "SECRET-env-DEADBEEF"
GITDIR_SECRET = "SECRET-gitdir-C0FFEE"
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


def sent_to_the_api(monkeypatch, content: str) -> str:
    """The user message the api backend would put on the wire for `content`."""
    import sys

    _FakeOpenAI.last_create_kwargs = None
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=_FakeOpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(api, "_load_env_file", lambda: None)
    api._call_openai(content, "instructions", "gpt-5.6-terra", None)
    return _FakeOpenAI.last_create_kwargs["input"]


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
