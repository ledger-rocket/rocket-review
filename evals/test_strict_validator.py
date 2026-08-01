"""Fixture tests for the strict validator.

Deterministic and offline: no backend is invoked here. The sweep runner
(`m0_sweep.py`) is a script, not a test module, so it is never collected.
"""

import json

from strict_validator import (
    DECODE_FAILURE,
    SCHEMA_VIOLATION,
    VALID,
    classify_output,
)


def review(**overrides) -> dict:
    obj = {
        "verdict": "needs_fixes",
        "summary": "One issue found.",
        "findings": [{
            "severity": "high",
            "title": "Unbounded retry loop",
            "file": "app/worker.py",
            "line": 42,
            "why": "A permanent failure retries forever.",
            "fix": "Cap attempts and surface the error.",
        }],
    }
    obj.update(overrides)
    return obj


def test_compliant_review_is_valid():
    result = classify_output(json.dumps(review()))
    assert result.outcome == VALID
    assert result.bare_json
    assert result.errors == []


def test_approve_with_no_findings_is_valid():
    result = classify_output(json.dumps(review(verdict="approve", findings=[])))
    assert result.outcome == VALID


def test_extra_property_is_a_violation():
    result = classify_output(json.dumps(review(confidence=0.9)))
    assert result.outcome == SCHEMA_VIOLATION
    assert any("confidence" in e for e in result.errors)


def test_extra_property_inside_a_finding_is_a_violation():
    finding = review()["findings"][0] | {"category": "reliability"}
    result = classify_output(json.dumps(review(findings=[finding])))
    assert result.outcome == SCHEMA_VIOLATION
    assert any("category" in e for e in result.errors)


def test_invalid_severity_value_is_a_violation():
    finding = review()["findings"][0] | {"severity": "blocker"}
    result = classify_output(json.dumps(review(findings=[finding])))
    assert result.outcome == SCHEMA_VIOLATION
    assert any("severity" in e for e in result.errors)


def test_uppercase_severity_is_a_violation():
    # The runtime parser lowercases severity before checking it, so this shape passes
    # at runtime while violating the enum the backend was handed — exactly the gap
    # this tooling exists to quantify.
    finding = review()["findings"][0] | {"severity": "HIGH"}
    assert classify_output(json.dumps(review(findings=[finding]))).outcome == SCHEMA_VIOLATION


def test_missing_required_field_is_a_violation():
    obj = review()
    del obj["summary"]
    result = classify_output(json.dumps(obj))
    assert result.outcome == SCHEMA_VIOLATION
    assert any("summary" in e for e in result.errors)


def test_missing_required_finding_field_is_a_violation():
    finding = {k: v for k, v in review()["findings"][0].items() if k != "fix"}
    result = classify_output(json.dumps(review(findings=[finding])))
    assert result.outcome == SCHEMA_VIOLATION
    assert any("fix" in e for e in result.errors)


def test_error_list_is_capped_and_path_prefixed():
    finding = {"severity": "urgent", "title": 7}
    result = classify_output(json.dumps(review(verdict="ship_it", findings=[finding])))
    assert result.outcome == SCHEMA_VIOLATION
    assert len(result.errors) <= 5
    assert any(e.startswith("$.findings[0]") for e in result.errors)


def test_prose_is_a_decode_failure():
    raw = "I can't review this diff without more context."
    result = classify_output(raw)
    assert result.outcome == DECODE_FAILURE
    assert result.excerpt == raw
    assert result.errors == []


def test_truncated_json_is_a_decode_failure():
    result = classify_output(json.dumps(review())[:80])
    assert result.outcome == DECODE_FAILURE


def test_decode_failure_excerpt_is_bounded():
    result = classify_output("x" * 5000)
    assert result.outcome == DECODE_FAILURE
    assert len(result.excerpt) == 401  # 400 chars plus the elision marker


def test_fenced_review_is_valid_but_not_bare():
    result = classify_output(f"```json\n{json.dumps(review())}\n```")
    assert result.outcome == VALID
    assert not result.bare_json


def test_non_object_json_is_a_violation_not_a_decode_failure():
    result = classify_output("[]")
    assert result.outcome == SCHEMA_VIOLATION
