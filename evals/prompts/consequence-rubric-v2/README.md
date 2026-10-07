The `consequence-rubric/` arm with one change in `DIFF_REVIEW_PROMPT`: a MEDIUM finding
must state a concrete failure scenario, the specific input or state and the wrong result
it produces. A maintainability or test-validity finding without one is LOW. Everything
else is as in `consequence-rubric/`: missing tests are LOW, REVIEW FOCUS has the TESTS
item, and DO NOT FLAG covers what the repository's own linters and CI gates enforce. The
other four files are `current/` at `3d169c3`. Frozen input: never re-exported, and
`test_arms.py` pins its hash and asserts that only the diff body differs from that base.
This is the rubric `rocket_review/prompts.py` ships.

Why. The `consequence-rubric/` sweep doubled the clean-control runs that carry a finding at
medium or above (7/18 to 14/18). Its new mediums were the kinds its MEDIUM line names. This
arm asks whether a failure-scenario bar keeps those at LOW unless the reviewer can show the
failure.

Measurement. Paired sweep `2df6d5e4`, same protocol as `consequence-rubric/`: control
`current/` at `3d169c3`, `claude:claude-opus-5-5`, `--runs 3`, the 25 `diff`-mode cases.
Strict-valid 75/75 control, 74/75 this arm. Findings per run, control vs this arm:
critical 0.53 vs 0.35, high 1.01 vs 1.01, medium 0.63 vs 0.83, low 0.64 vs 0.87. Clean
controls: medium 0.83 vs 1.00 per run, and 8/18 vs 10/18 runs carry a finding at medium or
above (`consequence-rubric/`: 14/18). "Missing tests" findings at medium or above: 6 vs 2.
Mutant runs with a finding that matches the defect: 55/57 in both arms, 54 of them at high
or above in both.

`adjudicate.py`: veto holds, NOT CERTIFIED (n=3, below the protocol's 5). No class loses a
defect. Both counts above are runs with an `adjudicate.py` `matches-defect` call. Two
cases differ by one run: `b-016` (control 3/3, this arm 2/3, found in both) and `b-017`
(control 1/3, this arm 2/3), where most findings cite the exit-code check rather than the
case span, so only this arm reaches a majority. `c-005`'s HIGH findings are
false positives, one per arm, called as for `consequence-rubric/`.

The promotion is a judgement on this record, per *Certification is suspended* in
`evals/README.md`: this arm refuses fewer clean diffs than `consequence-rubric/` with the
same mutant detection, and still slightly more than `current/` (10/18 against 8/18).
