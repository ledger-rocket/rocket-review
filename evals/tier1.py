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


@dataclass(frozen=True)
class Distribution:
    n: int
    mean: float
    median: float
    minimum: int
    maximum: int


def _distribution(values: list[int]) -> Distribution:
    if not values:
        return Distribution(n=0, mean=0.0, median=0.0, minimum=0, maximum=0)
    return Distribution(
        n=len(values), mean=round(statistics.fmean(values), 3),
        median=statistics.median(values), minimum=min(values), maximum=max(values),
    )


@dataclass
class GroupMetrics:
    backend: str
    arm: str
    arm_role: str
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
    findings_per_run: Distribution = field(
        default_factory=lambda: _distribution([])
    )
    findings_per_run_by_severity: dict[str, Distribution] = field(default_factory=dict)


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


def final_attempts(rows: list[dict]) -> list[dict]:
    """Keep the last attempt of each unit; the earlier ones record what was retried."""
    best: dict[tuple, dict] = {}
    for row in rows:
        key = (row["case_id"], row["backend"], row["arm"], row["rep"])
        if key not in best or row["attempt"] > best[key]["attempt"]:
            best[key] = row
    return list(best.values())


def compute(
    rows: list[dict], line_counter: LineCounter, repo: Path,
) -> list[GroupMetrics]:
    """Group the final attempt of every unit by backend and arm, and score each group."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in final_attempts(rows):
        groups.setdefault((row["backend"], row["arm"]), []).append(row)

    metrics: list[GroupMetrics] = []
    for (backend, arm), group in sorted(groups.items()):
        scored = [r for r in group if r["outcome"] != BACKEND_ERROR]
        counts_by_severity: dict[str, list[int]] = {s: [] for s in SEVERITIES}
        counts_total: list[int] = []
        findings_total = line_bearing = line_resolved = 0
        do_not_flag_by_class: dict[str, int] = {}

        for row in scored:
            parsed = parse_backend_output(row["raw"], row["backend"], row["requested_model"])
            counts_total.append(len(parsed.findings))
            for severity in SEVERITIES:
                counts_by_severity[severity].append(
                    sum(1 for f in parsed.findings if f.severity == severity)
                )
            for finding in parsed.findings:
                findings_total += 1
                category = classify_do_not_flag(finding.title or "")
                if category:
                    do_not_flag_by_class[category] = do_not_flag_by_class.get(category, 0) + 1
                # Findings with a null file or line make no locatable claim, so they are
                # exempt rather than counted as unresolved.
                if not finding.file or finding.line is None:
                    continue
                line_bearing += 1
                total_lines = line_counter(
                    row["repo_commit"], normalize_cited_path(finding.file, repo)
                )
                if total_lines is not None and 1 <= finding.line <= total_lines:
                    line_resolved += 1

        strict_valid = sum(1 for r in scored if r["outcome"] == VALID)
        do_not_flag_hits = sum(do_not_flag_by_class.values())
        metrics.append(GroupMetrics(
            backend=backend, arm=arm, arm_role=group[0]["arm_role"],
            runs_scored=len(scored), runs_failed=len(group) - len(scored),
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
                s: _distribution(counts_by_severity[s]) for s in SEVERITIES
            },
        ))
    return metrics


def _rate(numerator: int, denominator: int) -> float | None:
    # None, never 0.0: an empty denominator means "not measured here", and printing it as
    # 0% would read as a total failure of whatever it measures.
    return round(numerator / denominator, 4) if denominator else None


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def print_report(metrics: list[GroupMetrics]) -> None:
    for m in metrics:
        print(f"\n{m.backend} / {m.arm} ({m.arm_role})")
        print(f"  runs scored           {m.runs_scored} (failed: {m.runs_failed})")
        print(f"  strict-valid          {_pct(m.strict_valid_rate)} "
              f"({m.strict_valid}/{m.runs_scored})")
        print(f"  file:line resolves    {_pct(m.line_resolved_rate)} "
              f"({m.line_resolved}/{m.line_bearing} line-bearing findings)")
        print(f"  DO-NOT-FLAG tripwire  {_pct(m.do_not_flag_rate)} "
              f"({m.do_not_flag_hits}/{m.findings_total} findings)"
              + (f" {m.do_not_flag_by_class}" if m.do_not_flag_by_class else ""))
        d = m.findings_per_run
        print(f"  findings/run          mean {d.mean} median {d.median} "
              f"range {d.minimum}-{d.maximum} over {d.n} run(s)")
        for severity in SEVERITIES:
            s = m.findings_per_run_by_severity[severity]
            print(f"    {severity:<9}         mean {s.mean} median {s.median} "
                  f"range {s.minimum}-{s.maximum}")


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
    metrics = compute(rows, GitLineCounter(args.repo), args.repo)
    if args.json:
        print(json.dumps([asdict(m) for m in metrics], indent=2))
    else:
        print_report(metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
