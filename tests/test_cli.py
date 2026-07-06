import pytest

from rocket_review.cli import detect_mode, main


def run_cli(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["rr", *argv])
    with pytest.raises(SystemExit) as e:
        main()
    return e.value.code


def test_fail_on_requires_json(monkeypatch, capsys):
    code = run_cli(monkeypatch, ["--diff", "--fail-on", "high"])
    assert code == 1
    assert "--fail-on requires --json" in capsys.readouterr().err


def test_detect_mode_plan_for_markdown():
    assert detect_mode(["docs/plan.md"]) == "plan"
    assert detect_mode(["a.md", "b.txt", "c.plan"]) == "plan"


def test_detect_mode_code_for_source_files():
    assert detect_mode(["src/auth.py"]) == "code"


def test_detect_mode_mixed_is_code():
    assert detect_mode(["plan.md", "src/auth.py"]) == "code"
