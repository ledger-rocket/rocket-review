from rocket_review.cli import detect_mode


def test_detect_mode_plan_for_markdown():
    assert detect_mode(["docs/plan.md"]) == "plan"
    assert detect_mode(["a.md", "b.txt", "c.plan"]) == "plan"


def test_detect_mode_code_for_source_files():
    assert detect_mode(["src/auth.py"]) == "code"


def test_detect_mode_mixed_is_code():
    assert detect_mode(["plan.md", "src/auth.py"]) == "code"
