"""Tier-1 metrics: everything a paired run can be scored on without calling a backend.

Reads a JSONL file produced by `paired_runner.py` and computes, per backend and arm:
strict schema-validity, how often a cited file:line actually exists, how often a finding
looks like something the prompt's DO-NOT-FLAG list rules out, and the shape of the
findings-per-run distribution by severity.

None of this adjudicates whether a finding is *right*. That is a human call, governed by
the pre-registered rules in evals/README.md. Tier 1 is the cheap, deterministic layer that
says whether a prompt change moved anything worth adjudicating.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_common import REPO_ROOT  # noqa: E402
from strict_validator import BACKEND_ERROR, VALID  # noqa: E402

from rocket_review.models import SEVERITIES, parse_backend_output  # noqa: E402

# A finding whose title reads like one of these is something the prompt explicitly tells
# the model not to raise. Matching is on the TITLE only: the title is the claim the finding
# makes, whereas the same words inside `why` are usually incidental — "stripping trailing
# whitespace would corrupt the patch" is a real correctness argument, not a style nit.
#
# This is a keyword tripwire, not a classifier, and it has no notion of whether a finding
# is correct. A jump in the rate between two arms is a signal to go and read the findings;
# it is never on its own a verdict about either arm. First pattern to match wins.
DO_NOT_FLAG_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "formatting",
        r"\bformatting\b|\bwhitespace\b|\bindentation\b|\bindented\b"
        r"|\bline length\b|\bline (?:is )?too long\b"
        r"|\b(?:missing|extra|add|remove|one|two) blank lines?\b"
        r"|\bexceeds \d+ (?:characters|chars|columns)\b",
    ),
    (
        "import-ordering",
        r"\bimport (?:order|ordering|sorting|grouping|group)\b"
        r"|\b(?:unsorted|ungrouped|misordered|out-of-order) imports\b"
        r"|\bimports? (?:are |is )?(?:not )?(?:sorted|ordered|grouped)\b",
    ),
    # "missing semicolon" is a lint nit; "semicolon-separated PATH" is a real claim about
    # behaviour. The qualifier is what separates them, so the bare noun is not enough.
    (
        "quote-style",
        r"\bquote style\b|\b(?:single|double)[- ]quotes?\b"
        r"|\b(?:missing|extra|stray|omitted|unnecessary) semicolons?\b"
        r"|\bsemicolon (?:style|usage|placement)\b"
        r"|\b(?:missing|extra|add|unnecessary) trailing commas?\b"
        r"|\btrailing comma (?:style|convention)\b",
    ),
    (
        "naming",
        r"\bnaming (?:convention|style)\b"
        r"|\b(?:snake_case|camelCase|PascalCase|SCREAMING_SNAKE_CASE)\b"
        r"|\brename\b[^.]{0,40}\bconvention\b",
    ),
    # A wrong annotation is a real finding; only an absent one is on the DO-NOT-FLAG list.
    (
        "missing-annotations",
        r"\b(?:missing|no|add|lacks?|absent|without)\b[^.]{0,40}"
        r"\btype (?:hint|annotation)s?\b"
        r"|\btype (?:hint|annotation)s? (?:are |is )?(?:missing|absent)\b"
        r"|\buntyped\b",
    ),
    (
        "docs-only",
        r"\bdocstrings?\b|\btypos? in (?:a |the )?(?:comment|docstring|doc|documentation)\b"
        r"|\bcomment typo\b|\bmisspell",
    ),
)

_COMPILED = tuple(
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in DO_NOT_FLAG_PATTERNS
)


def classify_do_not_flag(title: str) -> str | None:
    """Name the DO-NOT-FLAG category a finding's title looks like, or None."""
    for name, pattern in _COMPILED:
        if pattern.search(title):
            return name
    return None


#: (commit, path) -> line count of that file at that commit, or None if it is not there.
LineCounter = Callable[[str, str], int | None]


#: Severities the schema defines, plus a bucket for the ones a model invents. Inventing a
#: label is one of the things this harness exists to notice, so those findings cannot be
#: left out of the per-severity view — the buckets would stop summing to the total.
SEVERITY_OTHER = "other"
SEVERITY_BUCKETS = (*SEVERITIES, SEVERITY_OTHER)


def severity_bucket(severity: str) -> str:
    return severity if severity in SEVERITIES else SEVERITY_OTHER


@dataclass(frozen=True)
class Distribution:
    n: int
    #: None, never 0, when nothing was measured — same rule as _rate, for the same reason:
    #: a zero would read as "measured, and it was zero".
    mean: float | None
    median: float | None
    minimum: int | None
    maximum: int | None


def _distribution(values: list[int]) -> Distribution:
    if not values:
        return Distribution(n=0, mean=None, median=None, minimum=None, maximum=None)
    return Distribution(
        n=len(values), mean=round(statistics.fmean(values), 3),
        median=statistics.median(values), minimum=min(values), maximum=max(values),
    )


@dataclass
class Scores:
    """Every number computable from a set of scored runs.

    The same shape describes one arm pooled across its cases and one case within that arm,
    because the decision rules read both: the success criterion aggregates, the veto is
    per clean-control case.
    """

    #: Final attempts that produced judgeable output. Runs still failing after their retry
    #: are counted in runs_failed and kept out of every denominator below.
    runs_scored: int
    runs_failed: int
    strict_valid: int
    strict_valid_rate: float | None
    findings_total: int
    line_bearing: int
    line_resolved: int
    line_resolved_rate: float | None
    do_not_flag_hits: int
    do_not_flag_rate: float | None
    do_not_flag_by_class: dict[str, int] = field(default_factory=dict)
    findings_per_run: Distribution = field(default_factory=lambda: _distribution([]))
    findings_per_run_by_severity: dict[str, Distribution] = field(default_factory=dict)
    #: CRITICAL+HIGH findings per run. Kept as its own distribution because the median and
    #: range of a sum cannot be recovered from the two severities separately. Only the
    #: instance on `GroupMetrics.control_scores` is the veto rule's input — on any wider set
    #: of cases this pools defect cases, where a CRITICAL finding is the right answer. Both
    #: are pre-adjudication either way.
    critical_high_per_run: Distribution = field(default_factory=lambda: _distribution([]))


@dataclass
class CaseMetrics:
    case_id: str
    #: From the row, which carries it so a filtered or concatenated file stays readable.
    #: None when the results predate the field.
    is_control: bool | None
    scores: Scores
    #: Runs dropped because the other arm's run of the same repetition did not complete.
    excluded_unpaired: int = 0


@dataclass
class GroupMetrics:
    #: Which sweep produced these rows. Never merged across sweeps — see unit_key.
    sweep_id: str
    backend: str
    #: Role first: it, not the arm name, is what identifies a group. An A/A run has two
    #: groups with the same arm name, and that is the point of an A/A run.
    arm_role: str
    arm: str
    scores: Scores
    #: The same scores over clean-control cases only. This is the veto rule's input:
    #: `scores.critical_high_per_run` pools defect cases, where a CRITICAL finding is the
    #: correct answer rather than the noise the veto bounds.
    control_scores: Scores | None = None
    #: Runs dropped because their repetition's other arm did not complete. Reported, not
    #: absorbed: a sweep that quietly lost a third of its work should look like one.
    excluded_unpaired: int = 0
    #: Per case, ordered by case id. The veto rule is written per clean-control case, so
    #: the breakdown ships with the metrics rather than being re-derived by hand at the
    #: moment someone is deciding whether to ship a prompt.
    per_case: list[CaseMetrics] = field(default_factory=list)


class GitLineCounter:
    """Line count of a path at a commit, straight out of the object database.

    Resolution is against `repo_commit`, the snapshot the case is defined against. For a
    mutant case that is the *pre-patch* base, so a finding citing a line the patch appended
    past the original end of file reads as unresolvable. Mutants are line-for-line edits by
    construction, which keeps that rare — but it is why this metric is a hallucination
    tripwire rather than an exact locator.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self._cache: dict[tuple[str, str], int | None] = {}

    def __call__(self, commit: str, path: str) -> int | None:
        key = (commit, path)
        if key not in self._cache:
            self._cache[key] = self._count(commit, path)
        return self._cache[key]

    def _count(self, commit: str, path: str) -> int | None:
        # A revision that is not a plain object id, or a path git would read as an option,
        # is not something to hand to git and guess about — it is unresolvable, which is
        # exactly what the metric wants to record.
        if not re.fullmatch(r"[0-9a-f]{7,40}", commit) or path.startswith("-"):
            return None
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.repo), "show", f"{commit}:{path}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return len(proc.stdout.splitlines())


def normalize_cited_path(cited: str, repo: Path) -> str:
    """Reduce a model-cited path to something git can look up, or leave it alone."""
    cleaned = cited.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    candidate = Path(cleaned)
    if candidate.is_absolute():
        try:
            return str(candidate.relative_to(repo.resolve()))
        except ValueError:
            return cleaned
    return cleaned


def load_jsonl(path: Path) -> tuple[dict, list[dict]]:
    """Split a results file into its header and its run rows."""
    header: dict = {}
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{number}: not JSON: {e}") from e
            if obj.get("type") == "header":
                header = obj
            else:
                rows.append(obj)
    return header, rows


#: Rows written before sweep ids existed. Kept distinguishable from a real id so such a
#: file still scores, but never silently pooled with an identified sweep.
UNKNOWN_SWEEP = "unknown-sweep"


def sweep_of(row: dict) -> str:
    return row.get("sweep_id") or UNKNOWN_SWEEP


def sweep_ids(rows: list[dict]) -> list[str]:
    return sorted({sweep_of(row) for row in rows})


def pair_key(row: dict) -> tuple:
    """The repetition a run belongs to: its control and its treatment are one pair."""
    return (sweep_of(row), row["case_id"], row["backend"], row["rep"])


def unit_key(row: dict) -> tuple:
    """What makes a run unique. `arm_role` and `sweep_id` are part of it, not decoration.

    An A/A run — the same arm as both control and treatment, which is how the noise floor
    gets measured — puts the same arm *name* on two different runs of the same case and
    repetition. Keying on the name alone would collapse them into one unit and silently
    discard half the sweep. Two different arm directories that happen to share a basename
    would collapse the same way.

    The sweep id is in the key for the same reason one level up: two sessions produce rows
    with identical case/backend/arm/rep, and treating them as retries of one another would
    silently drop one session's data or pool measurements taken hours apart.
    """
    return (
        sweep_of(row), row["case_id"], row["backend"],
        row["arm"], row["arm_role"], row["rep"],
    )


def final_attempts(rows: list[dict]) -> list[dict]:
    """Keep the last attempt of each unit; the earlier ones record what was retried."""
    best: dict[tuple, dict] = {}
    for row in rows:
        key = unit_key(row)
        if key not in best or row["attempt"] > best[key]["attempt"]:
            best[key] = row
    return list(best.values())


@dataclass(frozen=True)
class IncompletePair:
    sweep_id: str
    case_id: str
    backend: str
    rep: int
    reason: str


def split_complete_pairs(rows: list[dict]) -> tuple[list[dict], list[IncompletePair]]:
    """Keep only repetitions where both arms produced a judgeable review.

    Excluding a failed run arm-by-arm quietly desynchronises the two denominators: a case
    that times out on the treatment only would keep contributing its control runs, so the
    arms would no longer be compared on the same work. Since a noisy case is exactly the
    kind that fails, that bias points somewhere in particular. A repetition is therefore
    all-or-nothing, and what got dropped is reported rather than absorbed.
    """
    pairs: dict[tuple, list[dict]] = {}
    for row in rows:
        pairs.setdefault(pair_key(row), []).append(row)

    complete: list[dict] = []
    incomplete: list[IncompletePair] = []
    for key, members in sorted(pairs.items()):
        sweep, case_id, backend, rep = key
        roles = {m["arm_role"] for m in members}
        failed = sorted(
            {m["arm_role"] for m in members if m["outcome"] == BACKEND_ERROR}
        )
        if failed:
            reason = f"{', '.join(failed)} failed after every attempt"
        elif len(roles) < 2:
            reason = f"only the {roles.pop()} run is present"
        else:
            complete.extend(members)
            continue
        incomplete.append(IncompletePair(
            sweep_id=sweep, case_id=case_id, backend=backend, rep=rep, reason=reason,
        ))
    return complete, incomplete


def score(rows: list[dict], line_counter: LineCounter, repo: Path) -> Scores:
    """Score one set of final attempts — an arm, or one case within an arm."""
    scored = [r for r in rows if r["outcome"] != BACKEND_ERROR]
    counts_by_severity: dict[str, list[int]] = {s: [] for s in SEVERITY_BUCKETS}
    counts_total: list[int] = []
    counts_critical_high: list[int] = []
    findings_total = line_bearing = line_resolved = 0
    do_not_flag_by_class: dict[str, int] = {}

    for row in scored:
        parsed = parse_backend_output(row["raw"], row["backend"], row["requested_model"])
        buckets = [severity_bucket(f.severity) for f in parsed.findings]
        counts_total.append(len(parsed.findings))
        counts_critical_high.append(sum(1 for b in buckets if b in ("critical", "high")))
        for bucket in SEVERITY_BUCKETS:
            counts_by_severity[bucket].append(sum(1 for b in buckets if b == bucket))
        # A plan is a standalone artifact, not a repository snapshot: its `repo_commit` is
        # provenance, and the plan file itself exists at no commit at all. Resolving its
        # citations against the object database would score every one of them as a
        # hallucination forever, and drag the pooled rate down with them. Exempt, on the
        # same principle as a finding that cites nothing.
        resolvable = row.get("source") != "seeded-plan"
        for finding in parsed.findings:
            findings_total += 1
            category = classify_do_not_flag(finding.title or "")
            if category:
                do_not_flag_by_class[category] = do_not_flag_by_class.get(category, 0) + 1
            # A finding with no file, no line, or a `file` that is not even a string made
            # no locatable claim, so it is exempt rather than counted as unresolved. The
            # type check is not paranoia: the runtime parser passes `file` through
            # uncoerced, so a schema-violating review — exactly what this harness is built
            # to catch — can put an object there, and scoring a whole sweep must not die on
            # one bad field.
            if not resolvable:
                continue
            if not isinstance(finding.file, str) or not finding.file or finding.line is None:
                continue
            line_bearing += 1
            total_lines = line_counter(
                row["repo_commit"], normalize_cited_path(finding.file, repo)
            )
            if total_lines is not None and 1 <= finding.line <= total_lines:
                line_resolved += 1

    strict_valid = sum(1 for r in scored if r["outcome"] == VALID)
    do_not_flag_hits = sum(do_not_flag_by_class.values())
    return Scores(
        runs_scored=len(scored), runs_failed=len(rows) - len(scored),
        strict_valid=strict_valid,
        strict_valid_rate=_rate(strict_valid, len(scored)),
        findings_total=findings_total,
        line_bearing=line_bearing, line_resolved=line_resolved,
        line_resolved_rate=_rate(line_resolved, line_bearing),
        do_not_flag_hits=do_not_flag_hits,
        do_not_flag_rate=_rate(do_not_flag_hits, findings_total),
        do_not_flag_by_class=dict(sorted(do_not_flag_by_class.items())),
        findings_per_run=_distribution(counts_total),
        findings_per_run_by_severity={
            bucket: _distribution(counts_by_severity[bucket]) for bucket in SEVERITY_BUCKETS
        },
        critical_high_per_run=_distribution(counts_critical_high),
    )


def compute(
    rows: list[dict], line_counter: LineCounter, repo: Path,
) -> list[GroupMetrics]:
    """Score complete repetitions, per sweep/backend/arm role, and per case within each."""
    finals = final_attempts(rows)
    complete, incomplete = split_complete_pairs(finals)
    scored_keys = {unit_key(r) for r in complete}

    # Keyed on role as well as name, and sorted with role first so control precedes
    # treatment: an A/A run has two groups whose arm name is identical, and merging them
    # would report the noise-floor measurement as a single arm. Sweep id leads, because two
    # sessions are never one measurement.
    #
    # Group keys come from every final attempt, not only the complete ones, so an arm whose
    # every repetition was dropped still appears — with nothing scored and a count saying
    # why, rather than vanishing from the report.
    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for row in finals:
        key = (sweep_of(row), row["backend"], row["arm_role"], row["arm"])
        groups.setdefault(key, []).append(row)

    metrics: list[GroupMetrics] = []
    for (sweep, backend, arm_role, arm), group in sorted(groups.items()):
        paired = [r for r in group if unit_key(r) in scored_keys]
        by_case: dict[str, list[dict]] = {}
        for row in group:
            by_case.setdefault(row["case_id"], []).append(row)
        controls = [r for r in paired if r.get("case_is_control") is True]
        metrics.append(GroupMetrics(
            sweep_id=sweep, backend=backend, arm_role=arm_role, arm=arm,
            scores=score(paired, line_counter, repo),
            control_scores=score(controls, line_counter, repo) if controls else None,
            excluded_unpaired=len(group) - len(paired),
            per_case=[
                CaseMetrics(
                    case_id=case_id,
                    is_control=case_rows[0].get("case_is_control"),
                    scores=score(
                        [r for r in case_rows if unit_key(r) in scored_keys],
                        line_counter, repo,
                    ),
                    excluded_unpaired=sum(
                        1 for r in case_rows if unit_key(r) not in scored_keys
                    ),
                )
                for case_id, case_rows in sorted(by_case.items())
            ],
        ))
    return metrics, incomplete


def _rate(numerator: int, denominator: int) -> float | None:
    # None, never 0.0: an empty denominator means "not measured here", and printing it as
    # 0% would read as a total failure of whatever it measures.
    return round(numerator / denominator, 4) if denominator else None


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _spread(d: Distribution) -> str:
    if d.n == 0:
        return "no runs"
    return f"mean {d.mean} median {d.median} range {d.minimum}-{d.maximum}"


def _case_label(case: CaseMetrics) -> str:
    kind = {True: "control", False: "defect", None: "unlabelled"}[case.is_control]
    return f"{case.case_id} ({kind})"


def print_report(metrics: list[GroupMetrics], incomplete: list[IncompletePair]) -> None:
    multi_sweep = len({m.sweep_id for m in metrics}) > 1
    if incomplete:
        print(f"\n{len(incomplete)} repetition(s) excluded — a repetition scores only when "
              "both arms produced a review:")
        for pair in incomplete:
            prefix = f"{pair.sweep_id[:8]} " if multi_sweep else ""
            print(f"  {prefix}{pair.case_id} / {pair.backend} rep{pair.rep}: {pair.reason}")
    for m in metrics:
        s = m.scores
        # Role first, name in parentheses: in an A/A run the name is the same on both
        # lines, and the role is the only thing telling them apart.
        sweep = f"[{m.sweep_id[:8]}] " if multi_sweep else ""
        print(f"\n{sweep}{m.backend} / {m.arm_role} ({m.arm})")
        print(f"  runs scored           {s.runs_scored} "
              f"(unpaired, excluded: {m.excluded_unpaired})")
        print(f"  strict-valid          {_pct(s.strict_valid_rate)} "
              f"({s.strict_valid}/{s.runs_scored})")
        print(f"  file:line resolves    {_pct(s.line_resolved_rate)} "
              f"({s.line_resolved}/{s.line_bearing} line-bearing findings)")
        print(f"  DO-NOT-FLAG tripwire  {_pct(s.do_not_flag_rate)} "
              f"({s.do_not_flag_hits}/{s.findings_total} findings)"
              + (f" {s.do_not_flag_by_class}" if s.do_not_flag_by_class else ""))
        print(f"  findings/run          {_spread(s.findings_per_run)} "
              f"over {s.findings_per_run.n} run(s)")
        for bucket in SEVERITY_BUCKETS:
            print(f"    {bucket:<9}         {_spread(s.findings_per_run_by_severity[bucket])}")
        print(f"  critical+high/run     {_spread(s.critical_high_per_run)}")
        # The line above pools defect cases, where a CRITICAL finding is the right answer.
        # The veto rule asks only about clean controls, so that number is printed rather
        # than left to be recombined by hand from the per-case rows below.
        controls = m.control_scores
        if controls is None or controls.critical_high_per_run.n == 0:
            print("    clean controls only no control runs scored")
        else:
            print(f"    clean controls only {_spread(controls.critical_high_per_run)} "
                  f"over {controls.critical_high_per_run.n} run(s)")
        # The veto rule is written per clean-control case, so the numbers it reads are
        # printed per case rather than left to be re-derived at decision time.
        print("  per case:")
        width = max(len(_case_label(c)) for c in m.per_case) if m.per_case else 0
        for case in m.per_case:
            c = case.scores
            print(f"    {_case_label(case):<{width}}  runs {c.runs_scored}"
                  f" (excluded: {case.excluded_unpaired})"
                  f"  strict-valid {_pct(c.strict_valid_rate)}"
                  f"  findings/run {_spread(c.findings_per_run)}"
                  f"  critical+high/run {_spread(c.critical_high_per_run)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tier1",
        description="Score a paired run's JSONL. Reads stored output only; calls no backend.",
    )
    parser.add_argument("results", type=Path, help="JSONL file from paired_runner.py")
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT,
        help="Repository the cases' repo_commit values live in (default: this checkout)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the metrics as JSON instead of a report",
    )
    parser.add_argument(
        "--allow-multiple-sweeps", action="store_true",
        help="Score a file containing rows from more than one sweep. Refused by default: "
             "the comparison only means anything within a session, so rows from two "
             "sessions are two measurements, never one. Even with this flag they are never "
             "pooled — each sweep is scored on its own.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.results.is_file():
        print(f"Error: {args.results} not found.", file=sys.stderr)
        return 1
    try:
        _, rows = load_jsonl(args.results)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not rows:
        print(f"Error: {args.results} has no run rows.", file=sys.stderr)
        return 1

    found = sweep_ids(rows)
    if len(found) > 1:
        if not args.allow_multiple_sweeps:
            print(
                f"Error: {args.results} contains {len(found)} sweeps "
                f"({', '.join(s[:8] for s in found)}). Both arms are only comparable "
                "within the session that ran them, so scoring across sweeps compares a "
                "prompt change against whatever else moved in between. Pass "
                "--allow-multiple-sweeps to score each sweep separately anyway.",
                file=sys.stderr,
            )
            return 1
        print(
            f"WARNING: scoring {len(found)} separate sweeps from one file. Each is scored "
            "on its own and nothing is pooled across them, but numbers from different "
            "sweeps are not comparable — they were measured against different conditions.",
            file=sys.stderr,
        )

    metrics, incomplete = compute(rows, GitLineCounter(args.repo), args.repo)
    if args.json:
        print(json.dumps({
            "groups": [asdict(m) for m in metrics],
            "incomplete_pairs": [asdict(p) for p in incomplete],
        }, indent=2))
    else:
        print_report(metrics, incomplete)
    return 0


if __name__ == "__main__":
    sys.exit(main())
