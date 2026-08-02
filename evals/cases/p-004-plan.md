# Plan: break the `--json` severity counts down per backend

## Goal

`--json` findings already carry `backend` and `model`, but `summary.by_severity` pools
every backend together. On a cross-model run there is no way to see from the summary alone
that one backend produced every critical finding. Add a per-backend breakdown beside the
pooled one, changing nothing that exists.

## Steps

1. Add `summary.by_backend`: a mapping from backend name to the same `{severity: count}`
   shape `by_severity` already uses, built in `to_envelope` from the same `findings` list
   the pooled counts are built from.
2. Include every backend that ran as a key, with explicit zeros, so a backend that errored
   or found nothing is visibly present rather than silently absent — the same rule
   `by_severity` already follows for severities.
3. Leave `summary.by_severity`, `summary.worst_severity` and the `gate` block untouched.
   This is an additive optional field, which by the envelope's own stated contract does
   not bump `schema_version`.
4. Document the field in README.md beside `by_severity`, including that its per-backend
   counts sum to the pooled ones.

## Risks

- A consumer that rejects unknown keys would break on the new field. The envelope contract
  already states that consumers must tolerate unknown keys and that additive optional
  fields do not bump the version, so this is a documented-additive change rather than a
  silent one — but the risk is real for any consumer that ignored the contract.

## Validation

- Unit test: two backends with findings at different severities produce a `by_backend`
  whose per-backend counts sum, severity by severity, to `by_severity`.
- Unit test: a backend that errored appears in `by_backend` with all-zero counts.
- Unit test: the existing golden-envelope shape test is extended with the new key and
  still asserts `schema_version == 1`.
- Unit test: `should_fail` returns the same result as before for the same findings.
