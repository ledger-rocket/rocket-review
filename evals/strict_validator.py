"""Strict `REVIEW_SCHEMA` compliance check for one backend's raw review output.

The runtime parser (`rocket_review.models.parse_backend_output`) is deliberately
lenient: it checks the verdict, that `findings` is a list, and each finding's
severity/title, then coerces the rest. Extra properties, invented severity labels
and missing required fields all survive it, so "parsed OK" at runtime says nothing
about whether a backend actually honoured the schema it was handed. This module
answers that separately, offline, and changes no runtime behaviour.

`REVIEW_SCHEMA` is imported, never copied: the measurement has to track whatever
the backends are actually sent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from rocket_review.models import REVIEW_SCHEMA, extract_json

# Outcome taxonomy. BACKEND_ERROR is never returned by classify_output — only the
# sweep runner can see a timeout or non-zero exit, so it records that one itself.
VALID = "valid"
SCHEMA_VIOLATION = "schema_violation"
DECODE_FAILURE = "decode_failure"
BACKEND_ERROR = "backend_error"

OUTCOMES = (VALID, SCHEMA_VIOLATION, DECODE_FAILURE, BACKEND_ERROR)

EXCERPT_CHARS = 400
MAX_ERRORS = 5

# The schema carries no $schema keyword, so pin the dialect rather than inheriting
# whatever the installed jsonschema happens to default to.
#
# check_schema at import: REVIEW_SCHEMA is runtime code this module only observes, so a
# future edit that makes it invalid under 2020-12 must fail loudly here rather than quietly
# turn into nonsense validation output that a whole sweep then gets scored against.
Draft202012Validator.check_schema(REVIEW_SCHEMA)
_VALIDATOR = Draft202012Validator(REVIEW_SCHEMA)


@dataclass(frozen=True)
class Classification:
    outcome: str
    #: First few validation messages, JSON-path prefixed (schema_violation only).
    errors: list[str] = field(default_factory=list)
    #: Head of the raw text, for triaging refusals and truncated responses by hand
    #: (decode_failure only). No heuristic classification of *why* it failed.
    excerpt: str = ""
    #: True when the whole output decoded as JSON directly. False means the object
    #: had to be dug out of a markdown fence or surrounding prose — accepted here,
    #: since the runtime accepts it, but the prompt asks for neither.
    bare_json: bool = False


def _excerpt(raw: str) -> str:
    head = raw[:EXCERPT_CHARS]
    return head + "…" if len(raw) > EXCERPT_CHARS else head


def _format_error(error: ValidationError) -> str:
    return f"{error.json_path}: {error.message}"


def classify_output(raw: str) -> Classification:
    """Classify one backend's raw review output against `REVIEW_SCHEMA`."""
    bare_json = True
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        bare_json = False
        # Fall back to the runtime's own unwrapping so a fenced-but-correct review is
        # measured as a formatting deviation, not lumped in with output carrying no
        # JSON at all.
        obj = extract_json(raw)
        if obj is None:
            return Classification(outcome=DECODE_FAILURE, excerpt=_excerpt(raw))

    # Sort on json_path (a string) rather than the raw path deque, whose elements mix
    # property names and array indices and so are not orderable against each other.
    errors = sorted(_VALIDATOR.iter_errors(obj), key=lambda e: e.json_path)
    if errors:
        return Classification(
            outcome=SCHEMA_VIOLATION,
            errors=[_format_error(e) for e in errors[:MAX_ERRORS]],
            bare_json=bare_json,
        )
    return Classification(outcome=VALID, bare_json=bare_json)
