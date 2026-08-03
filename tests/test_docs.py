import subprocess
from pathlib import Path

import pytest

from rocket_review.cli import DocsSource, collect_docs, read_doc_with_links


def test_read_doc_follows_relative_links(tmp_path):
    (tmp_path / "standards.md").write_text("# Standards\nno global mutable state")
    llms = tmp_path / "llms.txt"
    llms.write_text("# Project\n[standards](standards.md)\n[web](https://x.io/a.md)")
    out = read_doc_with_links(llms)
    assert "no global mutable state" in out
    assert "--- standards.md ---" in out


def test_read_doc_skips_traversal_outside_base(tmp_path, capsys):
    secret = tmp_path / "secret.md"
    secret.write_text("s3cret")
    project = tmp_path / "project"
    project.mkdir()
    llms = project / "llms.txt"
    llms.write_text("[up](../secret.md)")
    out = read_doc_with_links(llms)
    assert "s3cret" not in out
    assert "outside project" in capsys.readouterr().err


def test_read_doc_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        read_doc_with_links(tmp_path / "nope.txt")


def test_collect_docs_bare_discovers_candidates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("agents rules")
    (tmp_path / "CLAUDE.md").write_text("claude rules")
    out = collect_docs([], None)
    assert "agents rules" in out and "claude rules" in out


def test_collect_docs_bare_none_found_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        collect_docs([], None)
    assert "none of" in capsys.readouterr().err


def test_collect_docs_explicit_missing_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        collect_docs(["nope.md"], None)


def test_collect_docs_llms_alias_combines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "llms.txt").write_text("llms index")
    (tmp_path / "extra.md").write_text("extra doc")
    out = collect_docs(["extra.md"], "llms.txt")
    assert "llms index" in out and "extra doc" in out
    assert out.index("llms index") < out.index("extra doc")


def test_collect_docs_dedupes_repeated_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "llms.txt").write_text("llms index")
    out = collect_docs(["llms.txt"], "llms.txt")
    assert out.count("llms index") == 1


def test_collect_docs_nothing_given_is_none():
    assert collect_docs(None, None) is None


SECRET = "SECRET-ghp-DEADBEEF"


def repo_with_a_credentialed_remote(tmp_path):
    """A checkout whose .git/config holds the kind of secret a real one does."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        f'[remote "origin"]\n\turl = https://x-token:{SECRET}@github.com/acme/private.git\n'
    )
    return tmp_path


@pytest.mark.parametrize("link", [".git/config", ".GIT/config", "./.Git/config"])
def test_read_doc_never_follows_a_link_into_repository_metadata(tmp_path, capsys, link):
    # A doc at the repo root has the whole repo inside its base directory, so traversal
    # confinement alone lets one hop reach .git — and the case-varied spellings open the
    # same file on a case-insensitive filesystem. This is the flag path too: the user
    # vouched for the doc, not for what it links to.
    repo = repo_with_a_credentialed_remote(tmp_path)
    standards = repo / "STANDARDS.md"
    standards.write_text(f"# Standards\nsmall diffs\nSee [remotes]({link}).\n")
    out = read_doc_with_links(standards)
    assert SECRET not in out
    assert "small diffs" in out  # the doc itself is still read
    assert "repository metadata" in capsys.readouterr().err


def test_collect_docs_discovery_never_carries_metadata_into_the_payload(tmp_path, monkeypatch):
    repo = repo_with_a_credentialed_remote(tmp_path)
    (repo / "llms.txt").write_text("# llms\nno bare excepts\n[cfg](.git/config)\n")
    monkeypatch.chdir(repo)
    out = collect_docs([], None)
    assert SECRET not in out
    assert "no bare excepts" in out


def test_read_doc_skips_an_unusable_link(tmp_path, capsys):
    (tmp_path / "llms.txt").write_text("[bad](a\x00b.md)\nkeep me\n")
    out = read_doc_with_links(tmp_path / "llms.txt")
    assert "keep me" in out
    assert "unusable link" in capsys.readouterr().err


def config_source(directory: Path, *, repo_root=None, repo_supplied=True) -> DocsSource:
    """A `docs` value supplied by a config file in `directory`."""
    return DocsSource(
        config_file=directory / ".rocket-review.toml",
        discovery_root=directory,
        repo_root=repo_root,
        repo_supplied=repo_supplied,
    )


def test_collect_docs_from_a_config_file_discovers_beside_that_file(tmp_path, monkeypatch):
    # A project standardising on `docs = true` must apply the same standards to everyone,
    # not only to whoever runs from the repo root.
    (tmp_path / "llms.txt").write_text("project standards")
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    out = collect_docs([], None, source=config_source(tmp_path))
    assert "project standards" in out


def test_collect_docs_from_a_config_file_is_silent_when_it_finds_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert collect_docs([], None, source=config_source(tmp_path)) is None


ENV_SECRET = "SECRET-env-DEADBEEF"


def repo_with_a_local_secret(tmp_path) -> Path:
    """A checkout holding a gitignored .env — the developer's file, not the project's."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text(f"AWS_SECRET_ACCESS_KEY={ENV_SECRET}\n")
    (tmp_path / "llms.txt").write_text("# llms\nno bare excepts\n[e](.env)\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore", "llms.txt"],
                   check=True, capture_output=True)
    return tmp_path


def test_a_repo_chosen_doc_cannot_link_to_an_untracked_local_file(tmp_path, monkeypatch, capsys):
    # The class the .git guard only covered one instance of: an untracked file in the
    # working tree is the developer's, so repository content may not reach it.
    repo = repo_with_a_local_secret(tmp_path)
    monkeypatch.chdir(repo)
    out = collect_docs([], None, source=config_source(repo, repo_root=repo))
    assert ENV_SECRET not in out
    assert "no bare excepts" in out  # the tracked doc itself is still read
    assert "does not track" in capsys.readouterr().err


def test_discovery_skips_a_standards_doc_the_repo_does_not_track(tmp_path, monkeypatch, capsys):
    # An untracked CLAUDE.md is someone's local file, not the project's standards — and a
    # config asked for it only by pattern, so it is skipped rather than refused.
    repo = repo_with_a_local_secret(tmp_path)
    (repo / "CLAUDE.md").write_text("LOCAL NOTES: staging password is hunter2\n")
    (repo / "llms.txt").unlink()
    monkeypatch.chdir(repo)
    assert collect_docs([], None, source=config_source(repo, repo_root=repo)) is None
    assert "skipping CLAUDE.md, which the repository does not track" in capsys.readouterr().err


def test_the_user_still_reaches_their_own_files_through_a_flag(tmp_path, monkeypatch):
    # No over-blocking: the restriction is about who chose the path, and here the user did.
    repo = repo_with_a_local_secret(tmp_path)
    monkeypatch.chdir(repo)
    assert ENV_SECRET in collect_docs([".env"], None)
    assert ENV_SECRET in collect_docs(["llms.txt"], None)  # links followed as before
    assert ENV_SECRET in collect_docs([], ".env")  # --llms, alongside a config-supplied docs
    assert ENV_SECRET in collect_docs(
        [], ".env", source=config_source(repo, repo_root=repo)
    )


def test_a_user_configs_own_paths_are_not_bounded_by_the_repo(tmp_path, monkeypatch):
    repo = repo_with_a_local_secret(tmp_path)
    monkeypatch.chdir(repo)
    source = config_source(repo, repo_root=repo, repo_supplied=False)
    assert ENV_SECRET in collect_docs([str(repo / ".env")], None, source=source)


def test_discovery_is_bounded_by_the_repo_for_a_user_config_too(tmp_path, monkeypatch, capsys):
    # A user config's `docs = true` does not name a file — the repository does, and it
    # decides what that file links to.
    repo = repo_with_a_local_secret(tmp_path)
    monkeypatch.chdir(repo)
    out = collect_docs([], None, source=config_source(repo, repo_root=repo, repo_supplied=False))
    assert ENV_SECRET not in out
    assert "no bare excepts" in out
    assert "does not track" in capsys.readouterr().err


def test_read_doc_blocks_sibling_prefix_escape(tmp_path, capsys):
    project = tmp_path / "proj"
    sibling = tmp_path / "proj-secret"
    project.mkdir()
    sibling.mkdir()
    (sibling / "leak.md").write_text("s3cret")
    llms = project / "llms.txt"
    llms.write_text("[x](../proj-secret/leak.md)")
    out = read_doc_with_links(llms)
    assert "s3cret" not in out  # /proj-secret must not pass as inside /proj
    assert "outside project" in capsys.readouterr().err
