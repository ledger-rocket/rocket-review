import json
import re
from dataclasses import asdict, dataclass, field

SEVERITIES = ["critical", "high", "medium", "low"]
VERDICTS = ("approve", "needs_fixes", "blocker")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "title": {"type": "string"},
                    "file": {"type": ["string", "null"]},
                    "line": {"type": ["integer", "null"]},
                    "why": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "title", "file", "line", "why", "fix"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "summary", "findings"],
    "additionalProperties": False,
}


@dataclass
class Finding:
    severity: str
    title: str
    file: str | None = None
    line: int | None = None
    why: str | None = None
    fix: str | None = None
    backend: str | None = None
    model: str | None = None


@dataclass
class BackendResult:
    backend: str
    model: str | None
    verdict: str | None = None
    summary: str | None = None
    findings: list[Finding] = field(default_factory=list)
    raw: str = ""
    error: str | None = None
    parse_error: bool = False
    raw_file: str | None = None


def extract_json(text: str) -> dict | None:
    candidates = []
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_backend_output(text: str, backend: str, model: str | None) -> BackendResult:
    obj = extract_json(text)
    if obj is None or not isinstance(obj.get("findings"), list):
        return BackendResult(backend=backend, model=model, raw=text, parse_error=True)
    verdict = str(obj.get("verdict", "")).lower()
    if verdict not in VERDICTS:
        # A review that never reached a verdict didn't actually conclude anything;
        # treat it the same as unparsable output so the gate fails closed.
        return BackendResult(backend=backend, model=model, raw=text, parse_error=True)
    findings = []
    for f in obj["findings"]:
        if not isinstance(f, dict) or "severity" not in f or "title" not in f:
            return BackendResult(backend=backend, model=model, raw=text, parse_error=True)
        findings.append(Finding(
            severity=str(f["severity"]).lower(),
            title=str(f["title"]),
            file=f.get("file"),
            line=f["line"] if isinstance(f.get("line"), int) else None,
            why=f.get("why"),
            fix=f.get("fix"),
            backend=backend,
            model=model,
        ))
    # A needs_fixes verdict asserts changes are required but gives the --fail-on
    # threshold no findings to measure, so it can't confirm the issues are below
    # the caller's bar. Fail closed. (blocker+empty already fails closed via
    # should_fail's blocker-verdict path, which keeps the structured verdict.)
    if verdict == "needs_fixes" and not findings:
        return BackendResult(backend=backend, model=model, raw=text, parse_error=True)
    return BackendResult(
        backend=backend, model=model,
        verdict=verdict, summary=obj.get("summary"),
        findings=findings, raw=text,
    )


def _severity_rank(severity: str) -> int:
    # Unknown severities rank as critical: an LLM inventing a label must not slip the gate.
    s = severity.lower()
    return SEVERITIES.index(s) if s in SEVERITIES else 0


def should_fail(results: list[BackendResult], threshold: str) -> bool:
    # Fail closed: a backend that errored or produced unparsable output blocks the gate.
    if any(r.error is not None or r.parse_error for r in results):
        return True
    # A `blocker` verdict is the model's top-level "do not merge"; honor it even when
    # no individual finding reaches the threshold, or the gate could pass a review that
    # explicitly concluded the change must be blocked.
    if any(r.verdict == "blocker" for r in results):
        return True
    limit = SEVERITIES.index(threshold)
    return any(
        _severity_rank(f.severity) <= limit
        for r in results for f in r.findings
    )


def to_envelope(results: list[BackendResult], fail_on: str | None = None) -> dict:
    findings = [f for r in results for f in r.findings]
    by_severity = {s: 0 for s in SEVERITIES}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
    worst = min(findings, key=lambda f: _severity_rank(f.severity)).severity if findings else None
    summary = {
        "findings_total": len(findings),
        "by_severity": by_severity,
        "worst_severity": worst,
        "backends_total": len(results),
        "backends_errored": sum(1 for r in results if r.error),
        "backends_parse_failed": sum(1 for r in results if r.parse_error),
        "verdicts": [{"backend": r.backend, "verdict": r.verdict} for r in results],
        "gate": ({"threshold": fail_on, "tripped": should_fail(results, fail_on)} if fail_on else None),
    }
    return {
        # summary must stay first — consumers read the counts and gate state off the head
        # of the payload without parsing the findings array.
        "summary": summary,
        # Envelope contract: schema_version is a string, "1" is the first version; it is
        # bumped only on a breaking change (a field removed, a type changed, semantics
        # changed). Additive optional fields do not bump it, so consumers must tolerate
        # unknown keys. Consumers should match the value exactly against known versions —
        # no numeric ordering is implied — and treat any unrecognized value as unsupported.
        "schema_version": "1",
        "results": [asdict(r) for r in results],
        "findings": [asdict(f) for f in findings],
    }
