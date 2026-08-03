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
    assert "skipping link ../secret.md" in capsys.readouterr().err


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


def repo_with_a_credentialed_remote(tmp_path, *, real=False):
    """A checkout whose .git/config holds the kind of secret a real one does."""
    if real:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(tmp_path), "config", key, value],
                           check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "add", "origin",
             f"https://x-token:{SECRET}@github.com/acme/private.git"],
            check=True, capture_output=True,
        )
    else:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text(
            f'[remote "origin"]\n\turl = https://x-token:{SECRET}@github.com/acme/private.git\n'
        )
    assert SECRET in (tmp_path / ".git" / "config").read_text()  # the setup itself, pinned
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
    repo = repo_with_a_credentialed_remote(tmp_path, real=True)
    (repo / "llms.txt").write_text("# llms\nno bare excepts\n[cfg](.git/config)\n")
    carry(repo, "llms.txt")
    monkeypatch.chdir(repo)
    out = collect_docs([], None)
    assert SECRET not in out
    assert "no bare excepts" in out


def test_read_doc_skips_an_unusable_link(tmp_path, capsys):
    (tmp_path / "llms.txt").write_text("[bad](a\x00b.md)\nkeep me\n")
    out = read_doc_with_links(tmp_path / "llms.txt")
    assert "keep me" in out
    assert "skipping link" in capsys.readouterr().err


def config_source(directory: Path, *, repo_supplied=True) -> DocsSource:
    """A `docs` value supplied by a config file in `directory`."""
    return DocsSource(
        config_file=directory / ".rocket-review.toml",
        discovery_root=directory,
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
    """A checkout carrying a standards doc, plus a gitignored .env that is not its content."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", key, value],
                       check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text(f"AWS_SECRET_ACCESS_KEY={ENV_SECRET}\n")
    (tmp_path / "llms.txt").write_text("# llms\nno bare excepts\n[e](.env)\n")
    carry(tmp_path, ".gitignore", "llms.txt")
    return tmp_path


def carry(repo: Path, *names: str) -> None:
    """Commit files, so the repository carries them at HEAD."""
    subprocess.run(["git", "-C", str(repo), "add", "-f", "--", *names],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "carry"],
                   check=True, capture_output=True)


def test_a_repo_chosen_doc_cannot_link_to_an_untracked_local_file(tmp_path, monkeypatch, capsys):
    # The class the .git guard only covered one instance of: an untracked file in the
    # working tree is the developer's, so repository content may not reach it.
    repo = repo_with_a_local_secret(tmp_path)
    monkeypatch.chdir(repo)
    out = collect_docs([], None, source=config_source(repo))
    assert ENV_SECRET not in out
    assert "no bare excepts" in out  # the tracked doc itself is still read
    assert "repository tracks it" in capsys.readouterr().err


def test_discovery_skips_a_standards_doc_the_repo_does_not_track(tmp_path, monkeypatch, capsys):
    # An untracked CLAUDE.md is someone's local file, not the project's standards — and a
    # config asked for it only by pattern, so it is skipped rather than refused.
    repo = repo_with_a_local_secret(tmp_path)
    (repo / "CLAUDE.md").write_text("LOCAL NOTES: staging password is hunter2\n")
    (repo / "llms.txt").unlink()
    monkeypatch.chdir(repo)
    assert collect_docs([], None, source=config_source(repo)) is None
    err = capsys.readouterr().err
    assert "skipping CLAUDE.md" in err and "repository tracks it" in err


@pytest.mark.parametrize("name", ["llms.txt", "AGENTS.md", "CLAUDE.md"])
def test_a_tracked_symlink_does_not_make_its_target_repo_content(tmp_path, monkeypatch, name):
    # The repository carries the *link*; what it points at is a different file, and here not
    # one the repo carries at all. Tracking is decided on the resolved path, and the tracked
    # set keeps the repository's own names — resolving both sides would collapse the two.
    repo = repo_with_a_local_secret(tmp_path)
    (repo / "llms.txt").unlink()
    (repo / name).symlink_to(".env")
    carry(repo, name)
    monkeypatch.chdir(repo)
    assert collect_docs([], None, source=config_source(repo)) is None
    assert collect_docs([], None, source=config_source(repo, repo_supplied=False)) is None


def test_a_tracked_symlink_out_of_the_repo_is_refused(tmp_path, monkeypatch):
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE-SECRET\n")
    repo = repo_with_a_local_secret(tmp_path / "repo")
    (repo / "llms.txt").unlink()
    (repo / "llms.txt").symlink_to("../outside.md")
    carry(repo, "llms.txt")
    monkeypatch.chdir(repo)
    assert collect_docs([], None, source=config_source(repo)) is None


def test_a_link_to_a_tracked_symlink_is_judged_by_its_target(tmp_path, monkeypatch, capsys):
    repo = repo_with_a_local_secret(tmp_path)
    (repo / "llms.txt").write_text("# llms\nno bare excepts\n[note](note.md)\n")
    (repo / "note.md").symlink_to(".env")
    carry(repo, "llms.txt", "note.md")
    monkeypatch.chdir(repo)
    out = collect_docs([], None, source=config_source(repo))
    assert ENV_SECRET not in out
    assert "no bare excepts" in out
    assert "skipping link note.md" in capsys.readouterr().err


def test_outside_a_repo_discovery_still_cannot_escape_its_directory(tmp_path, monkeypatch):
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE-SECRET\n")
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "llms.txt").symlink_to("../outside.md")
    monkeypatch.chdir(loose)
    assert collect_docs([], None, source=config_source(loose)) is None


def test_the_user_still_reaches_their_own_files_through_a_flag(tmp_path, monkeypatch):
    # No over-blocking: the restriction is about who chose the path, and here the user did.
    repo = repo_with_a_local_secret(tmp_path)
    monkeypatch.chdir(repo)
    assert ENV_SECRET in collect_docs([".env"], None)
    assert ENV_SECRET in collect_docs(None, ".env")
    assert ENV_SECRET in collect_docs([], ".env", source=config_source(repo))


def test_a_doc_the_user_named_still_does_not_vouch_for_its_links(tmp_path, monkeypatch, capsys):
    # What the user named is read; what that doc points at is written by whoever wrote the
    # doc, so inside a repository it is repository content and the same rule applies.
    repo = repo_with_a_local_secret(tmp_path)
    monkeypatch.chdir(repo)
    out = collect_docs(["llms.txt"], None)
    assert "no bare excepts" in out
    assert ENV_SECRET not in out
    assert "skipping link .env" in capsys.readouterr().err


def test_a_user_configs_own_paths_are_not_bounded_by_the_repo(tmp_path, monkeypatch):
    repo = repo_with_a_local_secret(tmp_path)
    monkeypatch.chdir(repo)
    source = config_source(repo, repo_supplied=False)
    assert ENV_SECRET in collect_docs([str(repo / ".env")], None, source=source)


def test_discovery_is_bounded_by_the_repo_for_a_user_config_too(tmp_path, monkeypatch, capsys):
    # A user config's `docs = true` does not name a file — the repository does, and it
    # decides what that file links to.
    repo = repo_with_a_local_secret(tmp_path)
    monkeypatch.chdir(repo)
    out = collect_docs([], None, source=config_source(repo, repo_supplied=False))
    assert ENV_SECRET not in out
    assert "no bare excepts" in out
    assert "repository tracks it" in capsys.readouterr().err


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
    assert "skipping link" in capsys.readouterr().err
