# The gates this repository runs before a push.
#
# quick-gate and preflight call scripts/check, the script each CI job runs
# (.github/workflows/ci.yml), so a gate here cannot drift from CI.

set shell := ["bash", "-c"]

[doc("List the recipes")]
default:
    @just --list --unsorted

[doc("ruff check, the quick gate")]
quick-gate:
    scripts/check --quick

[doc("scripts/check: what CI runs (ruff, mypy, pytest, build, yamllint, pip-audit)")]
preflight:
    scripts/check

[doc("rr review of the branch diff; refuses on a finding at or above high, or when no review ran")]
review-prepush:
    scripts/review-prepush

# core.hooksPath is relative, so a worktree of a branch without .githooks runs
# no hook. The executable bit matters: git ignores a hook without it.
[doc("Install this repository's git hooks (.githooks) in this clone")]
install-hooks:
    git config core.hooksPath .githooks
    chmod +x .githooks/*
    @echo "install-hooks: core.hooksPath = .githooks"
