# Plan: cache backend CLI version lookups

## Goal

`rr` shells out to `codex --version` and `claude --version` on every run to record which
tool answered. On a cold machine each call costs 200–400ms, and a cross-model run pays it
once per backend.

## Steps

1. Add a `~/.cache/rocket-review/versions.json` file mapping backend name to version
   string.
2. On startup, read the cache and use any entry it contains.
3. If a backend is missing from the cache, run `<binary> --version` and write the result
   back.
4. Ship it.

## Validation

Run `rr --diff` twice and confirm the second run is faster.
