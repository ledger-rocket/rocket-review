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

- Security: the claude backend no longer loads Claude Code settings from the checkout
  under review, in every mode. Only the user's own settings load, and no MCP server
  starts. New `--claude-setting-sources none` option and `claude_setting_sources` user
  config key: load no user, project or local settings file, so the user's own permission
  rules, plugins and hooks do not reach the reviewer either. Organisation managed (policy)
  settings still apply. The default stays `user`, because user settings often carry the
  `env` or `apiKeyHelper` that Claude Code needs to reach a model. One consequence of
  ignoring settings files: Claude Code no longer loads the project's `CLAUDE.md` as memory
  for the reviewer. The reviewer can still read it as a file, and `--docs` sends it as
  project standards.
- New `--allow-exec` flag and `allow_exec` user-config key, off by default. For a review
  of your own code, the claude backend may run the project's test commands to confirm a
  suspected defect, and the review names each command it ran. The default commands are
  `just test*`, `go test`, `pytest`, `uv run pytest`, `cargo test`, `node --test` and
  `npm test`; `exec_commands` replaces the list and `exec_commands_extra` adds to it. All
  three keys are user file only. `--exec-command PATTERN`, repeatable, sets the list for
  one run. `--allow-exec` or `--exec-command` with `--pr` is an error, and the config key
  is ignored for `--pr`. `--no-allow-exec` turns the config key off for one run.
  `rr --fingerprint` records the allowed commands.
- Diff reviews treat an unexplained removed, skipped or loosened test or assertion as a
  finding, HIGH when it removes coverage of a behaviour the change touches.
