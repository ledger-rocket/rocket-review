The `current/` arm at commit `3d169c3`, with `DIFF_REVIEW_PROMPT` edited and nothing else
touched (PRO-8570). The edit ranks findings by consequence instead of by kind. MEDIUM is a
bounded defect, a maintainability problem likely to cause a defect, or a test that cannot
fail. LOW takes missing tests, which coverage gates measure better than a model. A new
TESTS item in REVIEW FOCUS asks the test-validity questions, and "missing tests" leaves
COMPLETENESS. DO NOT FLAG gains what a linter or CI gate configured in the repository
already enforces (complexity, function length, nesting depth, argument count, unused or
dead code, test coverage, vulnerable dependencies). Frozen input: never re-exported, and
`test_arms.py` pins its hash and asserts that the other four files are `current/` at
`3d169c3`.

Only the diff body changes. `diff`, `--commit`, `--staged` and `--pr` reviews all run it,
and `diff` is the only mode with clean controls to measure noise against. The code body
reviews whole files rather than a change, and the plan body has no tests or code to rank,
so both keep their rubrics.

Measurement. Paired sweep `08dd840d` (results file
`paired-20261007T001341.306096Z-08dd840d.jsonl`, not committed, like every result file),
control `current/` at `3d169c3` (hash `14e3dcae…`) against this arm, backend
`claude:claude-opus-5-5`, `--runs 3`, the 25 `diff`-mode cases (19 mutants, 6 clean
controls). Tier 1: strict-valid 75/75 in both arms. Findings per run, control vs this arm:
critical 0.55 vs 0.37, high 1.05 vs 0.89, medium 0.67 vs 1.21, low 0.75 vs 0.72. On the
clean controls alone: medium 0.78 vs 1.33 per run, and 7/18 vs 14/18 runs carry a finding
at medium or above. The new mediums are the kinds the rubric names: tests that cannot
fail, values that must change together, comments that state the wrong behaviour.
"Missing tests" findings at medium or above fall from 10 to 3. An earlier sweep of the
arm without the DO NOT FLAG bullet (`730c71f4`, same protocol) gave the same picture.

`adjudicate.py` was not run, so no verdict exists. The clean-control CRITICAL/HIGH
findings are the control arm's two on `c-005` and this arm's one on `c-006`. The `c-006`
finding claims that a reviewed checkout's `.claude/settings.json` widens the claude
sandbox. A probe covers only part of it: in an untrusted workspace, Claude Code 2.1.291
printed "Ignoring 1 permissions.allow entry from .claude/settings.json: this workspace has
not been trusted" and refused the command. The probe does not cover a trusted workspace,
such as a PR branch checked out in the user's own repository, so the call is open. The two
calls decide the veto. If `c-006` is real, the case leaves both arms and the aggregate
condition holds under either `c-005` call. If `c-006` is a false positive and `c-005` is
invalidated, as M5 did, the aggregate fails by one finding in 15 runs; if `adjudicate.py`
confirms that, the verdict is VETOED, and that verdict is binding. This branch must not
merge until both calls are made and `adjudicate.py` has run.
