# Changelog

## 0.6.0 (unreleased)

- Diff reviews rank findings by consequence, not by kind. This changes every review of a
  diff, commit, staged change or pull request, in CI and in manual runs. MEDIUM is a
  defect with bounded consequence, and the finding must state a concrete failure scenario:
  the input or state, and the wrong result. A maintainability or test-validity finding is
  MEDIUM only with such a scenario; otherwise it is LOW. Missing tests are LOW, and rr
  still reports them. REVIEW FOCUS has a new TESTS item: which inputs no test exercises,
  which flag could switch the guarantee off silently, and how the suite could pass while
  it exercises nothing. DO NOT FLAG now covers what a linter or CI gate configured in the
  repository already enforces (complexity, function length, nesting depth, argument
  count, unused or dead code, test coverage, vulnerable dependencies). Code and plan
  reviews keep their rubrics.
