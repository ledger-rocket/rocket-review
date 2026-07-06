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
    if obj.get("verdict") not in VERDICTS:
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
    return BackendResult(
        backend=backend, model=model,
        verdict=obj.get("verdict"), summary=obj.get("summary"),
        findings=findings, raw=text,
    )


def _severity_rank(severity: str) -> int:
    # Unknown severities rank as critical: an LLM inventing a label must not slip the gate.
    s = severity.lower()
    return SEVERITIES.index(s) if s in SEVERITIES else 0


def should_fail(results: list[BackendResult], threshold: str) -> bool:
    # Fail closed: a backend that errored or produced unparsable output blocks the gate.
    if any(r.error or r.parse_error for r in results):
        return True
    limit = SEVERITIES.index(threshold)
    return any(
        _severity_rank(f.severity) <= limit
        for r in results for f in r.findings
    )


def to_envelope(results: list[BackendResult]) -> dict:
    return {
        "results": [asdict(r) for r in results],
        "findings": [asdict(f) for r in results for f in r.findings],
    }
