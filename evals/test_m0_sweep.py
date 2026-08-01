"""Fixture tests for the sweep runner's envelope handling.

Only the pure parts are covered: nothing here launches rr or a backend. The sweep
itself is a script, never collected as a test.
"""

import json

from m0_sweep import BACKEND_ERROR, WRAPPED_COLUMN, RunRecord, _extract_backend_result
from strict_validator import DECODE_FAILURE, SCHEMA_VIOLATION, VALID


def envelope(*results) -> str:
    return json.dumps({"summary": {}, "results": list(results), "findings": []})


def test_extracts_the_requested_backend_entry():
    stdout = envelope(
        {"backend": "claude", "model": "sonnet", "raw": "claude output", "error": None},
        {"backend": "codex", "model": "gpt-5.6-sol", "raw": "codex output", "error": None},
    )
    result, error = _extract_backend_result(stdout, "codex")
    assert error is None
    assert result["raw"] == "codex output"
    assert result["model"] == "gpt-5.6-sol"


def test_entry_with_error_is_returned_for_the_caller_to_classify():
    stdout = envelope(
        {"backend": "codex", "model": None, "raw": "", "error": "codex timed out"},
    )
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


def record(**overrides) -> RunRecord:
    fields = {
        "case_id": "codex:abc1234:r1", "commit": "abc1234", "backend": "codex",
        "model": "gpt-5.6-sol", "run": 1, "command": ["rr"], "exit_code": 0,
        "duration_s": 1.0, "raw": "{}", "outcome": VALID, "errors": [], "excerpt": "",
        "bare_json": True, "backend_error": None, "started_at": "2026-01-01T00:00:00+00:00",
    }
    fields.update(overrides)
    return RunRecord(**fields)


def test_summary_counts_wrapped_output_separately_from_outcomes(capsys):
    from m0_sweep import print_summary

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
