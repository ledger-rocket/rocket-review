import json
import types

import pytest

from rocket_review.backends.base import BackendError
from rocket_review.cli import RAW_TRUNCATE_LIMIT, detect_mode, main, parse_backend_arg

TEXT_HINT = "help: rr --diff --json --fail-on high (CI gate)"

REVIEW_JSON = (
    '{{"verdict": "needs_fixes", "summary": "s", "findings": '
    '[{{"severity": "high", "title": "t", "file": null, "line": null, '
    '"why": "w", "fix": "{body}"}}]}}'
)


def run_cli(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["rr", *argv])
    with pytest.raises(SystemExit) as e:
        main()
    return e.value.code


def run_main(monkeypatch, argv):
    """Drive main() tolerating the success path, which returns instead of exiting."""
    monkeypatch.setattr("sys.argv", ["rr", *argv])
    try:
        main()
    except SystemExit as e:
        return e.code if e.code is not None else 0
    return 0


def patch_backends(monkeypatch, reviews):
    """Swap in fake backends. reviews maps name -> output str, or a BackendError to raise."""
    def make_review(behavior):
        def review(job):
            if isinstance(behavior, BackendError):
                raise behavior
            return behavior
        return review

    fakes = {name: types.SimpleNamespace(review=make_review(b)) for name, b in reviews.items()}
    monkeypatch.setattr("rocket_review.cli.BACKENDS", fakes)
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)


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


def test_backend_arg_single_default():
    assert parse_backend_arg("codex", None) == [("codex", None)]


def test_backend_arg_list_with_permodel():
    assert parse_backend_arg("codex:gpt-5.5,claude", None) == [
        ("codex", "gpt-5.5"), ("claude", None),
    ]


def test_backend_arg_global_model_single():
    assert parse_backend_arg("claude", "claude-opus-4-8") == [("claude", "claude-opus-4-8")]


def test_backend_arg_global_model_multi_errors():
    with pytest.raises(SystemExit):
        parse_backend_arg("codex,claude", "gpt-5.5")


def test_backend_arg_unknown_errors():
    with pytest.raises(SystemExit):
        parse_backend_arg("gemini", None)


def test_backend_arg_duplicate_errors():
    with pytest.raises(SystemExit):
        parse_backend_arg("codex,codex", None)


def test_fanout_single_success_is_byte_identical(monkeypatch, capsys):
    patch_backends(monkeypatch, {"codex": "PROSE REVIEW"})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex"])
    out = capsys.readouterr()
    assert code == 0
    assert out.out == "PROSE REVIEW\n"  # no "## codex" header for a single backend


def test_fanout_all_fail_exits_1(monkeypatch, capsys):
    patch_backends(monkeypatch, {
        "codex": BackendError("codex boom"),
        "claude": BackendError("claude boom"),
    })
    code = run_main(monkeypatch, ["--diff", "--backend", "codex,claude"])
    err = capsys.readouterr().err
    assert code == 1
    assert "codex boom" in err and "claude boom" in err
    assert "some backends failed" not in err  # all-fail exits 1, no partial warning


def test_fanout_partial_fail_warns_and_exits_0(monkeypatch, capsys):
    patch_backends(monkeypatch, {
        "codex": "OK REVIEW",
        "claude": BackendError("claude boom"),
    })
    code = run_main(monkeypatch, ["--diff", "--backend", "codex,claude"])
    out = capsys.readouterr()
    assert code == 0
    assert "OK REVIEW" in out.out  # successful prose stays on stdout
    assert "claude boom" in out.err  # failed backend routed to stderr
    assert "some backends failed" in out.err


def test_fanout_gate_exits_2(monkeypatch):
    review_json = (
        '{"verdict": "needs_fixes", "summary": "s", "findings": '
        '[{"severity": "high", "title": "t", "file": null, "line": null, '
        '"why": "w", "fix": "f"}]}'
    )
    patch_backends(monkeypatch, {"codex": review_json})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex", "--json", "--fail-on", "high"])
    assert code == 2


def test_fanout_results_in_backend_order(monkeypatch, capsys):
    patch_backends(monkeypatch, {"codex": "FROM_CODEX", "claude": "FROM_CLAUDE"})
    code = run_main(monkeypatch, ["--diff", "--backend", "claude,codex"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.index("## claude") < out.index("## codex")  # --backend order preserved


def test_fanout_unexpected_worker_exception_surfaces_as_backend_error(monkeypatch, capsys):
    def make_review(behavior):
        def review(job):
            if isinstance(behavior, Exception):
                raise behavior
            return behavior
        return review

    fakes = {
        "codex": types.SimpleNamespace(review=make_review(ValueError("boom"))),
        "claude": types.SimpleNamespace(review=make_review("OK REVIEW")),
    }
    monkeypatch.setattr("rocket_review.cli.BACKENDS", fakes)
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)

    code = run_main(monkeypatch, ["--diff", "--backend", "codex,claude"])
    out = capsys.readouterr()
    assert code == 0  # not a traceback crash
    assert "OK REVIEW" in out.out  # sibling's completed review still delivered
    assert "ValueError: boom" in out.err  # surfaced as a backend error, not a crash
    assert "some backends failed" in out.err


def _json_envelope(monkeypatch, capsys, review_output, extra_args=()):
    patch_backends(monkeypatch, {"codex": review_output})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex", "--json", *extra_args])
    return code, json.loads(capsys.readouterr().out)


def test_json_truncates_long_raw_and_writes_full_to_file(monkeypatch, capsys):
    from pathlib import Path

    long = REVIEW_JSON.format(body="x" * 5000)
    assert len(long) > RAW_TRUNCATE_LIMIT
    code, env = _json_envelope(monkeypatch, capsys, long)
    assert code == 0
    r = env["results"][0]
    assert "(truncated," in r["raw"] and "use --full to inline" in r["raw"]
    assert len(r["raw"]) < len(long)
    assert Path(r["raw_file"]).read_text() == long  # full original preserved on disk


def test_json_short_raw_untouched(monkeypatch, capsys):
    short = REVIEW_JSON.format(body="short fix")
    assert len(short) <= RAW_TRUNCATE_LIMIT
    code, env = _json_envelope(monkeypatch, capsys, short)
    r = env["results"][0]
    assert r["raw"] == short
    assert r["raw_file"] is None


def test_json_truncation_survives_tempfile_failure(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("read-only fs")

    monkeypatch.setattr("rocket_review.cli.tempfile.NamedTemporaryFile", boom)
    long = REVIEW_JSON.format(body="x" * 5000)
    code, env = _json_envelope(monkeypatch, capsys, long)
    assert code == 0  # a full/read-only TMPDIR must not crash a good review
    r = env["results"][0]
    assert "could not write full text" in r["raw"]
    assert r["raw_file"] is None
    assert len(r["raw"]) < len(long)


def test_json_full_flag_inlines_everything(monkeypatch, capsys):
    long = REVIEW_JSON.format(body="x" * 5000)
    code, env = _json_envelope(monkeypatch, capsys, long, extra_args=["--full"])
    r = env["results"][0]
    assert r["raw"] == long
    assert r["raw_file"] is None
    assert "(truncated" not in r["raw"]


def test_help_shows_examples(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["rr", "--help"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0
    assert "examples:" in capsys.readouterr().out


def test_text_mode_prints_stderr_hint(monkeypatch, capsys):
    patch_backends(monkeypatch, {"codex": "PROSE REVIEW"})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex"])
    out = capsys.readouterr()
    assert code == 0
    assert TEXT_HINT in out.err  # next-step hint on stderr
    assert TEXT_HINT not in out.out  # never pollutes piped stdout


def test_json_mode_omits_hint(monkeypatch, capsys):
    review = REVIEW_JSON.format(body="f")
    patch_backends(monkeypatch, {"codex": review})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex", "--json"])
    out = capsys.readouterr()
    assert code == 0
    assert TEXT_HINT not in out.err and TEXT_HINT not in out.out


def test_text_mode_all_fail_omits_hint(monkeypatch, capsys):
    patch_backends(monkeypatch, {"codex": BackendError("codex boom")})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex"])
    out = capsys.readouterr()
    assert code == 1
    assert TEXT_HINT not in out.err  # no next-step hint on the error path
