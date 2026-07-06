import pytest

from rocket_review.cli import collect_docs, read_doc_with_links


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


def test_collect_docs_dedupes_repeated_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "llms.txt").write_text("llms index")
    out = collect_docs(["llms.txt"], "llms.txt")
    assert out.count("llms index") == 1


def test_collect_docs_nothing_given_is_none():
    assert collect_docs(None, None) is None
