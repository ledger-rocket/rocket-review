# evals — backend JSON format compliance

## What this measures, and why

`rr --json` hands every backend the same `REVIEW_SCHEMA` (`rocket_review/models.py`) and
asks for a single JSON object. What comes back is read by
`rocket_review.models.parse_backend_output`, which is deliberately lenient: it checks the
verdict, that `findings` is a list, and each finding's `severity`/`title`, then coerces the
rest. Extra properties, invented severity labels, `null` where a string was promised and
missing `why`/`fix` fields all pass. That leniency is right for the runtime — a usable
review shouldn't be thrown away over a stray key — but it means the CLI cannot tell you how
often backends actually comply.

This directory answers that separately and offline. It changes nothing at runtime; it only
re-reads output the CLI already produced, against the same schema object the backends were
sent.

## The four outcomes

- **`valid`** — decodes as JSON and passes strict `jsonschema` validation against
  `REVIEW_SCHEMA`.
- **`schema_violation`** — decodes as JSON but fails validation. The record keeps the first
  five validation messages, each prefixed with its JSON path.
- **`decode_failure`** — no JSON object could be recovered at all (a refusal, prose, a
  truncated response). The record keeps a 400-character excerpt so a human can triage what
  happened; nothing here guesses at the cause.
- **`backend_error`** — the run never produced output that can be judged: timeout, non-zero
  exit, crash, malformed envelope. Only the sweep runner can observe this, so only it records
  it; the validator never returns it.

**Exit status is authoritative.** `rr` prints its envelope *before* exiting non-zero when a
backend fails, so stdout for a failed run can look complete. Any non-zero exit is
`backend_error` regardless of what was on stdout — otherwise a run that did not succeed could
be scored `valid`.

Two details worth knowing when reading results:

- A response wrapped in a ` ```json ` fence or in prose is unwrapped with the runtime's own
  `extract_json` before validation, so it is *not* counted as a `decode_failure` — the
  measurement is about schema compliance, not fence-stripping. The `bare_json` field on each
  record is `false` for those, which is how you find backends that ignore the "no markdown
  fence" instruction while otherwise complying.
- `decode_failure` is judged after that same unwrapping, so it means the runtime parser
  would have failed too.

## Where the raw output comes from

The sweep runs `rr --commit <sha> --backend <name>:<model> --json --full` and validates
`results[].raw` from the envelope. That field is the exact string
`parse_backend_output` was handed, which makes it the most faithful available view of what
the backend produced — anything reconstructed from the parsed `findings` would already have
been normalised by the very leniency being measured.

`--full` is what makes this work: without it `rr` truncates `raw` at 4000 characters, and a
truncated review is unparsable by construction, so every long review would score as a
`decode_failure` caused by the harness rather than the backend.

## Running a sweep

```
python evals/m0_sweep.py --commits 4923a44,dd56c0d --backends codex:gpt-5.6-sol \
    --runs 3 --timeout 900
```

`--backends` is required and every spec must be `name:model` — see *Model provenance* below.
One model per backend per sweep; run separate sweeps to compare two models of the same
backend, since case ids and the summary both key on the backend name.

Other defaults: five representative commits from this repo's history, 3 runs each, a 900s
per-run timeout passed through to `rr`, and up to 3 parallel `rr` subprocesses
(`--concurrency`).

**Every case is a real, billed backend review.** The script refuses to start when `CI` is
set in the environment, and nothing in `.github/workflows/` invokes it. Only the two test
modules run in CI, and neither launches a backend.

A run that outlives its timeout is stopped with SIGINT, not SIGKILL. `rr` starts each backend
CLI in its own process group and only tears those groups down from its own interrupt handler,
so killing `rr` outright would leave a backend running — and billing — with nothing left to
stop it. The harness gives `rr` ten seconds to do that cleanup before killing it as a last
resort, and says so in the record when it comes to that.

### Model provenance

The recorded `requested_model` is the model *asked for*, not the one that answered. `rr`'s
envelope reports the model argument it was given, and a backend left on its own default
reports `null` — codex, for instance, resolves its default from `~/.codex/config.toml`
internally, where neither `rr` nor this harness can see it. Requiring an explicit model per
backend is what keeps the field meaningful; recording the genuinely resolved model would need
`rr` to report it in the envelope, which is out of scope here.

`--rr` overrides the command used to invoke rocket-review. It is meant for stubs or another
`rr` in this same environment: `REVIEW_SCHEMA` and the recorded `harness_rr_version` both come
from the rocket-review importable *here*, so pointing `--rr` at a different installation
leaves the results with unverified schema provenance — they may have been validated against a
schema the executed `rr` never sent.

### Output

One JSONL file per sweep in `evals/results/`, named with a microsecond timestamp and a random
suffix and created exclusively, so two sweeps started in the same second cannot truncate each
other's results. The first line is a header (harness rocket-review version, backend specs,
backend CLI versions, commits, runs, timeout, concurrency); every line after it is one run:
case id, backend, requested model, the exact command, exit code, wall time, the raw output,
and the validator outcome with its errors or excerpt.

`results/` is gitignored and never committed — the records embed full review text for real
commits, and they are measurements of a specific model version at a specific time rather
than anything a reader of this repo should treat as current.

The script also prints a per-backend summary:

```
backend             valid  schema_violation    decode_failure     backend_error    fenced/wrapped  total
--------------------------------------------------------------------------------------------------------
codex                  15                 0                 0                 0                 2     15
```

The first four columns are the outcomes, and they are mutually exclusive. **`fenced/wrapped`
is not** — it counts the `valid` and `schema_violation` runs whose JSON had to be dug out of a
markdown fence or surrounding prose, and it is the reason the summary is not just four
numbers. The prompt says "no prose before or after it, no markdown fence"; a backend that
fences every response still scores 100% `valid`, because the object inside is compliant. The
example above reads as: perfect schema compliance, but two runs ignored the format
instruction.

`decode_failure` and `backend_error` runs are excluded from that count: they never produced a
JSON object, so their `bare_json` is a default rather than an observation, and counting them
would report a refusal as a formatting problem.

## The decision this feeds

If the strict-valid rate is at or near 100% for the backends people actually use, then the
prompt/format layer is not what limits evaluation work, and reorganising it is ordinary
cleanup that can be scheduled on its own merits. A materially lower rate — or one backend
far behind the others — means format compliance is a real variable, and any evaluation built
on `--json` output has to account for it before the results mean anything.

## Tests

`evals/test_strict_validator.py` covers the validator with fixtures only (compliant review,
extra property, invalid severity, missing required field, non-JSON prose, fenced JSON).
`evals/test_m0_sweep.py` covers envelope extraction (including the malformed shapes that must
degrade to a record rather than take the sweep down), backend-spec parsing, the summary
counts, and `run_case` end to end against a generated stub `rr` — no real backend is ever
launched. Both modules are collected by the repo's normal `pytest -q` run. They require the
`dev` extra, which is where `jsonschema` lives — runtime `dependencies` stays empty on
purpose.
