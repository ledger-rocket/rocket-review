# evals — offline measurement for `rr`

Developer tooling, never installed and never run in CI. Two harnesses live here:

- **Format compliance** (`m0_sweep.py`) — how often backends really return
  `REVIEW_SCHEMA`-compliant `--json` output.
- **The paired prompt-arm harness** (`paired_runner.py`, `prompts/`, `cases/`, `tier1.py`)
  — whether a prompt change made `rr` sharper or just noisier.

Both spend real tokens. A third script, `verify_cases.py`, spends none but runs the
project's test suite once per case; it is what proves the defect corpus is made of real
defects. See *The corpora*.

All three refuse to start when `CI` is set in the environment, and nothing in
`.github/workflows/` invokes any of them. Only the test modules run in CI, and none
of them launches a backend. They need the `dev` extra, which is where `jsonschema` and
`PyYAML` live — runtime `dependencies` stays empty on purpose, and no runtime path reads
YAML or validates strictly.

Shared plumbing (subprocess launch and timeout teardown, backend-spec parsing, envelope
extraction, result-file naming) lives in `eval_common.py` so the two runners cannot drift
apart on the parts that are subtle.

---

# Part 1 — backend JSON format compliance

## What this measures, and why

`rr --json` hands every backend the same `REVIEW_SCHEMA` (`rocket_review/models.py`) and
asks for a single JSON object. What comes back is read by
`rocket_review.models.parse_backend_output`, which is deliberately lenient: it checks the
verdict, that `findings` is a list, and each finding's `severity`/`title`, then coerces the
rest. Extra properties, invented severity labels, `null` where a string was promised and
missing `why`/`fix` fields all pass. That leniency is right for the runtime — a usable
review shouldn't be thrown away over a stray key — but it means the CLI cannot tell you how
often backends actually comply.

This answers that separately and offline. It changes nothing at runtime; it only re-reads
output the CLI already produced, against the same schema object the backends were sent.

## The four outcomes

- **`valid`** — decodes as JSON and passes strict `jsonschema` validation against
  `REVIEW_SCHEMA`.
- **`schema_violation`** — decodes as JSON but fails validation. The record keeps the first
  five validation messages, each prefixed with its JSON path.
- **`decode_failure`** — no JSON object could be recovered at all (a refusal, prose, a
  truncated response). The record keeps a 400-character excerpt so a human can triage what
  happened; nothing here guesses at the cause.
- **`backend_error`** — the run never produced output that can be judged: timeout, non-zero
  exit, crash, malformed envelope. Only a runner can observe this, so only a runner records
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

---

# Part 2 — the paired prompt-arm harness

## What it compares, and why it is paired

Prompt edits are easy to make and hard to verify. Reviews are non-deterministic and models
change under you, so "the prompt looks better and the reviews seem sharper" is not evidence
of anything. This harness answers one question: did a prompt change make `rr` sharper, or
just noisier?

A **control** prompt set and a **treatment** prompt set are run on identical cases, with the
same backend and the same model, alternating within each case, in one session. Never one arm
today and the other next week — that would confound the prompt change with every other thing
that moved in between, including the model itself.

Running the *same* arm as both control and treatment is supported and useful: it measures
the backend's own run-to-run noise floor, which is the number every real comparison has to
beat. The runner says so on stderr rather than pretending it is a comparison.

## Prompt arms

An arm is a directory under `evals/prompts/<name>/` holding one plain-text file per prompt
constant in `rocket_review/prompts.py`:

```
evals/prompts/current/
  PLAN_REVIEW_PROMPT.txt
  CODE_REVIEW_PROMPT.txt
  DIFF_REVIEW_PROMPT.txt
  PROJECT_STANDARDS_ADDENDUM.txt
  JSON_OUTPUT_ADDENDUM.txt
  README.md            <- one line naming where this arm came from
```

An arm is immutable input. The runner loads it, content-hashes it (sha256 over a canonical,
length-prefixed concatenation in a fixed constant order), and writes that hash on **every**
result row, so any row can be traced back to the exact prompt bytes that produced it.

Two arms ship:

- **`current/`** — a byte-exact export of the constants at HEAD. `test_arms.py` asserts it
  still matches the live `prompts.py`, so editing the runtime prompts without re-exporting
  (`python evals/arms.py export current`) fails CI rather than silently comparing against
  stale text. The export writes a complete, loadable arm: it drops files for constants the
  runtime no longer has, and writes a provenance README stub for a new arm without ever
  overwriting one that is already filled in.
- **`pre-m3a/`** — the constants as of commit `41da0e8`, the last commit before the review
  prompts were rewritten. Frozen history; never re-exported.

Adding a prompt constant to `rocket_review/prompts.py` without adding it to every arm is
also caught: `PROMPT_CONSTANTS` is asserted against the constants the runtime actually
defines, and `apply_arm` refuses an arm that does not cover all of them. Without that guard
a new prompt would go un-injected, both arms would run the live text for it, and the
comparison would quietly stop being a comparison.

## The injection seam

`rr` assembles prompts internally, and no runtime code has any notion of an eval. The
harness makes a backend see an arm's text by launching

```
python evals/rr_arm_launcher.py <normal rr arguments>     # RR_EVAL_ARM=<arm dir>
```

The launcher rebinds the prompt constants on the imported `rocket_review.prompts` module and
then calls `rocket_review.cli.main`. Everything downstream — source materialization,
`ReviewJob`, the backend module, the subprocess, the `--json` envelope — is the production
code path, unmodified.

Three properties make this the right seam:

- **It works at all.** `get_prompt` resolves the constants from module globals on every
  call, so rebinding them reaches every backend. Rebinding `get_prompt` or
  `build_agent_prompt` themselves would *not* work: the backend modules import those by
  value at import time.
- **Arms cannot leak into each other.** The rebinding is module-global state, so one
  interpreter can only ever hold one arm. Each run is its own process and the runner's
  concurrency is process-level.
- **Provenance is airtight.** The prompts patched are those of the `rocket_review` importable
  by the launching interpreter, which is also the `rr` that runs. `rr` on PATH is
  deliberately never used, so injecting into an installation the arm was not applied to is
  not merely unsupported, it is unrepresentable. `--python` selects the interpreter, and that
  choice selects both.

`test_paired_runner.py` proves it end to end: a stub executable named `codex`, first on
PATH, records the exact prompt bytes `rr` hands it, and the test asserts the arm's marker
text is present and the live prompt text is *absent* — substitution, not addition.

## Case manifests

One YAML file per case under `evals/cases/`. The file name is the case id.

```yaml
id: b-017
mode: diff            # diff | code | plan — which prompt/mode this case exercises
source: mutant        # mutant | merged-pr | seeded-plan
diff: cases/b-017.patch     # mutant only; path relative to evals/
path: cases/b-017-plan.md   # seeded-plan only; path relative to evals/
repo_commit: <oid>    # exact snapshot the case is defined against
defect:               # present for defect-bearing cases only (corpus B and seeded plans)
  class: dropped-guard
  file: rocket_review/models.py
  span: [84, 91]      # inclusive line range a correct finding must overlap
  expected: one-line description of the injected defect
killed_by:            # mutants only; written by verify_cases.py, never by hand
  - tests/test_models.py::test_should_fail_threshold
```

A case with no `defect` is a **clean control**; that absence is the whole definition.

How each source is materialized, and why:

- **`mutant`** — a detached `git worktree` at `repo_commit` with the patch applied to the
  working tree. In `diff` mode it is reviewed with `rr --diff`. That is the faithful shape:
  the agentic backends are told to run `git diff HEAD` and can navigate the whole snapshot
  around the change, exactly as on a real uncommitted edit. Feeding the patch on stdin
  instead would hand every backend the same bytes but strip the repository context `rr`'s
  primary mode depends on, so a prompt change affecting repo navigation would not show up
  at all. In `code` mode the same worktree is reviewed as `rr <defect.file>` — the whole
  file, with nothing pointing at the change — which is what asks whether a defect is only
  found when a diff frames it. The path is passed repo-relative so findings cite the path
  the manifest and the file:line resolver use, not a throwaway worktree's absolute path.
- **`merged-pr`** — `rr --commit <oid>` in the repo itself. A commit is immutable, so this
  is already reproducible without a worktree, and it exercises rr's `git show` path.
- **`seeded-plan`** (and standalone `code` files) — reviewed as an ordinary file argument
  from the checkout. A plan is a standalone artifact rather than a repository snapshot, so
  `repo_commit` is provenance for it rather than something to check out.

One worktree per case is created up front, serially — `git worktree add` mutates
repo-level admin state — and then shared by every run of that case. That sharing is not
quite read-only: the backends review under read-only sandboxes, but `rr --diff` runs
`git diff HEAD`, and git refreshes the index and takes `index.lock` to do it, so two
concurrent runs of the same case can collide on **git's own writes** even with perfectly
behaved backends. The retry absorbs that; a worktree per run would mean hundreds of
checkouts for no other gain. Worktrees are torn down in a `finally`, including when a
patch fails to apply, and a removal that fails says so on stderr rather than leaving admin
state to be discovered later.

`rr --mode` is always passed explicitly from the manifest rather than left to
auto-detection, so the manifest decides which prompt constant the arm is measured on.

## The corpora

Every case is sourced from **this repository only**. It is public, so nothing from another
codebase can be committed here; that bounds how varied the corpus can be, and the caveats at
the end of this file are written with that in mind.

### Corpus B — injected defect mutants (recall)

14 cases over 11 distinct one-hunk mutations of `rocket_review/`; three of the eleven are
expressed a second time as `code`-mode cases (same patch, different id) so the same defect
can be scored with and without a diff framing it. Six class labels — `dropped-guard`,
`flipped-comparison`, `off-by-one-bound`, `swallowed-error`, `wrong-variable`, two distinct
mutants each, plus `b-001`'s singleton `inverted-fallback-rank` — across five runtime
modules (`cli.py`, `models.py`, `backends/base.py`, `backends/api.py`, `backends/claude.py`).

**A mutant is admitted only if the project's own test suite kills it**, and the proof is
mechanical:

```
python evals/verify_cases.py            # check the committed corpus
python evals/verify_cases.py --write    # record what killed each mutant
```

It checks out `repo_commit` in a throwaway worktree, applies the patch, runs the suite, and
writes the failing node ids to the manifest's `killed_by`. It first runs the suite unpatched
at each `repo_commit` and refuses to judge anything if that is not green — against a red
baseline every "kill" could be the pre-existing failure. A mutant nothing fails on is an
**equivalent mutant**: the code still behaves correctly, so it is not a defect and scoring a
reviewer on finding it would measure nothing. Those candidates are dropped rather than
weakened into cases.

The script is minutes of worktrees and full suite runs, so it is developer tooling, not a CI
gate. What CI enforces is the cheap half: `test_cases.py` asserts every mutant manifest
*carries* a `killed_by` naming tests that exist at its `repo_commit`, that `defect.file` is a
file the patch actually touches, and that `defect.span` ends inside that file. Those catch a
manifest that drifted from its patch; only `verify_cases.py` can tell you the patch is still
a real defect.

### Corpus C — clean controls (false positives)

Six merged, never-reverted commits from this repo's own history, reviewed with
`rr --commit <oid>`. Sizes span a 13-line docs change to a 23-file, ~3.7k-line feature —
deliberately, because a corpus of only small controls would measure the false-positive rate
only where a reviewer has an easy time. The large one (`c-006`) is also the expensive one:
M0 data has large diffs running 300–600s per review, so it sits near the default 900s
timeout and wants `--timeout` raised rather than its timeouts read as backend failures.

Every commit here is squash-merged, so `repo_commit` is the merge commit itself and
`git show <oid>` is the whole PR diff; there is no separate parent to review against.

### Plan set

Four cases: two seeded-flaw plans (`class: plan-flaw` — one step depending on something no
step creates, one plan whose only success criterion cannot fail) and two controls. The
seeded flaws are planted in plans that are otherwise deliberately sound, so a reviewer has
to read for the flaw rather than for general weakness. `p-001` predates the designed set and
is a pipeline smoke case rather than a scoring control; read it as such.

Four cases cannot certify anything about plan-mode prompts, and the decision rules below say
so — no defect class reaches the ≥5 independent cases the success criterion requires, in the
plan set or anywhere else in this corpus.

## Running a paired sweep

```
python evals/paired_runner.py \
    --control pre-m3a --treatment current \
    --backends codex:gpt-5.6-sol --runs 3 --timeout 900
```

Run protocol:

- **A model is mandatory.** Every `--backends` spec must be `name:model`, for the reason in
  *Model provenance* above: `rr` reports the model *argument*, and a backend on its own
  default reports `null`, which would leave rows describing nothing.
- **Arms alternate within each case.** Odd repetitions run control then treatment, even
  repetitions run treatment then control (C,T,T,C,C,T,…) per case and backend. Each row
  records its `order_index` in that sequence, so the alternation can be checked after the
  fact instead of taken on trust. Default `--concurrency 2` — one slot per arm — so a
  repetition's two runs face the same backend conditions rather than starting minutes apart.
  Above 2, interleaving relaxes to scheduling order; the pairing (same cases, same session,
  equal repetitions) is what carries the design.
- **Failures are retried once and never dropped.** A timeout or backend error is recorded as
  such and retried; both attempts are written. `tier1` scores the last attempt of each unit
  and excludes a unit still failing after its retry from every metric denominator, reporting
  it under `runs_failed`.
- A run that outlives its timeout is torn down exactly as in Part 1 (SIGINT, ten-second
  grace, SIGKILL only as a last resort), using the same shared code.

### Output

One JSONL file per run in `evals/results/`, named `paired-<timestamp>-<random>.jsonl` and
created exclusively. The header line records the harness rocket-review version, the
interpreter and launcher used, both arms with their paths and hashes, backend specs and CLI
versions, the repo, every case with its mode/source/`repo_commit`/control flag, runs,
timeout, concurrency, the alternation scheme in words, and the retry limit.

Every subsequent line is one attempt: case id, mode, source, `repo_commit`, whether the case
is a clean control, arm name, role and hash, backend, requested model, backend CLI version,
harness rocket-review version, repetition, `order_index`, attempt number, the exact command,
the working directory, exit code, wall time, the raw review text, and the strict validator's
outcome with its errors or excerpt. The versions and the control flag are on every row as
well as in the header, because rows get filtered and concatenated across files and a row
that cannot say what produced it is not evidence of anything.

## Tier-1 metrics

```
python evals/tier1.py evals/results/paired-<...>.jsonl
```

Computed from the stored JSONL and the case's `repo_commit`. No backend is called, nothing
is re-run, and every number is deterministic given the file. Per backend and arm:

- **strict-valid rate** — the Part 1 validator applied to each run's raw output.
- **file:line resolution** — the share of line-bearing findings whose cited file exists at
  `repo_commit` and whose line falls inside that file. Findings with a null `file` or `line`
  — or a `file` that is not even a string, which a schema-violating review can produce
  because the runtime parser passes that field through uncoerced — make no locatable claim,
  so they are exempt rather than counted as unresolved. Resolution is against `repo_commit`,
  which for a mutant case is the *pre-patch* base: a finding citing a line the patch appended
  past the original end of file would read as unresolvable. Mutants are line-for-line edits
  by construction, which keeps that rare — but it is why this is a hallucination tripwire and
  not an exact locator.
- **DO-NOT-FLAG tripwire** — see below.
- **findings per run, split by severity** — n, mean, median, and range, which is what shows
  a prompt change trading a drop in noise for a drop in real findings. Severities the model
  invented land in an **`other`** bucket rather than falling out of the split: inventing a
  label is one of the things this harness exists to notice, and without the bucket the
  per-severity numbers would stop summing to the total.
- **CRITICAL+HIGH per run**, reported as its own distribution because the median and range
  of a sum cannot be recovered from the two severities separately. This is the veto rule's
  input, before a human has adjudicated which of those findings are false positives.

**Everything above is reported twice: pooled across an arm's cases, and per case.** The two
decision rules read different levels — the success criterion aggregates, the veto is written
per clean-control case — so the per-case breakdown ships with the metrics instead of being
re-derived by hand at the moment someone is deciding whether to ship a prompt. Each row of
the results file carries `case_is_control`, so the breakdown labels which rule applies
without needing the header.

An empty distribution reports `None` for mean, median and range, never `0` — same rule as
the rates. A zero would read as "measured, and it was zero".

### The DO-NOT-FLAG tripwire

The review prompts list things the model must not raise (formatting, import ordering, quote
style, naming, missing annotations, docs-only). `tier1.py` carries a small keyword/regex
taxonomy for those six categories and reports the share of findings matching it.

**This is a heuristic tripwire, not a classifier and not a verdict.** It has no notion of
whether a finding is correct. A jump in the rate between two arms means *go and read the
findings*; it never on its own says anything about either arm's quality.

Two design decisions keep it honest, both validated by
`evals/fixtures/do_not_flag_labels.json`, a small hand-labelled set of finding titles that
`test_tier1.py` asserts the taxonomy reproduces exactly:

- **Only the title is classified.** The title is the claim a finding makes; the same words
  inside `why` are usually part of a real argument — "stripping trailing whitespace would
  corrupt the patch" is a correctness point, not a style nit.
- **Bare nouns are not enough where a qualifier separates the two readings.** "Missing
  semicolon" is a lint nit; "semicolon-separated PATH entries" is a claim about behaviour.
  Same for trailing commas, and for annotations: an *absent* annotation is on the
  DO-NOT-FLAG list, a *wrong* one is a real finding.

The residual false-positive mode is known and deliberate: a genuine finding whose *title*
happens to say "trailing whitespace" or "indentation" will trip the wire. Read the rate as a
regression signal on prompt discipline, never as a count of bad findings.

## Decision rules

**These rules are pre-registered.** They are committed here before any real sweep has been
executed against them, so that the threshold for shipping a prompt change cannot be chosen
after seeing which threshold the change happens to clear. Changing a rule is a commit of its
own, argued on its own merits, made before the sweep it governs — never in the same change
as the results it would reinterpret.

### Veto — must hold

For each **clean-control** case, compare the mean count of CRITICAL and HIGH
*adjudicated-false-positive* findings across the N runs of each arm. A gated change ships
only if:

- on **every** clean-control case, the treatment mean exceeds the control mean by no more
  than **0.5 findings**; **and**
- **in aggregate** across clean-control cases, the treatment mean does not exceed the
  control mean at all.

`tier1.py` prints the pre-adjudication form of both numbers directly — `critical+high/run`
per arm and again per case, with each case labelled control or defect — so the only step
left at decision time is the human adjudication itself.

A prompt change that finds more real defects while also inventing more high-severity noise
on clean code has not improved `rr`; it has moved the cost from missed bugs to wasted review
cycles, and that trade is refused here by construction.

### Success criterion — must also hold

Defect recall improves on at least one defect class that has **≥5 independent cases**, by
**≥20% relative AND ≥2 additional defects found**. Both conditions, not either.

- Classes with fewer than 5 cases are reported but **cannot certify** a change. They are too
  small to distinguish a real improvement from a run of luck.
- A class whose control recall is zero uses the absolute condition alone (≥2 additional
  defects found), since a relative improvement on zero is undefined rather than infinite.

### Scoring rule — when a finding matches a defect

A finding counts as detecting a manifest's defect only when **all three** hold:

1. the finding's cited `file` equals the manifest's `defect.file`; **and**
2. the finding's `line` overlaps `defect.span` — or, for a finding with a null line, the
   evidence it quotes lies within that span; **and**
3. the finding's `why` describes the injected defect rather than something else at the same
   location.

The third check is a **recorded human call**, made by reading the finding against
`defect.expected`, and the call is written down with the result. It cannot be automated
without building the very judgement being measured.

Unrelated-but-real findings on a defect case are **ignored**: no recall credit, and no
false-positive debit either. The case was constructed to test one thing, and the rest of the
diff was never adjudicated.

### Clean-control adjudication

A CRITICAL or HIGH finding on a clean control is a false positive **only after human
confirmation**. If adjudication finds it is a genuinely real problem, the case is not a clean
control and is **removed from the corpus** — retroactively, for both arms, in every metric.
Leaving it in would punish whichever arm was better at finding real bugs.

### Caveats

At the corpus sizes this harness is designed for (~15–20 cases), it detects **large effects
only**. It is a regression guard on DO-NOT-FLAG discipline and false-positive rate, not a
precision instrument, and it cannot resolve small differences in review quality at all. A
result that "looks slightly better" is not a result. Every number is also specific to one
model version at one moment: an arm comparison is valid only within the session that
produced it, which is why both arms always run together.

---

## Tests

Everything under `evals/` is collected by the repo's normal `pytest -q` run, and none of it
launches a real backend.

- `test_strict_validator.py` — the validator against fixtures only (compliant review, extra
  property, invalid severity, missing required field, non-JSON prose, fenced JSON).
- `test_m0_sweep.py` — envelope extraction (including the malformed shapes that must degrade
  to a record rather than take a sweep down), backend-spec parsing, the summary counts, and
  `run_case` end to end against a generated stub `rr`.
- `test_arms.py` — the `current/` drift guard, the constant-coverage guard, hash stability
  and sensitivity, arm loading errors, and that applying an arm actually changes what
  `get_prompt` and `build_agent_prompt` return.
- `test_cases.py` — manifest validation, materialization of all three source types against a
  throwaway git repository (including that a patch which does not apply fails loudly and
  leaves no worktree behind), and the integrity of the shipped corpus itself: every case's
  `repo_commit` is a full oid naming a commit in this repo, every patch parses and touches
  the file its manifest scores against, every referenced path exists at that commit, every
  span ends inside its file, every mutant carries a `killed_by`, and no clean control has
  grown a defect block. All of it reads the git object database; nothing is checked out.
- `test_paired_runner.py` — the injection proof, arm alternation, and complete paired runs
  against a stub `codex` binary: result-file shape, per-row provenance, the retry path, CI
  refusal, and worktree teardown.
- `test_tier1.py` — every tier-1 metric on fixed rows, plus the hand-labelled DO-NOT-FLAG
  fixture.
