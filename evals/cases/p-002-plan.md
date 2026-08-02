# Plan: add `--since <ref>` to review a range of commits

## Goal

`rr --commit <sha>` reviews one commit. Reviewing a stack of commits before opening a PR
means running `rr` once per commit and reading the results separately. `--since <ref>`
should review every commit reachable from HEAD but not from `<ref>`, one review per
commit, printed under one heading.

## Steps

1. Add `--since REF` to the parser, mutually exclusive with `--commit`, `--diff`,
   `--staged`, `--pr` and file arguments. Resolve `REF` through the existing
   `resolve_commit`, so an option-shaped value is rejected exactly as it is today.
2. Enumerate the range with `git rev-list --reverse <ref>..HEAD`, and fail with a clear
   message when it is empty.
3. For each commit in the range, build a `ReviewJob` with `commit` set to that OID and run
   the selected backends over it through the existing `run_one` fan-out.
4. Before reviewing a commit, look it up in the review cache written in step 3 and skip
   any commit whose diff was already reviewed under the same prompt hash, so re-running
   after adding one commit does not pay for the whole range again.
5. Render the per-commit results in `rev-list` order, each under a
   `## <short-sha> <subject>` heading. Under `--json`, emit one envelope with a `commits`
   array instead of the single `results` list.

## Risks

- A long range is expensive: every commit is a full billed review. Cap the range at 20
  commits and exit with an error above that, telling the caller to narrow it.
- `<ref>..HEAD` is empty when `<ref>` is not an ancestor of HEAD, which reads as "nothing
  to review" rather than as the mistake it usually is. Step 2's message must say which of
  the two happened.

## Validation

- Unit test: `--since` together with `--diff` exits 1 with the mutual-exclusion message.
- Unit test: an empty range exits 1 before any backend is launched.
- Unit test with a stub backend: a three-commit range produces three result blocks, in
  `rev-list` order.
- Unit test: `--json` for a three-commit range validates against the extended envelope,
  and `schema_version` is bumped because `results` changed shape.
