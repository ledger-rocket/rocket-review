"""Fixture tests for the sweep runner's envelope handling, arg parsing and summary.

No real backend is involved: the `run_case` tests point `--rr` at a generated stub whose
stdout and exit code the test dictates. The sweep itself is a script, never collected as
a test.
"""

import json
import sys
from pathlib import Path

from m0_sweep import (
    BACKEND_ERROR,
    WRAPPED_COLUMN,
    RunRecord,
    _extract_backend_result,
    parse_backend_specs,
    print_summary,
    run_case,
)
from strict_validator import DECODE_FAILURE, SCHEMA_VIOLATION, VALID


def envelope(*results) -> str:
    return json.dumps({"summary": {}, "results": list(results), "findings": []})


# --- envelope extraction -------------------------------------------------------------


def test_extracts_the_requested_backend_entry():
    stdout = envelope(
        {"backend": "claude", "model": "sonnet", "raw": "claude output", "error": None},
        {"backend": "codex", "model": "gpt-5.6-sol", "raw": "codex output", "error": None},
    )
    result, error = _extract_backend_result(stdout, "codex")
    assert error is None
    assert result["raw"] == "codex output"


def test_entry_with_error_is_returned_for_the_caller_to_classify():
    stdout = envelope({"backend": "codex", "raw": "", "error": "codex timed out"})
    result, error = _extract_backend_result(stdout, "codex")
    # The envelope itself is fine; the backend failure is the caller's to record.
    assert error is None
    assert result["error"] == "codex timed out"


def test_non_json_stdout_is_an_envelope_error():
    result, error = _extract_backend_result("Error: unknown commit deadbeef.", "codex")
    assert result is None
    assert error == "rr did not emit a JSON envelope"


def test_missing_backend_entry_is_an_envelope_error():
    result, error = _extract_backend_result(envelope(), "codex")
    assert result is None
    assert error == "no codex entry in rr envelope"


# Valid JSON of the wrong shape must degrade to a reason, never raise: an exception here
# would take down the sweep and lose an expensive run's record.
def test_json_null_envelope_is_reported_not_raised():
    assert _extract_backend_result("null", "codex") == (None, "rr envelope is not a JSON object")


def test_json_array_envelope_is_reported_not_raised():
    assert _extract_backend_result("[]", "codex") == (None, "rr envelope is not a JSON object")


def test_scalar_envelope_is_reported_not_raised():
    assert _extract_backend_result("42", "codex") == (None, "rr envelope is not a JSON object")


def test_null_results_is_reported_not_raised():
    stdout = json.dumps({"results": None})
    assert _extract_backend_result(stdout, "codex") == (None, "rr envelope has no results list")


def test_scalar_inside_results_is_skipped_not_raised():
    stdout = json.dumps({"results": ["nonsense", None, 7]})
    assert _extract_backend_result(stdout, "codex") == (None, "no codex entry in rr envelope")


# --- backend specs -------------------------------------------------------------------


def test_backend_spec_requires_a_model():
    specs, error = parse_backend_specs("codex")
    assert specs == []
    assert "must be name:model" in error


def test_backend_spec_rejects_an_empty_model():
    _, error = parse_backend_specs("codex:")
    assert "must be name:model" in error


def test_backend_spec_rejects_an_unknown_backend():
    _, error = parse_backend_specs("nope:some-model")
    assert "unknown backend" in error


def test_backend_spec_rejects_a_repeated_backend():
    # Two models for one backend would collide on case_id and merge in the summary.
    _, error = parse_backend_specs("codex:model-a,codex:model-b")
    assert "listed twice" in error


def test_backend_specs_parse_into_pairs():
    specs, error = parse_backend_specs("codex:gpt-5.6-sol, claude:sonnet")
    assert error is None
    assert specs == [("codex", "gpt-5.6-sol"), ("claude", "sonnet")]


# --- run_case against a stub rr ------------------------------------------------------


def stub_case(tmp_path: Path, stdout: str, stderr: str = "", exit_code: int = 0) -> RunRecord:
    """Run one case against an rr stand-in whose output the caller dictates."""
    stub = tmp_path / "stub_rr.py"
    stub.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return run_case(
        [sys.executable, str(stub)], tmp_path, "abc1234", "codex", "gpt-5.6-sol", 1, 30,
    )


GOOD_REVIEW = json.dumps({"verdict": "approve", "summary": "s", "findings": []})


def test_happy_path_is_classified(tmp_path):
    record = stub_case(tmp_path, envelope({"backend": "codex", "raw": GOOD_REVIEW}))
    assert record.outcome == VALID
    assert record.exit_code == 0
    assert record.requested_model == "gpt-5.6-sol"
    assert record.command[record.command.index("--backend") + 1] == "codex:gpt-5.6-sol"


def test_non_zero_exit_is_backend_error_even_with_a_complete_envelope(tmp_path):
    # rr prints its envelope before exiting 1, so stdout alone looks like a clean run;
    # exit status is what decides.
    record = stub_case(
        tmp_path, envelope({"backend": "codex", "raw": GOOD_REVIEW}),
        stderr="Warning: some backends failed", exit_code=1,
    )
    assert record.outcome == BACKEND_ERROR
    assert record.backend_error.startswith("rr exited 1")
    assert record.raw == ""


def test_non_zero_exit_prefers_the_envelope_error_text(tmp_path):
    record = stub_case(
        tmp_path, envelope({"backend": "codex", "raw": "", "error": "codex timed out"}),
        stderr="noise", exit_code=1,
    )
    assert "codex timed out" in record.backend_error


def test_non_zero_exit_without_an_envelope_falls_back_to_stderr(tmp_path):
    record = stub_case(tmp_path, "", stderr="Error: unknown commit deadbeef.", exit_code=1)
    assert record.outcome == BACKEND_ERROR
    assert "unknown commit deadbeef" in record.backend_error


def test_malformed_envelopes_still_produce_a_record(tmp_path):
    for stdout in ("null", "[]", "42", json.dumps({"results": None}), "not json at all"):
        record = stub_case(tmp_path, stdout)
        assert record.outcome == BACKEND_ERROR, stdout
        assert record.backend_error
        assert record.case_id == "codex:abc1234:r1"


def test_backend_error_entry_is_recorded_as_such(tmp_path):
    record = stub_case(tmp_path, envelope({"backend": "codex", "error": "codex not found"}))
    assert record.outcome == BACKEND_ERROR
    assert record.backend_error == "codex not found"


# --- summary -------------------------------------------------------------------------


def record(**overrides) -> RunRecord:
    fields = {
        "case_id": "codex:abc1234:r1", "commit": "abc1234", "backend": "codex",
        "requested_model": "gpt-5.6-sol", "run": 1, "command": ["rr"], "exit_code": 0,
        "duration_s": 1.0, "raw": "{}", "outcome": VALID, "errors": [], "excerpt": "",
        "bare_json": True, "backend_error": None, "started_at": "2026-01-01T00:00:00+00:00",
    }
    fields.update(overrides)
    return RunRecord(**fields)


def test_summary_counts_wrapped_output_separately_from_outcomes(capsys):
    print_summary([
        record(),
        record(outcome=VALID, bare_json=False),
        record(outcome=SCHEMA_VIOLATION, bare_json=False),
        # These two produced no JSON object to wrap, so bare_json is a default rather
        # than an observation and neither may be counted as a formatting deviation.
        record(outcome=DECODE_FAILURE, bare_json=False),
        record(outcome=BACKEND_ERROR, bare_json=False, backend_error="boom"),
    ], ["codex"])

    lines = capsys.readouterr().out.strip().splitlines()
    assert WRAPPED_COLUMN in lines[0]
    counts = [int(n) for n in lines[-1].split()[1:]]
    # valid, schema_violation, decode_failure, backend_error, fenced/wrapped, total
    assert counts == [2, 1, 1, 1, 2, 5]
