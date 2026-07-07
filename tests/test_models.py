from rocket_review.models import (
    SEVERITIES, BackendResult, extract_json, parse_backend_output, should_fail, to_envelope,
)

GOOD = '{"verdict": "needs_fixes", "summary": "s", "findings": [{"severity": "HIGH", "title": "t", "file": "a.py", "line": 3, "why": "w", "fix": "f"}]}'


def test_extract_json_plain():
    assert extract_json(GOOD)["verdict"] == "needs_fixes"


def test_extract_json_fenced():
    assert extract_json(f"preamble\n```json\n{GOOD}\n```\ntrailer")["summary"] == "s"


def test_extract_json_prose_wrapped():
    assert extract_json(f"Here is my review: {GOOD} Hope it helps!") is not None


def test_extract_json_garbage_is_none():
    assert extract_json("no json here { broken") is None


def test_parse_normalizes_severity_and_tags_backend():
    r = parse_backend_output(GOOD, "codex", "gpt-5.5")
    assert not r.parse_error
    assert r.findings[0].severity == "high"
    assert r.findings[0].backend == "codex" and r.findings[0].model == "gpt-5.5"


def test_parse_failure_keeps_raw():
    r = parse_backend_output("plain text review", "claude", None)
    assert r.parse_error and r.raw == "plain text review" and r.findings == []


def test_should_fail_threshold():
    r = parse_backend_output(GOOD, "codex", None)
    assert should_fail([r], "high")
    assert should_fail([r], "low")          # high finding trips a lower bar too
    assert not should_fail([r], "critical")  # bar above the worst finding


def test_should_fail_unknown_severity_is_conservative():
    txt = GOOD.replace("HIGH", "bananas")
    assert should_fail([parse_backend_output(txt, "codex", None)], "critical")


def test_should_fail_closed_on_errors():
    assert should_fail([BackendResult(backend="codex", model=None, error="boom")], "critical")
    assert should_fail([parse_backend_output("not json", "codex", None)], "critical")


def test_parse_missing_verdict_is_parse_error():
    r = parse_backend_output('{"findings": []}', "api", None)
    assert r.parse_error


def test_parse_invalid_verdict_is_parse_error():
    txt = GOOD.replace("needs_fixes", "LGTM")
    r = parse_backend_output(txt, "api", None)
    assert r.parse_error


def test_parse_verdict_case_insensitive():
    r = parse_backend_output(GOOD.replace("needs_fixes", "Needs_Fixes"), "claude", None)
    assert not r.parse_error and r.verdict == "needs_fixes"


def test_envelope_merges_and_tags():
    r1 = parse_backend_output(GOOD, "codex", "gpt-5.5")
    r2 = parse_backend_output(GOOD, "claude", "claude-sonnet-5")
    env = to_envelope([r1, r2])
    assert len(env["results"]) == 2
    assert len(env["findings"]) == 2
    assert {f["backend"] for f in env["findings"]} == {"codex", "claude"}


def test_envelope_summary_is_first_key():
    env = to_envelope([parse_backend_output(GOOD, "codex", None)])
    assert list(env)[0] == "summary"


def test_summary_counts_across_backends():
    r1 = parse_backend_output(GOOD, "codex", "gpt-5.5")
    r2 = parse_backend_output(GOOD, "claude", "claude-sonnet-5")
    s = to_envelope([r1, r2])["summary"]
    assert s["findings_total"] == 2
    assert s["backends_total"] == 2
    assert s["backends_errored"] == 0
    assert s["backends_parse_failed"] == 0
    assert s["verdicts"] == [
        {"backend": "codex", "verdict": "needs_fixes"},
        {"backend": "claude", "verdict": "needs_fixes"},
    ]


def test_summary_by_severity_has_explicit_zeros():
    s = to_envelope([parse_backend_output(GOOD, "codex", None)])["summary"]
    assert s["by_severity"] == {"critical": 0, "high": 1, "medium": 0, "low": 0}


def test_summary_by_severity_unknown_appears_as_extra_key():
    txt = GOOD.replace("HIGH", "bananas")
    s = to_envelope([parse_backend_output(txt, "codex", None)])["summary"]
    assert s["by_severity"]["bananas"] == 1
    assert set(SEVERITIES) <= set(s["by_severity"])


def test_summary_worst_severity_and_none_when_empty():
    two = (
        '{"verdict": "needs_fixes", "summary": "s", "findings": ['
        '{"severity": "low", "title": "t", "file": null, "line": null, "why": "w", "fix": "f"},'
        '{"severity": "critical", "title": "t", "file": null, "line": null, "why": "w", "fix": "f"}'
        ']}'
    )
    assert to_envelope([parse_backend_output(two, "codex", None)])["summary"]["worst_severity"] == "critical"
    empty = '{"verdict": "approve", "summary": "s", "findings": []}'
    assert to_envelope([parse_backend_output(empty, "codex", None)])["summary"]["worst_severity"] is None


def test_summary_gate_absent_without_fail_on():
    assert to_envelope([parse_backend_output(GOOD, "codex", None)])["summary"]["gate"] is None


def test_summary_gate_tripped_true_and_false():
    r = parse_backend_output(GOOD, "codex", None)  # a single high finding
    tripped = to_envelope([r], fail_on="high")["summary"]["gate"]
    assert tripped == {"threshold": "high", "tripped": True}
    not_tripped = to_envelope([r], fail_on="critical")["summary"]["gate"]
    assert not_tripped == {"threshold": "critical", "tripped": False}


def test_summary_counts_errored_and_parse_failed():
    errored = BackendResult(backend="codex", model=None, error="boom")
    parse_failed = parse_backend_output("not json", "claude", None)
    s = to_envelope([errored, parse_failed])["summary"]
    assert s["backends_errored"] == 1
    assert s["backends_parse_failed"] == 1
