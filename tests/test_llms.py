import pytest

from rocket_review.cli import read_llms


def test_read_llms_follows_relative_links(tmp_path):
    (tmp_path / "standards.md").write_text("# Standards\nno global mutable state")
    llms = tmp_path / "llms.txt"
    llms.write_text("# Project\n[standards](standards.md)\n[web](https://x.io/a.md)")
    out = read_llms(llms)
    assert "no global mutable state" in out
    assert "--- standards.md ---" in out


def test_read_llms_skips_traversal_outside_base(tmp_path, capsys):
    secret = tmp_path / "secret.md"
    secret.write_text("s3cret")
    project = tmp_path / "project"
    project.mkdir()
    llms = project / "llms.txt"
    llms.write_text("[up](../secret.md)")
    out = read_llms(llms)
    assert "s3cret" not in out
    assert "outside project" in capsys.readouterr().err


def test_read_llms_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        read_llms(tmp_path / "nope.txt")
