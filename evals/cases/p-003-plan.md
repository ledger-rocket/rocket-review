# Plan: cut `rr`'s start-up cost on cross-model runs

## Goal

Every run shells out to each selected backend's CLI for `--version` before doing any work,
and `--docs` re-reads and re-follows the same standards files on every invocation. On a
cross-model run that is several hundred milliseconds spent before the first token.

## Steps

1. Move the backend version lookup off the start-up path: resolve it lazily, only when a
   run actually reports provenance, and never for a backend that was not selected.
2. Memoise `read_doc_with_links` per absolute path within a single process, so a doc
   reachable from two standards files is read and link-followed once rather than twice.
3. Keep the assembled text byte-identical: same document order, same `=== path ===`
   headers, same refusal for links that resolve outside the base directory.

## Risks

- Memoising by path is only safe within one process. Nothing may be written to disk, or
  the cache outlives the file it describes and a later run reviews stale standards.
- Lazy version lookup must not move the "backend binary is missing" error later than it
  is today; that check stays on the start-up path where it already is.

## Validation

- Run `rr --diff --backend codex,claude` before and after the change and confirm start-up
  feels snappier.
