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
