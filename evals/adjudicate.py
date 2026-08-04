"""The adjudication scorer: human finding-decisions in, one binding verdict out.

Tier 1 says whether a prompt change moved anything worth adjudicating. This says whether
the change may ship. Its inputs are a paired-run JSONL, the case manifests, and a YAML
adjudication artifact carrying one recorded human decision per finding that a decision
rule reads. Its output is the veto result, per-class recall, the success criterion, and a
single final verdict: CERTIFIED, NOT CERTIFIED or VETOED.

Every aggregation rule applied here is pre-registered in evals/README.md and committed
before any decision sweep runs. That is the whole point of the module: a threshold or a
denominator chosen after seeing results is not evidence, so none of them is chosen at
report time. The human input is one call per finding, on the question the README says
cannot be automated — whether a finding's `why` describes the injected defect, and whether
a high-severity finding on a clean control is noise or a real bug. Everything downstream
of that call is arithmetic fixed in advance.

No backend is called and no git object is read: the verdict is a pure function of the
three input files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import NoReturn

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import CASES_DIR, Case, CaseError, load_cases  # noqa: E402
from eval_common import REPO_ROOT  # noqa: E402
from paired_runner import CONTROL, TREATMENT  # noqa: E402
from strict_validator import VALID  # noqa: E402
from tier1 import (  # noqa: E402
    IncompletePair,
    final_attempts,
    load_jsonl,
    normalize_cited_path,
    severity_bucket,
    split_complete_pairs,
    sweep_ids,
    unit_key,
)

from rocket_review.models import Finding, parse_backend_output  # noqa: E402

#: The finding's `why` describes the injected defect — the recorded human call that
#: rule 3 of the scoring rule is made of. Only valid on a defect case, and only on a
#: finding that already satisfies rules 1 and 2.
MATCHES_DEFECT = "matches-defect"
#: A genuine finding that is not the injected defect. Ignored by the scoring rule: no
#: recall credit and no false-positive debit, because the case was built to test one
#: thing and the rest of the diff was never adjudicated.
REAL_UNRELATED = "real-unrelated"
#: A clean-control finding confirmed as noise. The veto's numerator.
FALSE_POSITIVE = "false-positive"
#: The seeded defect does not measure what it claims to: a "clean" control carrying a real
#: bug, or a defect case whose defect turned out not to be one. Removes the whole mutation
#: — every encoding of it — from every certification number, for both arms.
CASE_INVALIDATED = "case-invalidated"

DECISIONS = (MATCHES_DEFECT, REAL_UNRELATED, FALSE_POSITIVE, CASE_INVALIDATED)

#: Severities the veto counts. Anything a model invented buckets to `other` in tier 1 and
#: is not a CRITICAL or a HIGH here either — same rule, same reason.
VETO_SEVERITIES = ("critical", "high")

CERTIFIED = "CERTIFIED"
NOT_CERTIFIED = "NOT CERTIFIED"
VETOED = "VETOED"

#: Pre-registered thresholds. Named rather than inlined so that changing one is a visible
#: diff against the README paragraph that argues for it.
VETO_PER_CASE_SLACK = Fraction(1, 2)
CRITERION_RELATIVE_GAIN = Fraction(1, 5)
CRITERION_ABSOLUTE_GAIN = 2
CERTIFYING_CLASS_MIN_CASES = 5
#: Complete repetitions a case needs before anything it contributes can certify. At n=1 a
#: strict majority is satisfied by a single run, which is the any-run rule the majority
#: rule exists to reject — so the depth the thresholds were argued at is enforced, not
#: assumed. Blocking is deliberately not gated on it: a shallow sweep still vetoes.
PROTOCOL_MIN_REPS = 5

@dataclass(frozen=True)
class ArmRules:
    """Pre-registrations that bind one arm, committed before the sweep they govern.

    They live here rather than only in the arm's README because the verdict this scorer
    prints is binding and its numbers may not be recombined by hand — so a rule the
    decision depends on has to be one the scorer reads. See `evals/README.md`,
    *The language-checks arm*.
    """

    #: The clean controls this arm's added text can reach at all. The veto's aggregate is
    #: pooled over every clean control, so an arm that is inert on some of them can have
    #: those cases carry the ones it is not inert on; an arm listed here has to clear the
    #: aggregate over this subset as well. Pinned against the corpus in `test_cases.py`.
    control_subset: frozenset[str] = frozenset()
    #: The one defect class a gain may be certified on. Two verdict-grade classes would
    #: give one sweep two independent draws at the success criterion, and a class whose
    #: members instantiate nothing the arm added cannot attribute a gain to it either.
    certifying_class: str | None = None


#: Keyed by the arm under test — the treatment — under its directory name.
ARM_RULES: dict[str, ArmRules] = {
    "lang-python": ArmRules(
        control_subset=frozenset({"c-003", "c-004", "c-006"}),
        certifying_class="swallowed-error",
    ),
}

#: What an arm nobody pre-registered anything for is scored under: every clean control in
#: the aggregate, any verdict-grade class able to certify.
NO_ARM_RULES = ArmRules()

#: Exit status. 0/1/2 are the three verdicts; nothing that is not a computed verdict may
#: land on one of them.
EXIT_STATUS = {CERTIFIED: 0, NOT_CERTIFIED: 1, VETOED: 2}
#: The inputs could not support a verdict at all — unreadable, incomplete, nothing scored.
EXIT_NO_VERDICT = 3
#: `--pending` drafted a work list. Nothing was decided, so it cannot exit 0.
EXIT_DRAFTED = 4
#: A usage error, kept well clear of the verdicts. argparse exits 2 by default, which is
#: VETOED here: a caller gating on status would read a mistyped flag as a blocked change.
EXIT_USAGE = 64

TOP_LEVEL_KEYS = {
    "sweep_id", "results_file", "adjudicator", "corpus_digest", "results_digest", "decisions",
}
DECISION_KEYS = {
    "case_id", "backend", "arm", "arm_role", "rep", "finding_index", "decision", "rationale",
}


class AdjudicationError(Exception):
    """The adjudication artifact does not describe a scorable set of decisions."""


class IncompleteAdjudication(AdjudicationError):
    """Findings a decision rule reads have no recorded human call.

    Separate from the base class because it is not a malformed artifact — it is a
    complete one that is not finished. The veto cannot compute without every
    CRITICAL/HIGH control finding adjudicated, and class recall cannot compute without
    every finding sitting on a defect's own lines adjudicated.
    """

    def __init__(self, missing: list[PendingFinding]) -> None:
        super().__init__(f"{len(missing)} finding(s) still need a recorded decision")
        self.missing = missing


@dataclass(frozen=True)
class Adjudication:
    """One recorded human call, joined to a result row by everything but `finding_index`.

    The key is `tier1.unit_key` minus the sweep (which the artifact carries once, at the
    top) plus the finding's position in the parsed findings array of that unit's final
    attempt. `arm_role` is in the key for the reason it is in `unit_key`: an A/A run puts
    the same arm name on two different runs of the same repetition.
    """

    case_id: str
    backend: str
    arm: str
    arm_role: str
    rep: int
    finding_index: int
    decision: str
    rationale: str

    @property
    def key(self) -> tuple:
        return (
            self.case_id, self.backend, self.arm, self.arm_role, self.rep, self.finding_index,
        )


@dataclass(frozen=True)
class AdjudicationFile:
    sweep_id: str
    #: Provenance only; the join is on sweep_id, which is checked against the results.
    results_file: str | None
    adjudicator: str | None
    #: What these calls were made against. Verified before anything is scored: a manifest
    #: or a results file edited afterwards re-decides things the calls cannot be re-made
    #: for. See `corpus_digest` and `results_digest`.
    corpus_digest: str
    results_digest: str
    decisions: tuple[Adjudication, ...]
    path: Path


@dataclass(frozen=True)
class PendingFinding:
    """A finding a decision rule reads, described well enough to adjudicate from."""

    case_id: str
    backend: str
    arm: str
    arm_role: str
    rep: int
    finding_index: int
    #: Which rule needs this call, in the words of the rule.
    reason: str
    severity: str
    title: str

    @property
    def key(self) -> tuple:
        return (
            self.case_id, self.backend, self.arm, self.arm_role, self.rep, self.finding_index,
        )


# --- loading and validating the artifact ---------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AdjudicationError(message)


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a duplicate mapping key instead of silently keeping the last.

    PyYAML's default is to keep the last, so an entry carrying two `decision:` lines parses
    as whichever came second. In an artifact whose entire job is to be the audit trail, a
    key that is quietly dropped is the one place a recorded call could differ from the call
    that was actually made.
    """


def _no_duplicate_keys(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    seen: list = []
    duplicates: list = []
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        # Membership on a list compares with __eq__ and never hashes, so an unhashable key
        # reaches PyYAML's own error rather than blowing up here.
        (duplicates if key in seen else seen).append(key)
    if duplicates:
        raise yaml.constructor.ConstructorError(
            None, None,
            f"duplicate key(s): {', '.join(sorted(repr(k) for k in duplicates))}",
            node.start_mark,
        )
    return loader.construct_mapping(node, deep=True)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys,
)


def _string_keys(raw: dict, where: str) -> None:
    """Refuse a mapping with non-string keys before anything tries to sort or join them."""
    offenders = [k for k in raw if not isinstance(k, str)]
    _require(
        not offenders,
        f"{where}: keys must be strings, got {', '.join(sorted(repr(k) for k in offenders))}",
    )


def load_adjudications(path: Path) -> AdjudicationFile:
    """Parse and structurally validate one adjudication file. Every rejection names why."""
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except (OSError, yaml.YAMLError, ValueError) as e:
        raise AdjudicationError(f"{path}: could not read adjudications: {e}") from e
    _require(isinstance(raw, dict), f"{path}: adjudications must be a YAML mapping")
    assert isinstance(raw, dict)
    _string_keys(raw, str(path))
    unknown = set(raw) - TOP_LEVEL_KEYS
    _require(not unknown, f"{path}: unknown keys: {', '.join(sorted(unknown))}")
    _require(
        isinstance(raw.get("sweep_id"), str) and raw["sweep_id"].strip() != "",
        f"{path}: sweep_id is required — a decision only means anything about the sweep "
        "whose rows it was read from",
    )
    for key in ("corpus_digest", "results_digest"):
        _require(
            isinstance(raw.get(key), str) and raw[key].strip() != "",
            f"{path}: {key} is required — it is what binds these calls to the inputs they "
            "were made against. `--pending` writes it; an artifact without one cannot be "
            "shown to describe this sweep's findings at all.",
        )
    entries = raw.get("decisions")
    _require(isinstance(entries, list), f"{path}: decisions must be a list")
    assert isinstance(entries, list)

    decisions: list[Adjudication] = []
    seen: dict[tuple, int] = {}
    for number, entry in enumerate(entries, start=1):
        where = f"{path}: decisions[{number}]"
        decisions.append(_parse_decision(entry, where))
        key = decisions[-1].key
        _require(
            key not in seen,
            f"{where}: duplicates decisions[{seen.get(key)}] — one finding, one call",
        )
        seen[key] = number
    return AdjudicationFile(
        sweep_id=raw["sweep_id"],
        results_file=raw.get("results_file"),
        adjudicator=raw.get("adjudicator"),
        corpus_digest=raw["corpus_digest"],
        results_digest=raw["results_digest"],
        decisions=tuple(decisions),
        path=path,
    )


def _parse_decision(raw: object, where: str) -> Adjudication:
    _require(isinstance(raw, dict), f"{where}: must be a mapping")
    assert isinstance(raw, dict)
    _string_keys(raw, where)
    unknown = set(raw) - DECISION_KEYS
    _require(not unknown, f"{where}: unknown keys: {', '.join(sorted(unknown))}")
    missing = DECISION_KEYS - set(raw)
    _require(not missing, f"{where}: missing keys: {', '.join(sorted(missing))}")
    for key in ("case_id", "backend", "arm", "arm_role"):
        _require(
            isinstance(raw[key], str) and raw[key].strip() != "",
            f"{where}: {key} must be a non-empty string",
        )
    # bool is an int in Python and would sail through an isinstance check, keying a
    # decision to rep True.
    for key in ("rep", "finding_index"):
        _require(
            isinstance(raw[key], int) and not isinstance(raw[key], bool),
            f"{where}: {key} must be an integer",
        )
    _require(raw["rep"] >= 1, f"{where}: rep must be 1 or greater")
    _require(raw["finding_index"] >= 0, f"{where}: finding_index must be 0 or greater")
    _require(
        raw["decision"] in DECISIONS,
        f"{where}: decision must be one of {', '.join(DECISIONS)}, got "
        f"{raw['decision']!r}",
    )
    # The rationale is the audit trail: without it the artifact records that a call was
    # made but not what it was made on, which is exactly the state this scorer exists to
    # make impossible. One line, because it is read in a list beside dozens of others.
    _require(
        isinstance(raw["rationale"], str) and raw["rationale"].strip() != "",
        f"{where}: rationale is required — the artifact is the audit trail for a call "
        "that cannot be recomputed",
    )
    _require(
        "\n" not in raw["rationale"].strip(),
        f"{where}: rationale must be one line",
    )
    return Adjudication(
        case_id=raw["case_id"], backend=raw["backend"], arm=raw["arm"],
        arm_role=raw["arm_role"], rep=raw["rep"], finding_index=raw["finding_index"],
        decision=raw["decision"], rationale=raw["rationale"].strip(),
    )


def findings_by_unit(rows: list[dict]) -> dict[tuple, list[Finding]]:
    """Parse each unit's review once — and only when it strictly validated.

    A run whose output was not `REVIEW_SCHEMA`-compliant contributes **no findings**, which
    is the rule already registered under *Veto arithmetic*: it lands a zero in its arm's
    denominator rather than dropping out of it. The runtime parser is deliberately lenient
    — an invented severity, a missing `why`, a `why` that is not even a string all survive
    it — because a usable review should not be thrown away over a stray key. Scoring recall
    or a false positive off a finding the schema rejects would let a malformed review earn
    credit a compliant one had to argue for, and it would make the reported numbers
    disagree with the rule the README committed to.
    """
    return {
        unit_key(row): (
            parse_backend_output(
                row["raw"], row["backend"], row["requested_model"]
            ).findings
            if row["outcome"] == VALID else []
        )
        for row in rows
    }


def results_digest(path: Path) -> str:
    """Fingerprint of the exact bytes the calls were read from."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as e:
        raise AdjudicationError(f"{path}: could not read the results file: {e}") from e


def corpus_digest(cases: dict[str, Case], case_ids: set[str]) -> str:
    """Fingerprint of everything about the corpus a verdict reads.

    A manifest edited between drafting the calls and scoring them re-decides things the
    calls were made under and cannot be re-made for: which class a case counts toward,
    which lines rule 2 accepts, which encoding of a mutation represents it, what
    `defect.expected` a human read the finding against. That is the same post-hoc move as
    the twin exploit, made with an editor rather than a decision.

    **`case_ids` must be the whole loaded corpus, never just the cases the results
    reference.** Narrowing it looks harmless and is not: `compute_recall` elects each
    mutation's representative over every manifest it loaded, so a manifest no result row
    mentions still decides which scored case represents its mutation. Adding one that
    shares a scored case's patch under a lower id demotes that case to a twin and drops it
    out of its class — under a results-scoped digest, without moving the digest at all.
    The scope of the fingerprint has to match the scope of what reads the corpus.

    Length-prefixed so no concatenation of two fields can collide with another.
    """
    digest = hashlib.sha256()
    for case_id in sorted(case_ids):
        case = cases[case_id]
        defect = case.defect
        fields = [
            case.id, case.mode, case.source, case.repo_commit,
            case.diff or "", case.path or "",
            "" if defect is None else defect.defect_class,
            "" if defect is None else defect.file,
            "" if defect is None else f"{defect.span[0]}-{defect.span[1]}",
            "" if defect is None else defect.expected,
        ]
        for value in fields:
            digest.update(f"{len(value)}:{value}\n".encode())
        if case.source == "mutant" and case.diff:
            patch = case.root / case.diff
            try:
                data = patch.read_bytes()
            except OSError:
                # Distinguishable from an empty patch: a corpus missing its patches is a
                # different corpus, not one whose mutations happen to be no-ops.
                digest.update(b"patch:absent\n")
            else:
                digest.update(f"patch:{len(data)}:".encode())
                digest.update(data)
    return digest.hexdigest()


#: What to do about a digest mismatch. Not "re-draft with --pending": drafting verifies the
#: same digests against the same artifact and refuses identically, so naming it alone sends
#: the reader in a circle.
_REBIND = (
    "Either put the input back the way it was, or — if the change is the one you want — "
    "move the stale artifact aside and re-draft from scratch with --pending. Re-drafting "
    "does not carry recorded calls forward, so the moved file (and git) is the only copy "
    "of them."
)


def verify_digests(
    adjudications: AdjudicationFile, cases: dict[str, Case], case_ids: set[str],
    results_path: Path,
) -> None:
    """Refuse calls that were made against different inputs than the ones in hand."""
    actual = results_digest(results_path)
    _require(
        adjudications.results_digest == actual,
        f"{adjudications.path} was drafted against a different results file: it records "
        f"{adjudications.results_digest[:12]}, {results_path} hashes to {actual[:12]}. "
        "Every decision is keyed by a finding's position in a run's findings array, so a "
        "results file that was edited or re-ordered rebinds all of them to different "
        "findings. " + _REBIND,
    )
    actual = corpus_digest(cases, case_ids)
    _require(
        adjudications.corpus_digest == actual,
        f"{adjudications.path} was drafted against a different corpus: it records "
        f"{adjudications.corpus_digest[:12]}, the manifests in {CASES_DIR.name}/ hash to "
        f"{actual[:12]}. A manifest edited after the calls were made re-decides which class "
        "a case counts toward, which lines rule 2 accepts and which encoding of a mutation "
        "represents it — the same post-hoc move as any other, made with an editor. "
        + _REBIND,
    )


def matches_location(finding: Finding, case: Case, repo: Path) -> bool:
    """Rules 1 and 2 of the scoring rule: cited file and line land on the defect.

    A finding with a null line makes no line claim, so rule 2 is delegated to the human
    along with rule 3 — the README's "the evidence it quotes lies within that span". Only
    rule 1 is decided here for those.
    """
    assert case.defect is not None
    if not isinstance(finding.file, str) or not finding.file:
        return False
    if normalize_cited_path(finding.file, repo) != case.defect.file:
        return False
    if finding.line is None:
        return True
    start, end = case.defect.span
    return start <= finding.line <= end


def pending_findings(
    rows: list[dict], cases: dict[str, Case], parsed: dict[tuple, list[Finding]],
    repo: Path, skip_cases: frozenset[str] = frozenset(),
) -> list[PendingFinding]:
    """Every finding in `rows` that a decision rule cannot be computed without.

    Two sets, one per rule. On a clean control, the veto reads CRITICAL and HIGH findings
    and nothing else, and each is noise or a real bug — a call only a human makes. On a
    defect case, class recall reads the findings that already satisfy rules 1 and 2, where
    what is left is rule 3.

    Derived mechanically from the results, so `--pending` can emit the work list without
    any of it being a judgement about what is worth adjudicating.
    """
    out: list[PendingFinding] = []
    for row in sorted(rows, key=lambda r: (r["case_id"], r["backend"], r["arm_role"], r["rep"])):
        case = cases[row["case_id"]]
        if case.id in skip_cases:
            continue
        for index, finding in enumerate(parsed[unit_key(row)]):
            severity = severity_bucket(finding.severity)
            if case.is_control:
                if severity not in VETO_SEVERITIES:
                    continue
                reason = "clean-control critical/high: false-positive or case-invalidated"
            else:
                if not matches_location(finding, case, repo):
                    continue
                reason = "sits on the defect's own lines: does the why describe the defect?"
            out.append(PendingFinding(
                case_id=row["case_id"], backend=row["backend"], arm=row["arm"],
                arm_role=row["arm_role"], rep=row["rep"], finding_index=index,
                reason=reason, severity=severity, title=finding.title,
            ))
    return out


def validate_against_results(
    adjudications: tuple[Adjudication, ...], rows: list[dict], cases: dict[str, Case],
    parsed: dict[tuple, list[Finding]], sweep_id: str, repo: Path,
) -> None:
    """Refuse an artifact that cannot be joined to the results, or that breaks a rule.

    Every problem is collected before raising: an adjudicator fixing one typo at a time
    across a hundred decisions is a bad enough loop to be worth the extra pass.
    """
    by_unit = {unit_key(row): row for row in rows}
    problems: list[str] = []
    for adj in adjudications:
        where = f"{adj.case_id} {adj.backend} {adj.arm_role}({adj.arm}) rep{adj.rep} " \
                f"#{adj.finding_index}"
        key = (sweep_id, adj.case_id, adj.backend, adj.arm, adj.arm_role, adj.rep)
        row = by_unit.get(key)
        if row is None:
            problems.append(f"{where}: no such run in the results file")
            continue
        if row["outcome"] != VALID:
            problems.append(
                f"{where}: that run's output did not validate ({row['outcome']}), so it "
                "contributes no findings and nothing on it can be adjudicated"
            )
            continue
        findings = parsed[key]
        if adj.finding_index >= len(findings):
            problems.append(
                f"{where}: that run's review has {len(findings)} finding(s), so there is "
                f"no #{adj.finding_index}"
            )
            continue
        case = cases[adj.case_id]
        problems.extend(_rule_problems(adj, case, findings[adj.finding_index], where, repo))
    if problems:
        raise AdjudicationError(
            "the adjudication artifact does not match the results:\n  "
            + "\n  ".join(problems)
        )


def _rule_problems(
    adj: Adjudication, case: Case, finding: Finding, where: str, repo: Path,
) -> list[str]:
    """Decisions the pre-registered rules do not allow on this finding.

    These are refusals, not warnings. A `matches-defect` on a finding that fails rule 1 or
    2 would give recall credit the scoring rule does not allow, and a `real-unrelated` on
    a control's CRITICAL finding would let a genuinely real bug sit in the corpus as
    neither noise nor an invalidation — both are ways of deciding a number after the fact,
    which is the failure this whole harness is built to prevent.
    """
    problems: list[str] = []
    if case.is_control:
        if adj.decision == MATCHES_DEFECT:
            problems.append(f"{where}: {MATCHES_DEFECT} on a clean control, which has no defect")
        if adj.decision == REAL_UNRELATED and severity_bucket(finding.severity) in VETO_SEVERITIES:
            problems.append(
                f"{where}: a real CRITICAL/HIGH finding on a clean control means the case is "
                f"not a control — record {CASE_INVALIDATED}, not {REAL_UNRELATED}"
            )
    else:
        if adj.decision == FALSE_POSITIVE:
            problems.append(
                f"{where}: {FALSE_POSITIVE} on a defect case. Unrelated-but-real findings "
                f"there are ignored, not debited — record {REAL_UNRELATED}"
            )
        # Invalidating a defect case is a claim about the seeded defect, so it is made on a
        # finding about the seeded defect. Without the check the removal floats free of any
        # evidence, and removing a case is the single largest thing one call can do to the
        # arithmetic.
        if adj.decision in (MATCHES_DEFECT, CASE_INVALIDATED) and not matches_location(
            finding, case, repo,
        ):
            assert case.defect is not None
            cited = finding.file if isinstance(finding.file, str) else repr(finding.file)
            problems.append(
                f"{where}: {adj.decision} but the finding fails rule 1 or 2 — it cites "
                f"{cited}:{finding.line}, and the defect is {case.defect.file} lines "
                f"{case.defect.span[0]}-{case.defect.span[1]}"
            )
    return problems


# --- the pre-registered arithmetic ---------------------------------------------------------


@dataclass
class CaseRecall:
    case_id: str
    defect_class: str
    #: Which seeded mutation this case is an expression of; twins share one.
    mutation: str
    #: A second encoding of a mutation another case already carries. Excluded from
    #: certification arithmetic and reported as mode-sensitivity information.
    is_twin: bool
    control_reps: int
    treatment_reps: int
    control_hits: int
    treatment_hits: int
    control_found: bool
    treatment_found: bool


@dataclass
class ClassRecall:
    defect_class: str
    #: Independent cases, after twin dedup and case invalidation.
    cases: int
    control_found: int
    treatment_found: int
    #: Complete repetitions on the class's shallowest scored case and arm. The majority
    #: rule is only the majority rule at depth: at n=1 one run carries a case.
    min_reps: int
    #: A class below either bar — case count or protocol depth — is reported and cannot
    #: certify, whatever its numbers say.
    certifying: bool
    absolute_gain: int
    #: None when control recall is zero: a relative improvement on zero is undefined
    #: rather than infinite, and the criterion falls back to the absolute condition.
    relative_gain: Fraction | None
    criterion_met: bool
    per_case: list[CaseRecall] = field(default_factory=list)


@dataclass
class CaseVeto:
    case_id: str
    reps: int
    control_total: int
    treatment_total: int
    control_mean: Fraction
    treatment_mean: Fraction
    delta: Fraction
    within_bound: bool


@dataclass
class VetoResult:
    per_case: list[CaseVeto]
    control_runs: int
    treatment_runs: int
    control_total: int
    treatment_total: int
    #: None, never 0, when no clean control run scored — a zero would read as "measured,
    #: and it was zero", which is the one thing an empty veto must not say.
    control_mean: Fraction | None
    treatment_mean: Fraction | None
    #: Complete repetitions on the shallowest clean-control case, and whether that reaches
    #: the protocol depth. It gates certifying, never blocking: a shallow sweep that trips
    #: the veto has still shown harm, and refusing to act on that would be the wrong
    #: direction to fail in.
    min_case_reps: int
    at_protocol_depth: bool
    per_case_ok: bool
    aggregate_ok: bool
    #: The same aggregate over the arm's pre-registered control subset. Empty tuple and a
    #: `True` when the arm registered none, so an unrestricted arm reads as it always did.
    subset_cases: tuple[str, ...]
    subset_control_runs: int
    subset_treatment_runs: int
    subset_control_total: int
    subset_treatment_total: int
    subset_control_mean: Fraction | None
    subset_treatment_mean: Fraction | None
    subset_ok: bool
    holds: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class BackendCertification:
    backend: str
    control_arm: str
    treatment_arm: str
    veto: VetoResult
    classes: list[ClassRecall]
    twins: list[CaseRecall]
    verdict: str
    reasons: list[str] = field(default_factory=list)


# Not frozen, unlike its neighbours: a frozen dataclass advertises a __hash__ this one
# cannot honour, because `by_role` is a dict.
@dataclass
class CrossArmInconsistency:
    title: str
    #: Arm role -> the distinct decisions recorded against this exact finding text there.
    by_role: dict[str, list[str]]


def _text(value: object) -> str:
    """A finding's prose as a comparable string.

    `why` reaches here uncoerced — the runtime parser passes it through whatever the model
    put in it. Strict validation gates the findings this sees, so an object should never
    arrive; comparing one must still not take the report down if it does.
    """
    return value.strip() if isinstance(value, str) else ""


def cross_arm_inconsistencies(
    adjudications: tuple[Adjudication, ...], rows: list[dict],
    parsed: dict[tuple, list[Finding]], sweep_id: str,
) -> list[CrossArmInconsistency]:
    """Identical finding text called one thing in one arm and another in the other.

    Never blocking, and not necessarily wrong: the same sentence can be right about one
    diff and wrong about another, and a twenty-case corpus produces honest near-duplicates.
    But an adjudicator reading the control's finding as noise and the treatment's
    word-for-word identical finding as real is the shape a thumb on the scale makes, and
    the arm role is on screen while the call is made. Printed, so it is at least seen.
    """
    by_unit = {unit_key(row): row for row in rows}
    by_text: dict[tuple[str, str], dict[str, set[str]]] = {}
    for adj in adjudications:
        key = (sweep_id, adj.case_id, adj.backend, adj.arm, adj.arm_role, adj.rep)
        if key not in by_unit:
            continue
        finding = parsed[key][adj.finding_index]
        text = (_text(finding.title), _text(finding.why))
        by_text.setdefault(text, {}).setdefault(adj.arm_role, set()).add(adj.decision)
    return [
        CrossArmInconsistency(
            title=title, by_role={role: sorted(d) for role, d in sorted(roles.items())},
        )
        for (title, _why), roles in sorted(by_text.items())
        if len(roles) > 1 and len({frozenset(d) for d in roles.values()}) > 1
    ]


@dataclass
class Certification:
    sweep_id: str
    results_path: str
    adjudications_path: str
    decisions_total: int
    decisions_by_type: dict[str, int]
    #: Decisions on repetitions that did not survive the complete-pair rule. Recorded work
    #: that no number reads; reported so a shrinking corpus is visible.
    decisions_unused: int
    #: The cases an adjudicator called invalid...
    invalidated_cases: list[str]
    #: ...and everything that went with them: an invalidation removes a whole mutation, so
    #: a `code`-mode twin of an invalidated representative is here but not above.
    removed_cases: list[str]
    cross_arm: list[CrossArmInconsistency]
    incomplete_pairs: list[IncompletePair]
    backends: list[BackendCertification]
    verdict: str
    reasons: list[str] = field(default_factory=list)


def mutation_of(case: Case) -> str:
    """What makes two cases one seeded defect rather than two of them.

    A `diff`-mode and a `code`-mode re-expression of one mutant share a patch file — same
    bug, same lines, same reason a reviewer would or would not see it — so the patch path
    is the mutation's identity. Anything else (a seeded plan) is its own.
    """
    if case.source == "mutant" and case.diff:
        return case.diff
    return f"case:{case.id}"


def certifying_representative(group: list[Case]) -> Case:
    """The one case of a mutation that certification arithmetic counts.

    The `diff`-mode expression, which is the shape the corpus is built around; lowest case
    id breaks any tie. Deterministic and independent of the results, so which twin counts
    can never be a choice made after seeing which one scored better.
    """
    diff_mode = sorted((c for c in group if c.mode == "diff"), key=lambda c: c.id)
    return diff_mode[0] if diff_mode else sorted(group, key=lambda c: c.id)[0]


def _grouped(cases: dict[str, Case]) -> dict[str, list[Case]]:
    """Every case of the corpus, bucketed by the mutation it is an expression of."""
    groups: dict[str, list[Case]] = {}
    for case in cases.values():
        groups.setdefault(mutation_of(case), []).append(case)
    return groups


def removed_cases(cases: dict[str, Case], invalidated: frozenset[str]) -> frozenset[str]:
    """Every case an invalidation takes out: the ones named, and their twins with them.

    Invalidation lands on a **mutation**, not on one encoding of it. A `diff`-mode case and
    its `code`-mode twin are one seeded defect, so if that defect's validity is disputed,
    both expressions of it go.

    Removing only the named case would be worse than incomplete — it would be exploitable.
    The named case is usually the `diff`-mode representative; dropping it alone leaves the
    twin as the sole member of its mutation group, where `certifying_representative` elects
    it. The class keeps its case count, and the numbers of the very encoding twin dedup
    exists to hold out are swapped into certification arithmetic by an adjudication call.
    """
    return frozenset(
        case.id
        for group in _grouped(cases).values()
        if any(case.id in invalidated for case in group)
        for case in group
    )


def _found(hits: int, reps: int) -> bool:
    """Majority rule: a defect is FOUND by an arm iff it was matched in more than half of
    that arm's scored repetitions for the case.

    Strictly more than half, so an even number of repetitions splitting evenly — 2 of 4
    after a failed repetition — is NOT found. A tie is not reliable detection, and the
    burden of proof sits on the claim that the reviewer finds the defect.
    """
    return hits * 2 > reps


def _mean(total: int, count: int) -> Fraction | None:
    return Fraction(total, count) if count else None


def compute_recall(
    rows: list[dict], cases: dict[str, Case], parsed: dict[tuple, list[Finding]],
    matched: set[tuple], removed: frozenset[str],
) -> tuple[list[ClassRecall], list[CaseRecall]]:
    """Per-class recall for one backend, plus the twins held out of it.

    `removed` is already closed over mutations (see `removed_cases`), so a mutation is
    either wholly present or wholly gone here. Electing the representative from a group an
    invalidation had partly emptied is what would let a twin be promoted into the class.
    """
    defect_cases = [
        c for c in cases.values() if c.defect is not None and c.id not in removed
    ]
    by_mutation: dict[str, list[Case]] = {}
    for case in defect_cases:
        by_mutation.setdefault(mutation_of(case), []).append(case)
    representatives = {certifying_representative(g).id for g in by_mutation.values()}

    scored_ids = {row["case_id"] for row in rows}
    per_case: dict[str, CaseRecall] = {}
    for case in defect_cases:
        if case.id not in scored_ids:
            continue
        assert case.defect is not None
        counts = {role: [0, 0] for role in (CONTROL, TREATMENT)}
        for row in rows:
            if row["case_id"] != case.id or row["arm_role"] not in counts:
                continue
            tally = counts[row["arm_role"]]
            tally[0] += 1
            key = unit_key(row)
            hit = any(
                (row["case_id"], row["backend"], row["arm"], row["arm_role"], row["rep"], i)
                in matched
                for i in range(len(parsed[key]))
            )
            tally[1] += 1 if hit else 0
        control_reps, control_hits = counts[CONTROL]
        treatment_reps, treatment_hits = counts[TREATMENT]
        per_case[case.id] = CaseRecall(
            case_id=case.id, defect_class=case.defect.defect_class,
            mutation=mutation_of(case), is_twin=case.id not in representatives,
            control_reps=control_reps, treatment_reps=treatment_reps,
            control_hits=control_hits, treatment_hits=treatment_hits,
            control_found=_found(control_hits, control_reps),
            treatment_found=_found(treatment_hits, treatment_reps),
        )

    certifying_cases = [c for c in per_case.values() if not c.is_twin]
    by_class: dict[str, list[CaseRecall]] = {}
    for entry in certifying_cases:
        by_class.setdefault(entry.defect_class, []).append(entry)

    classes: list[ClassRecall] = []
    for defect_class, entries in sorted(by_class.items()):
        entries.sort(key=lambda c: c.case_id)
        control_found = sum(1 for c in entries if c.control_found)
        treatment_found = sum(1 for c in entries if c.treatment_found)
        classes.append(_class_recall(defect_class, entries, control_found, treatment_found))
    twins = sorted(
        (c for c in per_case.values() if c.is_twin), key=lambda c: c.case_id
    )
    return classes, twins


def _class_recall(
    defect_class: str, entries: list[CaseRecall], control_found: int, treatment_found: int,
) -> ClassRecall:
    absolute = treatment_found - control_found
    # A relative improvement on a zero baseline is undefined, not infinite, so the class
    # is judged on the absolute condition alone there.
    relative = Fraction(absolute, control_found) if control_found else None
    min_reps = min(
        (min(c.control_reps, c.treatment_reps) for c in entries), default=0,
    )
    certifying = (
        len(entries) >= CERTIFYING_CLASS_MIN_CASES and min_reps >= PROTOCOL_MIN_REPS
    )
    met = absolute >= CRITERION_ABSOLUTE_GAIN and (
        relative is None or relative >= CRITERION_RELATIVE_GAIN
    )
    return ClassRecall(
        defect_class=defect_class, cases=len(entries), control_found=control_found,
        treatment_found=treatment_found, min_reps=min_reps, certifying=certifying,
        absolute_gain=absolute, relative_gain=relative, criterion_met=met, per_case=entries,
    )


def compute_veto(
    rows: list[dict], cases: dict[str, Case], parsed: dict[tuple, list[Finding]],
    false_positives: set[tuple], removed: frozenset[str], rules: ArmRules = NO_ARM_RULES,
) -> VetoResult:
    """The veto for one backend, from adjudicated false positives rather than raw counts.

    Per clean-control case, the mean CRITICAL+HIGH adjudicated-false-positive count per
    run per arm, over complete pairs only — the same runs tier 1 scores, so the two
    reports cannot disagree about which work happened.

    An arm that registered a control subset is held to the aggregate twice, over every
    clean control and over that subset: pooling controls the arm's text cannot reach lets
    them carry the ones it can, and a change ships in the form that only ever runs on the
    subset.
    """
    totals = {role: [0, 0] for role in (CONTROL, TREATMENT)}
    subset_totals = {role: [0, 0] for role in (CONTROL, TREATMENT)}
    by_case: dict[str, dict[str, list[int]]] = {}
    for row in rows:
        case = cases[row["case_id"]]
        if not case.is_control or case.id in removed or row["arm_role"] not in totals:
            continue
        count = sum(
            1 for i, finding in enumerate(parsed[unit_key(row)])
            if severity_bucket(finding.severity) in VETO_SEVERITIES
            and (row["case_id"], row["backend"], row["arm"], row["arm_role"], row["rep"], i)
            in false_positives
        )
        totals[row["arm_role"]][0] += 1
        totals[row["arm_role"]][1] += count
        if case.id in rules.control_subset:
            subset_totals[row["arm_role"]][0] += 1
            subset_totals[row["arm_role"]][1] += count
        tally = by_case.setdefault(case.id, {role: [0, 0] for role in (CONTROL, TREATMENT)})
        tally[row["arm_role"]][0] += 1
        tally[row["arm_role"]][1] += count

    per_case: list[CaseVeto] = []
    for case_id, tally in sorted(by_case.items()):
        # Both arms are present and equally deep by construction: a repetition only
        # reaches here when both of its runs produced a review.
        control_runs, control_total = tally[CONTROL]
        treatment_runs, treatment_total = tally[TREATMENT]
        control_mean = Fraction(control_total, control_runs)
        treatment_mean = Fraction(treatment_total, treatment_runs)
        delta = treatment_mean - control_mean
        per_case.append(CaseVeto(
            case_id=case_id, reps=control_runs, control_total=control_total,
            treatment_total=treatment_total, control_mean=control_mean,
            treatment_mean=treatment_mean, delta=delta,
            within_bound=delta <= VETO_PER_CASE_SLACK,
        ))

    control_runs, control_total = totals[CONTROL]
    treatment_runs, treatment_total = totals[TREATMENT]
    control_mean = _mean(control_total, control_runs)
    treatment_mean = _mean(treatment_total, treatment_runs)
    subset_control_runs, subset_control_total = subset_totals[CONTROL]
    subset_treatment_runs, subset_treatment_total = subset_totals[TREATMENT]
    subset_control_mean = _mean(subset_control_total, subset_control_runs)
    subset_treatment_mean = _mean(subset_treatment_total, subset_treatment_runs)
    per_case_ok = all(c.within_bound for c in per_case)
    aggregate_ok = (
        control_mean is not None and treatment_mean is not None
        and treatment_mean <= control_mean
    )
    # An arm with no registered subset is unrestricted, not unmeasured. One that has a
    # subset and scored nothing in it fails for the reason an unmeasured veto fails: the
    # condition was never shown to hold.
    subset_ok = not rules.control_subset or (
        subset_control_mean is not None and subset_treatment_mean is not None
        and subset_treatment_mean <= subset_control_mean
    )
    reasons: list[str] = []
    if control_mean is None or treatment_mean is None:
        # The veto is a "must hold" condition, so an unmeasured veto is a failed one. A
        # sweep whose clean controls all dropped out has not demonstrated anything about
        # false positives, and treating silence as a pass is how a veto stops binding.
        reasons.append("no clean-control repetition scored, so the veto cannot be shown to hold")
    for case in per_case:
        if not case.within_bound:
            reasons.append(
                f"{case.case_id}: treatment {_num(case.treatment_mean)} exceeds control "
                f"{_num(case.control_mean)} by more than {_num(VETO_PER_CASE_SLACK)} "
                f"adjudicated false-positive critical+high per run"
            )
    if control_mean is not None and treatment_mean is not None and not aggregate_ok:
        reasons.append(
            f"in aggregate over clean controls, treatment {_num(treatment_mean)} exceeds "
            f"control {_num(control_mean)} adjudicated false-positive critical+high per run"
        )
    subset_label = ", ".join(sorted(rules.control_subset))
    if rules.control_subset and not subset_ok:
        reasons.append(
            f"over the controls this arm's text reaches ({subset_label}), "
            + (
                "no repetition scored, so the restricted aggregate cannot be shown to hold"
                if subset_control_mean is None or subset_treatment_mean is None else
                f"treatment {_num(subset_treatment_mean)} exceeds control "
                f"{_num(subset_control_mean)} adjudicated false-positive critical+high per run"
            )
        )
    min_case_reps = min((c.reps for c in per_case), default=0)
    return VetoResult(
        per_case=per_case, control_runs=control_runs, treatment_runs=treatment_runs,
        control_total=control_total, treatment_total=treatment_total,
        control_mean=control_mean, treatment_mean=treatment_mean,
        min_case_reps=min_case_reps,
        at_protocol_depth=bool(per_case) and min_case_reps >= PROTOCOL_MIN_REPS,
        per_case_ok=per_case_ok, aggregate_ok=aggregate_ok,
        subset_cases=tuple(sorted(rules.control_subset)),
        subset_control_runs=subset_control_runs,
        subset_treatment_runs=subset_treatment_runs,
        subset_control_total=subset_control_total,
        subset_treatment_total=subset_treatment_total,
        subset_control_mean=subset_control_mean,
        subset_treatment_mean=subset_treatment_mean,
        subset_ok=subset_ok,
        holds=per_case_ok and aggregate_ok and subset_ok, reasons=reasons,
    )


def _arm_of(rows: list[dict], role: str) -> str:
    names = sorted({row["arm"] for row in rows if row["arm_role"] == role})
    if len(names) != 1:
        raise AdjudicationError(
            f"expected exactly one {role} arm in the results, found {len(names)}"
            + (f": {', '.join(names)}" if names else "")
        )
    return names[0]


def expected_backends(header: dict, rows: list[dict]) -> set[str]:
    """Every backend this sweep was supposed to produce a comparison for.

    Taken from the header's declared specs as well as the rows, because a backend whose
    every run failed still has rows, and a backend that never started has only the header.
    Both have to be visible: certification is a conjunction over backends, and a backend
    that silently drops out of the conjunction weakens it to whichever ones survived.
    """
    specs = header.get("backend_specs")
    declared = set(specs) if isinstance(specs, dict) else set()
    return declared | {row["backend"] for row in rows}


def check_header(header: dict, rows: list[dict]) -> None:
    """Refuse rows that disagree with the header that says what produced them.

    The header names each role's arm and its content hash. A file whose rows carry a
    different arm under a role is not the sweep the header describes, and the arm hash is
    the only thing tying a row to the exact prompt bytes that produced it.
    """
    arms = header.get("arms")
    if not isinstance(arms, dict):
        return
    for role, spec in sorted(arms.items()):
        if not isinstance(spec, dict):
            continue
        observed = {(r["arm"], r.get("arm_hash")) for r in rows if r["arm_role"] == role}
        declared = (spec.get("name"), spec.get("hash"))
        _require(
            not observed or observed == {declared},
            f"the results header declares the {role} arm as {declared[0]}"
            f"@{str(declared[1])[:12]}, but its rows carry "
            f"{', '.join(sorted(f'{n}@{str(h)[:12]}' for n, h in observed))}. The rows and "
            "the header describe different sweeps, so neither can be trusted about the "
            "other.",
        )


def compute(
    rows: list[dict], cases: dict[str, Case], adjudications: AdjudicationFile, repo: Path,
    results_path: Path, header: dict,
) -> Certification:
    """Score a sweep. Raises rather than reporting when the inputs cannot support a verdict."""
    finals = final_attempts(rows)
    check_header(header, finals)
    verify_digests(adjudications, cases, set(cases), results_path)
    parsed = findings_by_unit(finals)
    validate_against_results(
        adjudications.decisions, finals, cases, parsed, adjudications.sweep_id, repo,
    )
    complete, incomplete = split_complete_pairs(finals)

    invalidated = frozenset(
        adj.case_id for adj in adjudications.decisions if adj.decision == CASE_INVALIDATED
    )
    removed = removed_cases(cases, invalidated)
    recorded = {adj.key for adj in adjudications.decisions}
    missing = [
        item for item in pending_findings(complete, cases, parsed, repo, skip_cases=removed)
        if item.key not in recorded
    ]
    if missing:
        raise IncompleteAdjudication(missing)

    matched = {adj.key for adj in adjudications.decisions if adj.decision == MATCHES_DEFECT}
    false_positives = {
        adj.key for adj in adjudications.decisions if adj.decision == FALSE_POSITIVE
    }
    scored_keys = {unit_key(row) for row in complete}
    unused = sum(
        1 for adj in adjudications.decisions
        if (adjudications.sweep_id, adj.case_id, adj.backend, adj.arm, adj.arm_role, adj.rep)
        not in scored_keys
    )

    by_backend: dict[str, list[dict]] = {}
    for row in complete:
        by_backend.setdefault(row["backend"], []).append(row)

    backends: list[BackendCertification] = []
    for backend, backend_rows in sorted(by_backend.items()):
        treatment_arm = _arm_of(backend_rows, TREATMENT)
        rules = ARM_RULES.get(treatment_arm, NO_ARM_RULES)
        veto = compute_veto(backend_rows, cases, parsed, false_positives, removed, rules)
        classes, twins = compute_recall(backend_rows, cases, parsed, matched, removed)
        backends.append(_backend_verdict(
            backend=backend,
            control_arm=_arm_of(backend_rows, CONTROL),
            treatment_arm=treatment_arm,
            veto=veto, classes=classes, twins=twins, rules=rules,
        ))
    silent = sorted(expected_backends(header, finals) - set(by_backend))
    if silent:
        # Not a NOT CERTIFIED: a backend with nothing scored has not failed the criterion,
        # it has failed to be measured. Letting it fall out of the report would quietly
        # reduce "every backend must certify" to "every backend that finished must".
        raise AdjudicationError(
            f"no complete repetition survived for {', '.join(silent)}. Certification is a "
            "conjunction over the backends the sweep ran, so a backend that scored nothing "
            "cannot be dropped from it — re-run the sweep for it."
        )

    by_type = {d: sum(1 for a in adjudications.decisions if a.decision == d) for d in DECISIONS}
    verdict, reasons = _sweep_verdict(backends)
    return Certification(
        sweep_id=adjudications.sweep_id,
        results_path=str(results_path), adjudications_path=str(adjudications.path),
        decisions_total=len(adjudications.decisions), decisions_by_type=by_type,
        decisions_unused=unused, invalidated_cases=sorted(invalidated),
        removed_cases=sorted(removed),
        cross_arm=cross_arm_inconsistencies(
            adjudications.decisions, finals, parsed, adjudications.sweep_id,
        ),
        incomplete_pairs=incomplete, backends=backends,
        verdict=verdict, reasons=reasons,
    )


def _backend_verdict(
    backend: str, control_arm: str, treatment_arm: str, veto: VetoResult,
    classes: list[ClassRecall], twins: list[CaseRecall], rules: ArmRules = NO_ARM_RULES,
) -> BackendCertification:
    certifying = [c for c in classes if c.certifying]
    # A designated class narrows what may certify; every other class is still computed and
    # printed, which is the difference between reported and decided.
    eligible = [
        c for c in certifying
        if rules.certifying_class is None or c.defect_class == rules.certifying_class
    ]
    winners = [c for c in eligible if c.criterion_met]
    if not veto.holds:
        verdict, reasons = VETOED, list(veto.reasons)
    elif winners and veto.at_protocol_depth:
        verdict = CERTIFIED
        reasons = [
            f"{c.defect_class}: {c.control_found} -> {c.treatment_found} of {c.cases} cases "
            f"(+{c.absolute_gain}, {_pct(c.relative_gain)})"
            for c in winners
        ]
    else:
        verdict = NOT_CERTIFIED
        reasons = _not_certified_reasons(classes, certifying, winners, veto, rules)
    return BackendCertification(
        backend=backend, control_arm=control_arm, treatment_arm=treatment_arm,
        veto=veto, classes=classes, twins=twins, verdict=verdict, reasons=reasons,
    )


def _not_certified_reasons(
    classes: list[ClassRecall], certifying: list[ClassRecall], winners: list[ClassRecall],
    veto: VetoResult, rules: ArmRules = NO_ARM_RULES,
) -> list[str]:
    """Name what actually stood between this sweep and a certification."""
    if winners and not veto.at_protocol_depth:
        met = ", ".join(c.defect_class for c in winners)
        depth = (
            f"the shallowest clean control scored {veto.min_case_reps} complete "
            f"repetition(s)" if veto.per_case else "no clean-control case scored"
        )
        return [
            f"the success criterion is met on {met}, but {depth} and certification needs "
            f"{PROTOCOL_MIN_REPS} — the veto is measured too thinly to certify against"
        ]
    reasons: list[str] = []
    if rules.certifying_class is not None:
        # Said out loud, because a gain that met the criterion and still did not certify is
        # exactly the outcome a reader would otherwise suspect of being a scorer bug.
        blocked = [
            c for c in certifying
            if c.defect_class != rules.certifying_class and c.criterion_met
        ]
        if blocked:
            reasons.append(
                f"{', '.join(c.defect_class for c in blocked)} met the success criterion, "
                f"but {rules.certifying_class} is this arm's designated certifying class, "
                "so that gain is reported and cannot certify"
            )
        certifying = [c for c in certifying if c.defect_class == rules.certifying_class]
    if not certifying:
        if not classes:
            return reasons + ["no defect case scored, so no class can certify"]
        return reasons + [f"{c.defect_class}: {_grade_text(c)}" for c in classes]
    return reasons + [f"{c.defect_class}: {_criterion_miss(c)}" for c in certifying]


def _criterion_miss(entry: ClassRecall) -> str:
    """Which half of the criterion this class actually missed, and only that half.

    Citing the relative threshold on a zero-baseline class would report a bar that was
    waived as one that was failed — the criterion says a relative improvement on zero is
    undefined, not zero.
    """
    missed = []
    if entry.absolute_gain < CRITERION_ABSOLUTE_GAIN:
        missed.append(f">={CRITERION_ABSOLUTE_GAIN} absolute")
    if entry.relative_gain is not None and entry.relative_gain < CRITERION_RELATIVE_GAIN:
        missed.append(f">={_pct(CRITERION_RELATIVE_GAIN)} relative")
    gain = f"+{entry.absolute_gain} defect(s)" + (
        " on a zero baseline, where the relative threshold is waived"
        if entry.relative_gain is None else f" ({_pct(entry.relative_gain)})"
    )
    return f"{gain} misses {' and '.join(missed)}"


def _sweep_verdict(backends: list[BackendCertification]) -> tuple[str, list[str]]:
    """One verdict over every backend the sweep ran.

    A veto anywhere vetoes the sweep, and certification needs every backend to certify: a
    prompt change ships to all of them at once, so one backend improving while another
    gets noisier is not an improvement to `rr`.
    """
    vetoed = [b for b in backends if b.verdict == VETOED]
    if vetoed:
        return VETOED, [f"{b.backend}: {r}" for b in vetoed for r in b.reasons]
    if all(b.verdict == CERTIFIED for b in backends):
        return CERTIFIED, [f"{b.backend}: {r}" for b in backends for r in b.reasons]
    return NOT_CERTIFIED, [
        f"{b.backend}: {r}" for b in backends if b.verdict != CERTIFIED for r in b.reasons
    ]


# --- reporting -----------------------------------------------------------------------------


def _num(value: Fraction | None) -> str:
    return "n/a" if value is None else f"{float(value):.3f}"


def _pct(value: Fraction | None) -> str:
    return "undefined baseline" if value is None else f"{float(value) * 100:+.1f}%"


def _float(value: Fraction | None) -> float | None:
    return None if value is None else round(float(value), 6)


def print_report(cert: Certification) -> None:
    print(f"\nsweep {cert.sweep_id}")
    print(f"  results        {cert.results_path}")
    print(f"  adjudications  {cert.adjudications_path}")
    counts = ", ".join(f"{d} {cert.decisions_by_type[d]}" for d in DECISIONS)
    print(f"  decisions      {cert.decisions_total} ({counts})")
    if cert.decisions_unused:
        print(f"                 {cert.decisions_unused} on repetitions that did not score")
    if cert.removed_cases:
        print("  cases removed  " + ", ".join(cert.removed_cases)
              + " (from both arms, every metric)")
        with_them = [c for c in cert.removed_cases if c not in cert.invalidated_cases]
        print("                 invalidated: " + ", ".join(cert.invalidated_cases)
              + (f"; same mutation: {', '.join(with_them)}" if with_them else ""))
    for item in cert.cross_arm:
        detail = "; ".join(f"{role} {', '.join(d)}" for role, d in item.by_role.items())
        print(f"  WARNING        identical finding text adjudicated differently across arms "
              f"({detail}): {item.title!r}")
    if cert.incomplete_pairs:
        print(f"  incomplete     {len(cert.incomplete_pairs)} repetition(s) lost an arm:")
        for pair in cert.incomplete_pairs:
            print(f"    {pair.case_id} / {pair.backend} rep{pair.rep}: {pair.reason}")

    for backend in cert.backends:
        print(f"\n{backend.backend}: control ({backend.control_arm}) vs "
              f"treatment ({backend.treatment_arm})")
        rules = ARM_RULES.get(backend.treatment_arm, NO_ARM_RULES)
        if rules.certifying_class is not None:
            print(f"  pre-registered for {backend.treatment_arm}: only "
                  f"{rules.certifying_class} may certify; every other class is reported")
        _print_veto(backend.veto)
        _print_classes(backend.classes)
        _print_twins(backend.twins)
        print(f"  verdict: {backend.verdict}")
        for reason in backend.reasons:
            print(f"    {reason}")

    print(f"\n{cert.verdict}")
    for reason in cert.reasons:
        print(f"  {reason}")


def _print_veto(veto: VetoResult) -> None:
    print("  veto — adjudicated false-positive critical+high per run, clean controls only")
    for case in veto.per_case:
        print(f"    {case.case_id}  reps {case.reps}  control {_num(case.control_mean)}  "
              f"treatment {_num(case.treatment_mean)}  delta {_num(case.delta)}  "
              f"{'ok' if case.within_bound else 'OVER'}")
    if not veto.per_case:
        print("    no clean-control case scored")
    print(f"    aggregate  control {_num(veto.control_mean)} "
          f"({veto.control_total} fp over {veto.control_runs} runs)  "
          f"treatment {_num(veto.treatment_mean)} "
          f"({veto.treatment_total} fp over {veto.treatment_runs} runs)  "
          f"{'ok' if veto.aggregate_ok else 'OVER'}")
    if veto.subset_cases:
        print(f"    aggregate over {', '.join(veto.subset_cases)} — the controls this arm's "
              f"text reaches — control {_num(veto.subset_control_mean)} "
              f"({veto.subset_control_total} fp over {veto.subset_control_runs} runs)  "
              f"treatment {_num(veto.subset_treatment_mean)} "
              f"({veto.subset_treatment_total} fp over {veto.subset_treatment_runs} runs)  "
              f"{'ok' if veto.subset_ok else 'OVER'}")
    print(f"    veto {'holds' if veto.holds else 'TRIPPED'}"
          + ("" if veto.at_protocol_depth else
             f" — measured at n={veto.min_case_reps}, below the protocol "
             f"n>={PROTOCOL_MIN_REPS}, so it can block but cannot certify"))


def _grade_text(entry: ClassRecall) -> str:
    """Whether a class is verdict-grade, and if not, which bar it missed."""
    if entry.certifying:
        return f"certifying, {entry.cases} independent cases at n>={entry.min_reps}"
    if entry.cases < CERTIFYING_CLASS_MIN_CASES:
        return (f"reported only, {entry.cases} independent case(s) — below the "
                f"{CERTIFYING_CLASS_MIN_CASES}-case bar")
    return (f"reported only, {entry.cases} independent cases but its shallowest scored "
            f"{entry.min_reps} complete repetition(s) — below the protocol "
            f"n>={PROTOCOL_MIN_REPS}")


def _print_classes(classes: list[ClassRecall]) -> None:
    print("  class recall — a defect is FOUND by an arm when a majority of its scored "
          "repetitions matched")
    if not classes:
        print("    no defect case scored")
    for entry in classes:
        print(f"    {entry.defect_class} [{_grade_text(entry)}]")
        print(f"      control {entry.control_found}/{entry.cases}  "
              f"treatment {entry.treatment_found}/{entry.cases}  "
              f"{entry.absolute_gain:+d} ({_pct(entry.relative_gain)})  "
              f"criterion {'met' if entry.criterion_met else 'not met'}")
        for case in entry.per_case:
            print(f"        {case.case_id}  control {_recall_cell(case, CONTROL)}  "
                  f"treatment {_recall_cell(case, TREATMENT)}")


def _print_twins(twins: list[CaseRecall]) -> None:
    if not twins:
        return
    print("  mode sensitivity — code-mode twins, excluded from certification arithmetic")
    for case in twins:
        print(f"    {case.case_id} ({case.defect_class}, twin of {case.mutation})  "
              f"control {_recall_cell(case, CONTROL)}  "
              f"treatment {_recall_cell(case, TREATMENT)}")


def _recall_cell(case: CaseRecall, role: str) -> str:
    hits, reps, found = (
        (case.control_hits, case.control_reps, case.control_found) if role == CONTROL
        else (case.treatment_hits, case.treatment_reps, case.treatment_found)
    )
    return f"{'found' if found else 'not found':<9} ({hits}/{reps})"


def report_json(cert: Certification) -> dict:
    return {
        "sweep_id": cert.sweep_id,
        "results": cert.results_path,
        "adjudications": cert.adjudications_path,
        "decisions_total": cert.decisions_total,
        "decisions_by_type": cert.decisions_by_type,
        "decisions_unused": cert.decisions_unused,
        "invalidated_cases": cert.invalidated_cases,
        "removed_cases": cert.removed_cases,
        "cross_arm_inconsistencies": [asdict(c) for c in cert.cross_arm],
        "incomplete_pairs": [asdict(p) for p in cert.incomplete_pairs],
        "backends": [
            {
                "backend": b.backend,
                "control_arm": b.control_arm,
                "treatment_arm": b.treatment_arm,
                "veto": {
                    "per_case": [
                        {
                            "case_id": c.case_id, "reps": c.reps,
                            "control_total": c.control_total,
                            "treatment_total": c.treatment_total,
                            "control_mean": _float(c.control_mean),
                            "treatment_mean": _float(c.treatment_mean),
                            "delta": _float(c.delta), "within_bound": c.within_bound,
                        }
                        for c in b.veto.per_case
                    ],
                    "control_runs": b.veto.control_runs,
                    "treatment_runs": b.veto.treatment_runs,
                    "control_total": b.veto.control_total,
                    "treatment_total": b.veto.treatment_total,
                    "control_mean": _float(b.veto.control_mean),
                    "treatment_mean": _float(b.veto.treatment_mean),
                    "min_case_reps": b.veto.min_case_reps,
                    "at_protocol_depth": b.veto.at_protocol_depth,
                    "per_case_ok": b.veto.per_case_ok,
                    "aggregate_ok": b.veto.aggregate_ok,
                    "subset_cases": list(b.veto.subset_cases),
                    "subset_control_runs": b.veto.subset_control_runs,
                    "subset_treatment_runs": b.veto.subset_treatment_runs,
                    "subset_control_total": b.veto.subset_control_total,
                    "subset_treatment_total": b.veto.subset_treatment_total,
                    "subset_control_mean": _float(b.veto.subset_control_mean),
                    "subset_treatment_mean": _float(b.veto.subset_treatment_mean),
                    "subset_ok": b.veto.subset_ok,
                    "holds": b.veto.holds,
                    "reasons": b.veto.reasons,
                },
                "designated_certifying_class":
                    ARM_RULES.get(b.treatment_arm, NO_ARM_RULES).certifying_class,
                "classes": [
                    {
                        "defect_class": c.defect_class, "cases": c.cases,
                        "control_found": c.control_found,
                        "treatment_found": c.treatment_found,
                        "min_reps": c.min_reps,
                        "certifying": c.certifying, "absolute_gain": c.absolute_gain,
                        "relative_gain": _float(c.relative_gain),
                        "criterion_met": c.criterion_met,
                        "per_case": [asdict(p) for p in c.per_case],
                    }
                    for c in b.classes
                ],
                "twins": [asdict(t) for t in b.twins],
                "verdict": b.verdict,
                "reasons": b.reasons,
            }
            for b in cert.backends
        ],
        "verdict": cert.verdict,
        "reasons": cert.reasons,
    }


def render_pending(
    sweep_id: str, results: Path, pending: list[PendingFinding],
    corpus: str, results_hash: str,
) -> str:
    """A YAML skeleton of every decision the rules need, ready to be filled in.

    Derived mechanically from the results, so emitting it decides nothing: `decision:
    TODO` fails the enum check, which is what keeps the skeleton from becoming a default.
    """
    lines = [
        f"# {len(pending)} finding(s) need a recorded human call before this sweep scores.",
        "# Replace every TODO. A decision must be one of: " + ", ".join(DECISIONS) + ".",
        f"sweep_id: {json.dumps(sweep_id)}",
        f"results_file: {json.dumps(results.name)}",
        "# Written here, checked at scoring time: calls made against one corpus and one",
        "# results file cannot be re-made for another. Do not edit.",
        f"corpus_digest: {json.dumps(corpus)}",
        f"results_digest: {json.dumps(results_hash)}",
        "adjudicator: TODO",
        "decisions:",
    ]
    for item in pending:
        lines += [
            f"  # [{item.severity}] {item.title}",
            f"  # {item.reason}",
            f"  - case_id: {json.dumps(item.case_id)}",
            f"    backend: {json.dumps(item.backend)}",
            f"    arm: {json.dumps(item.arm)}",
            f"    arm_role: {json.dumps(item.arm_role)}",
            f"    rep: {item.rep}",
            f"    finding_index: {item.finding_index}",
            "    decision: TODO",
            "    rationale: TODO",
        ]
    return "\n".join(lines) + "\n"


# --- CLI -----------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """An argument parser that cannot exit on a verdict code.

    Two of argparse's defaults are verdicts here: it exits 2 on a bad argument, which is
    VETOED, and 0 after printing `--help`, which is CERTIFIED. A caller gating a prompt
    change on exit status would read a mistyped flag as a blocked change and a help screen
    as a certified one — quietly, and one of them in the direction that looks responsible.
    Both become EXIT_USAGE: asking for usage and getting usage wrong are the same kind of
    outcome, and neither is a decision about a prompt.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        super().exit(EXIT_USAGE if status == 0 else status, message)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _Parser(
        prog="adjudicate",
        description="Turn recorded human finding-decisions into the certification verdict. "
                    "Reads stored output only; calls no backend.",
    )
    parser.add_argument("results", type=Path, help="JSONL file from paired_runner.py")
    parser.add_argument(
        "--adjudications", type=Path,
        help="YAML adjudication artifact for this sweep. Required unless --pending.",
    )
    parser.add_argument(
        "--cases", type=Path, default=CASES_DIR,
        help="Case manifest directory the results were run against (default: evals/cases)",
    )
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT,
        help="Checkout the cases' paths are relative to. Used only to reduce an absolute "
             "path a model cited to a repo-relative one (default: this checkout)",
    )
    parser.add_argument(
        "--pending", action="store_true",
        help="Emit a YAML skeleton of every finding still needing a decision, then stop. "
             "With --adjudications, only the ones not already recorded.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the report as JSON instead of text",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Score a sweep, or draft the calls it still needs.

    Exit status carries the outcome, and nothing that is not a computed verdict may land on
    a verdict code: 0 CERTIFIED, 1 NOT CERTIFIED, 2 VETOED, 3 no verdict computable, 4 a
    `--pending` draft, 64 a usage error. A verdict, a sweep that could not be scored and a
    mistyped flag are three different outcomes; a drafted work list exiting 0 would read as
    a certification.
    """
    args = parse_args(argv)
    if not args.results.is_file():
        print(f"Error: {args.results} not found.", file=sys.stderr)
        return EXIT_NO_VERDICT
    if args.adjudications is None and not args.pending:
        print("Error: --adjudications is required (or --pending to draft one).",
              file=sys.stderr)
        return EXIT_NO_VERDICT
    try:
        header, rows = load_jsonl(args.results)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_NO_VERDICT
    if not rows:
        print(f"Error: {args.results} has no run rows.", file=sys.stderr)
        return EXIT_NO_VERDICT
    found = sweep_ids(rows)
    if len(found) > 1:
        # No override here, unlike tier1's --allow-multiple-sweeps: tier 1 can report two
        # sweeps side by side because it only describes them. A certification is one
        # decision about one comparison, and there is no honest way to make it out of
        # measurements taken against different conditions.
        print(
            f"Error: {args.results} contains {len(found)} sweeps "
            f"({', '.join(s[:8] for s in found)}). A certification is one decision about "
            "one paired session; rows from two sessions cannot make one.",
            file=sys.stderr,
        )
        return EXIT_NO_VERDICT

    try:
        cases = _load_corpus(args.cases, rows)
        if args.pending:
            print(_pending_document(args, rows, cases, header), end="")
            return EXIT_DRAFTED
        adjudications = load_adjudications(args.adjudications)
        if adjudications.sweep_id != found[0]:
            raise AdjudicationError(
                f"{args.adjudications} is for sweep {adjudications.sweep_id[:8]}, but "
                f"{args.results} holds sweep {found[0][:8]}"
            )
        cert = compute(rows, cases, adjudications, args.repo, args.results, header)
    except IncompleteAdjudication as e:
        print(f"Error: {e}. Nothing is scored until every one has a recorded call "
              "(`--pending` drafts them):", file=sys.stderr)
        for item in e.missing:
            print(f"  {item.case_id} {item.backend} {item.arm_role}({item.arm}) "
                  f"rep{item.rep} #{item.finding_index} [{item.severity}] {item.title}"
                  f" — {item.reason}", file=sys.stderr)
        return EXIT_NO_VERDICT
    except (AdjudicationError, CaseError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_NO_VERDICT

    if args.json:
        print(json.dumps(report_json(cert), indent=2))
    else:
        print_report(cert)
    return EXIT_STATUS[cert.verdict]


def _load_corpus(directory: Path, rows: list[dict]) -> dict[str, Case]:
    cases = {case.id: case for case in load_cases(directory)}
    missing = sorted({row["case_id"] for row in rows} - set(cases))
    if missing:
        raise AdjudicationError(
            f"{directory} has no manifest for {', '.join(missing)}. The verdict reads "
            "defect.class, defect.file and defect.span from the manifests, so a result "
            "row whose case is gone cannot be scored."
        )
    mislabelled = sorted({
        row["case_id"] for row in rows
        if row.get("case_is_control") is not None
        and row["case_is_control"] != cases[row["case_id"]].is_control
    })
    if mislabelled:
        raise AdjudicationError(
            f"the results and the corpus disagree about whether {', '.join(mislabelled)} "
            "is a clean control. The manifest changed after the sweep ran; score against "
            "the corpus the sweep was run with."
        )
    return cases


def _pending_document(
    args: argparse.Namespace, rows: list[dict], cases: dict[str, Case], header: dict,
) -> str:
    """Draft the calls still outstanding, holding any artifact given to the same bar.

    Re-drafting reads an existing artifact to subtract what is already recorded, so that
    artifact decides which findings a human is shown as still needing a call. An artifact
    for another sweep, or one whose decisions do not survive validation, would silently
    hide real work — so it is checked here exactly as it is at scoring time.
    """
    sweep_id = sweep_ids(rows)[0]
    finals = final_attempts(rows)
    check_header(header, finals)
    parsed = findings_by_unit(finals)
    complete, _ = split_complete_pairs(finals)
    invalidated: frozenset[str] = frozenset()
    recorded: set[tuple] = set()
    if args.adjudications is not None:
        _require(
            args.adjudications.is_file(),
            f"{args.adjudications} does not exist. Drop --adjudications to draft from "
            "scratch; naming a file that is not there would silently draft every call "
            "again as if none had been made.",
        )
        existing = load_adjudications(args.adjudications)
        _require(
            existing.sweep_id == sweep_id,
            f"{args.adjudications} is for sweep {existing.sweep_id[:8]}, but "
            f"{args.results} holds sweep {sweep_id[:8]}",
        )
        verify_digests(existing, cases, set(cases), args.results)
        validate_against_results(
            existing.decisions, finals, cases, parsed, sweep_id, args.repo,
        )
        invalidated = frozenset(
            a.case_id for a in existing.decisions if a.decision == CASE_INVALIDATED
        )
        recorded = {a.key for a in existing.decisions}
    # Closed over mutations, exactly as at scoring time: an invalidated representative
    # takes its twin with it, so the twin's findings are not drafted as outstanding work.
    removed = removed_cases(cases, invalidated)
    pending = [
        item for item in pending_findings(
            complete, cases, parsed, args.repo, skip_cases=removed,
        )
        if item.key not in recorded
    ]
    return render_pending(
        sweep_id, args.results, pending,
        corpus_digest(cases, set(cases)),
        results_digest(args.results),
    )


if __name__ == "__main__":
    sys.exit(main())
