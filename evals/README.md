# evals — offline measurement for `rr`

Developer tooling, never installed and never run in CI. Two harnesses live here:

- **Format compliance** (`m0_sweep.py`) — how often backends really return
  `REVIEW_SCHEMA`-compliant `--json` output.
- **The paired prompt-arm harness** (`paired_runner.py`, `prompts/`, `cases/`, `tier1.py`,
  `adjudicate.py`) — whether a prompt change made `rr` sharper or just noisier, and
  whether it may ship.

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
beat. The runner says so on stderr rather than pretending it is a comparison, and both the
summary and `tier1.py` still report two groups — because **a run is identified by its arm's
role as well as its name**. That matters beyond A/A: two genuinely different arms can share
a directory basename (`--control current --treatment /tmp/experiment/current`), and keying
on the name alone would merge them into one set of numbers with nothing to say so.

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
- **`pre-prompt-rewrite/`** — the constants as of commit `41da0e8`, the last commit before the review
  prompts were rewritten. Frozen history; never re-exported.

Adding a prompt constant to `rocket_review/prompts.py` without adding it to every arm is
also caught: `PROMPT_CONSTANTS` is asserted against the constants the runtime actually
defines, and `apply_arm` refuses an arm that does not cover all of them. Without that guard
a new prompt would go un-injected, both arms would run the live text for it, and the
comparison would quietly stop being a comparison.

That guard is also why the surface is exactly five names. `get_prompt` composes a mode's
body with one output-format section, and the *prose* format sections are private to
`prompts.py`: a sixth public constant would need a file in every arm, and the frozen
historical arm cannot grow one without ceasing to be history. Nothing is lost, because
every measured run is `--json`, where the assembled prompt is the arm's mode body, its
`PROJECT_STANDARDS_ADDENDUM` when the case supplies docs, and its `JSON_OUTPUT_ADDENDUM` —
arm bytes throughout, with no live text anywhere in it. Asserted in `test_arms.py`.

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
id: b-999             # illustrative; the shipped ids run b-001…
mode: diff            # diff | code | plan — which prompt/mode this case exercises
source: mutant        # mutant | merged-pr | seeded-plan
diff: cases/b-999.patch     # mutant only; path relative to evals/
path: cases/b-999-plan.md   # seeded-plan only; path relative to evals/
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
  found when a diff frames it. There the patch is **committed inside the worktree** before
  the review: a backend under codex's read-only sandbox may still run `git diff HEAD`, and
  an uncommitted mutation would hand it exactly the diff this mode exists to withhold.
  Committing makes that diff empty, so the two modes differ in what the reviewer can see
  and not merely in which prompt ran. The path is passed repo-relative so findings cite the
  path the manifest and the file:line resolver use, not a throwaway worktree's absolute one.
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

**A case may only pin a `repo_commit` reachable from `main`.** This repo squash-merges, so a
commit that exists only on a feature branch is destroyed the moment that branch lands: after
the merge no clone has the object, the manifest-integrity tests fail for every case pinned to
it, and no mutant can be materialized at all. A case authored on a branch therefore pins the
`main` commit its patch applies to, never the branch tip it happened to be written against.
`test_cases.py` enforces this against `origin/main` (falling back to a local `main`), and
`ci.yml` checks out with `fetch-depth: 0` so the ancestry is actually there to check.

### Corpus B — injected defect mutants (recall)

20 cases over 17 distinct one-hunk mutations of `rocket_review/`; three of the seventeen are
expressed a second time as `code`-mode cases (same patch, different id) so the same defect
can be scored with and without a diff framing it. Across five runtime modules (`cli.py`,
`models.py`, `backends/base.py`, `backends/api.py`, `backends/claude.py`), the class labels
are:

| class | distinct mutants | what it names |
|-------|------------------|---------------|
| `dropped-guard` | 5 | a check removed outright, or left running but no longer excluding what it was written to exclude |
| `swallowed-error` | 4 | a failure that stops producing a failing outcome — returned instead of raised, or recorded and then not acted on |
| `flipped-comparison` | 2 | a comparison or exit-status test read the wrong way round |
| `off-by-one-bound` | 2 | a boundary shifted by one element or one line |
| `lost-diagnostic` | 1 | a failure still detected downstream, but reported with the wrong cause |
| `wrong-set-members` | 1 | a membership test naming the wrong members |
| `wrong-reducer` | 1 | a reduction over an ordering that selects the opposite end |
| `inverted-fallback-rank` | 1 | `b-001`, prior work |

Labels describe what the mutation *is*, not how it was authored, because recall is
aggregated by them: a label two mutants share only loosely would pool numbers about
different things. That is why the last four are singletons rather than one tidier bucket.

`dropped-guard`, `swallowed-error` and `lost-diagnostic` are adjacent enough that the
boundaries are worth stating rather than left to the labels. A mutant is **`swallowed-error`**
only when a failure of the review pipeline stops producing a failing outcome *end to end* —
the run comes back as a review, or the gate stops tripping. It is **`lost-diagnostic`** when
something downstream still catches the failure and the run still fails, but with the wrong
cause. And it is **`dropped-guard`** when what gets through is invalid input, an invalid
argument combination, or malformed model output.

Two cases decide those boundaries. `b-008` returns `""` on the timeout path, and `""` is a
value its callers already reject: claude and opencode re-detect it and fail, and codex — which
never reads the return value — fails on an empty output file. On every path the run still
fails and only the cause is lost, so it is labelled `lost-diagnostic`. The one path where that
does not hold is codex's output file when the killed process had already written part of it,
which surfaces a fragment as a complete review; the label is the conservative reading of a
mutant that is mostly diagnostic loss, not a claim that nothing can survive. Its sibling
`b-018` returns the child's real stdout on the exit-status path, which no caller rejects, so
there a failed backend comes back as a review by the ordinary route. `b-021` mutates a boolean
guard, which looks like `dropped-guard`, but the arm it removes is the errored-backend arm and
the outcome is a gate that exits 0 on a run that never completed — so it lands on the
`swallowed-error` side.

`dropped-guard` is the one class at the ≥5 bar, so what makes its members *independent* is
written out here rather than left to the label:

- the doc-link containment check (`cli.read_doc_with_links`, `b-002`); the exact-match git
  allow rule in the claude sandbox's tool allowlist (`b-003`); the verdict-validity check in
  `models.parse_backend_output` (`b-016`); the `--fail-on` requires `--json` argument check,
  whose removal turns the CI gate into a silent no-op (`b-017`); and the repo-containment
  check on the files named in the content under review (`api.extract_referenced_files`,
  `b-022`). Four modules, five checks, five different things getting through.

`b-002` and `b-022` are the same *flavour* — `is_relative_to` demoted to a string-prefix test
— at different sites, in different modules, on different attack surfaces. `b-002` is a
relative markdown link inside a doc passed to `--docs`/`--llms`. `b-022` runs on the content
under review before the API call is made (`api.py:205`), so its surface is any file-shaped
token in the diff, plan or piped input being reviewed — which is attacker-adjacent in a way
the doc path is not, since a diff can come from a branch nobody on the team wrote.
Independence is the rule and they pass it; flavour spread is a preference this pair does not
satisfy, which is recorded here rather than smoothed over.

`swallowed-error`'s four members are `cli.run_one`'s `BackendError` handler (`b-009`), where
the error is lost before it is ever recorded; `base.run_command`'s non-zero-exit arm (`b-018`);
`api._output_text`'s unfinished-response check (`b-020`), where the fragment of a truncated
review is returned as the whole of it; and `models.should_fail`'s errored-backend arm
(`b-021`). Four modules, four failures, four ways of not failing — one short of the bar, and
left short rather than padded.

Ids are never reused. A case retired during construction leaves its number behind rather than
freeing it, because the id keys every result row and a reused one would make two different
defects share a history — so the sequence can have gaps.

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

A kill is necessary but not sufficient, because a test can fail for a reason that is not a
defect. Two further rules, both learned by admitting a mutant that broke them:

- **The defect must be decided inside this repository at `repo_commit`.** A mutant whose
  consequence lives in an external tool's contract — which permission-mode string a CLI
  treats as deny-by-default, what an SDK does with a given argument — is not pinnable: the
  contract can change under a fixed `repo_commit`, the two spellings may already be aliases,
  and nothing in the corpus can settle it. Retired case `b-015` was exactly this, and its
  claimed sandbox bypass turned out to rest on an alias.
- **The kill must assert behaviour, not a spelling.** If the only failing assertion is
  `argv[i] == "<literal>"`, then every other value of that argument kills the mutant too,
  including values that behave identically — so the suite is not distinguishing a defect from
  a rename, and the `killed_by` proof proves nothing about behaviour.

The corpus is checked against both rules, not only new cases. `b-003`/`b-014` are the closest
surviving call: the consequence (a `:*` allow rule lets an injected `--output=<path>` through)
does lean on Claude Code's rule-matching semantics. Two things keep them:

- **The kill is a containment check, not an argv literal.** The assertion that fires is
  `"Bash(git diff HEAD)" in allow` — the exact rule must appear somewhere in the assembled
  `--allowedTools` string. Reordering the allowlist, adding a tool, or any other spelling that
  still carries the exact rule passes it, so it distinguishes a widened rule from a rewrite.
  (The test's companion conjunct, `"Bash(git diff:*)" not in allow`, does *not* fire on this
  mutant — the mutation produces `Bash(git diff HEAD:*)`, which that substring never matches.
  It guards a different widening, and the kill does not rest on it.)
- **The mutation appends a defined widening token**, so the mutated rule is a strict superset
  of the exact one and the mutant cannot be equivalent — no reading of the syntax makes
  `Bash(git diff HEAD:*)` narrower than or equal to `Bash(git diff HEAD)`.

The residual premise is that a bare `Bash(cmd)` rule is an exact match — otherwise `:*` would
be redundant everywhere in the permission syntax, not just here. That premise is external and
unpinnable, and it is the first thing to re-examine if Claude Code's rule syntax changes.

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
so: `plan-flaw` is two independent cases, well under the ≥5 the success criterion requires.
The one class that does reach five, `dropped-guard`, is a mutant class scored in `diff` and
`code` mode with no plan case in it — so nothing that ever clears the gate on this corpus
will say anything about the plan prompt.

## Running a paired sweep

```
python evals/paired_runner.py \
    --control pre-prompt-rewrite --treatment current \
    --backends codex:gpt-5.6-sol --runs 3 --timeout 900
```

Run protocol:

- **A model is mandatory.** Every `--backends` spec must be `name:model`, for the reason in
  *Model provenance* above: `rr` reports the model *argument*, and a backend on its own
  default reports `null`, which would leave rows describing nothing.
- **Arms alternate within each case, and the schedule guarantees it.** Odd repetitions run
  control then treatment, even repetitions run treatment then control (C,T,T,C,C,T,…) per
  case and backend. Each row records its `order_index` in that sequence, so the alternation
  can be checked after the fact instead of taken on trust.

  Exactly what is guaranteed: **both runs of a repetition are submitted together, and the
  next repetition of that case/backend starts only after both have finished.** Two runs of
  the same arm on one case therefore never overlap, and a repetition's control and treatment
  are always adjacent in time. Different cases (and different backends) still run
  concurrently, bounded by `--concurrency`; since a repetition is two runs, up to
  `concurrency // 2` repetitions are in flight, so `--concurrency 2` (the default) runs one
  repetition at a time and `--concurrency 4` runs two cases in parallel. Nothing beyond a
  repetition's own two runs is ever queued.
- **An interrupt stops the spending.** Every queued unit is a billed review, so a Ctrl-C or
  a worker failure cancels what has not started rather than letting the pool work through
  the backlog. Runs already in flight are bounded by their own timeout, and `rr` tears its
  backend down when interrupted.
- **Failures are retried once and never dropped.** A timeout or backend error is recorded as
  such and retried; both attempts are written. `tier1` scores the last attempt of each unit,
  and drops the whole repetition — both arms — if either arm never completed. See
  *Complete repetitions* below.
- **Every manifest's `repo_commit` is checked before anything is staged or spent.** It must
  be a full 40-character lowercase oid (never `HEAD`, a branch, or an abbreviation, none of
  which name a fixed snapshot) and must resolve to a commit in `--repo`.
- A run that outlives its timeout is torn down exactly as in Part 1 (SIGINT, ten-second
  grace, SIGKILL only as a last resort), using the same shared code.

### Output

One JSONL file per run in `evals/results/`, named `paired-<timestamp>-<random>.jsonl` and
created exclusively. The header line records a **`sweep_id`**, the harness rocket-review
version and the harness checkout's HEAD commit, the rocket-review the *selected interpreter*
would import and where it lives, the interpreter and launcher used, both arms with their
paths and hashes, backend specs and CLI versions, the repo, every case with its
mode/source/`repo_commit`/control flag, runs, timeout, concurrency, the alternation scheme
in words, and the retry limit.

Two rocket-review versions, because `--python` can select a different environment: one is
the harness's own, the other is what actually ran the reviews. The harness commit is
recorded because the released version string is constant across many source commits —
including every change to the prompts under test — so on its own it cannot say what a result
came from.

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
  so they are exempt rather than counted as unresolved. Two more things about the base it
  resolves against, `repo_commit`:
  - for a **mutant** case that is the *pre-patch* base, so a finding citing a line the patch
    appended past the original end of file would read as unresolvable. Mutants are
    line-for-line edits by construction, which keeps that rare — but it is why this is a
    hallucination tripwire and not an exact locator.
  - **seeded-plan cases are exempt entirely.** A plan is a standalone artifact, not a
    repository snapshot: its `repo_commit` is provenance, and the plan file exists at no
    commit at all. Resolving its citations would score every one of them a hallucination
    forever and drag the pooled rate down with them, so plan rows are left out of the
    denominator on the same principle as a finding that cites nothing.
- **DO-NOT-FLAG tripwire** — see below.
- **findings per run, split by severity** — n, mean, median, and range, which is what shows
  a prompt change trading a drop in noise for a drop in real findings. Severities the model
  invented land in an **`other`** bucket rather than falling out of the split: inventing a
  label is one of the things this harness exists to notice, and without the bucket the
  per-severity numbers would stop summing to the total.
- **CRITICAL+HIGH per run**, reported as its own distribution because the median and range
  of a sum cannot be recovered from the two severities separately. Printed twice: over all
  cases, and again over **clean controls only**. The second is the veto rule's input — the
  pooled figure includes defect cases, where a CRITICAL finding is the correct answer rather
  than the noise the veto bounds. Both are pre-adjudication: which of those findings are
  actually false positives is still a human call.

**Everything above is reported twice: pooled across an arm's cases, and per case.** The two
decision rules read different levels — the success criterion aggregates, the veto is written
per clean-control case — so the per-case breakdown ships with the metrics instead of being
re-derived by hand at the moment someone is deciding whether to ship a prompt. Each row of
the results file carries `case_is_control`, so the breakdown labels which rule applies
without needing the header.

### Complete repetitions

**A repetition scores only when both arms produced a review.** If either arm's run of a
`(case, backend, repetition)` failed after its retry — or is simply absent — the whole
repetition is dropped from *both* arms and reported separately, with the case, repetition
and reason named.

Excluding a failed run arm-by-arm instead would quietly desynchronise the two denominators:
the surviving arm keeps a repetition its partner never completed, so the arms stop being
compared on the same work. That is not a neutral loss — the cases that time out are the long,
noisy ones, so the bias has a direction.

### One sweep at a time

Every row carries the `sweep_id` of the session that produced it, and it is part of the key
that identifies both a run and a group. Two sessions produce rows with identical
case/backend/arm/repetition, so without it one would be read as a retry of the other, or the
two would pool across whatever changed in between — which is precisely the confound the
paired design exists to remove.

`tier1.py` therefore refuses a file containing more than one `sweep_id`.
`--allow-multiple-sweeps` overrides that with a loud warning, and even then the sweeps are
scored side by side and never merged.

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

### What this release can and cannot decide

> **The gate is live for `dropped-guard` only. Every other class is reported, never
> decided.**
>
> Both prerequisites of the success criterion are now met:
>
> - **Corpus depth.** The criterion needs a defect class with **≥5 independent cases**.
>   `dropped-guard` has five (listed case by case under *Corpus B*). It is the only class
>   that does: `swallowed-error` (4), `flipped-comparison` (2), `off-by-one-bound` (2),
>   `plan-flaw` (2), `lost-diagnostic` (1), `wrong-set-members` (1), `wrong-reducer` (1),
>   `inverted-fallback-rank` (1) are all below the bar and are **reported but cannot
>   certify**, whatever their numbers look like.
> - **A scorer.** `adjudicate.py` turns a committed artifact of human finding-decisions
>   into class recall, the veto and the success criterion, under the aggregation rules
>   written out below — repetition aggregation, twin dedup, veto arithmetic, case
>   invalidation. It is committed **before any decision sweep has run against it**, which
>   is the whole of what makes those rules pre-registered rather than chosen alongside the
>   numbers they produce.
>
> Two conditions on using it, both part of the pre-registration:
>
> - **The decision sweep runs at `--runs 5` or more.** The majority rule needs depth to mean
>   what it says, and five is the depth every threshold below was argued at. A sweep run
>   shallower may be read, but it does not certify anything — `adjudicate.py` enforces that
>   rather than trusting it (*Protocol depth*). It can still be **vetoed** at any depth.
> - **`adjudicate.py`'s verdict is binding.** CERTIFIED, NOT CERTIFIED or VETOED is the
>   answer, not an input to a judgement made afterwards. A rule that turns out to be wrong
>   is changed in a commit of its own, argued on its own merits, before the next sweep —
>   never in the same change as the results it would reinterpret.
>
> What the harness could already do is unchanged: establish the run-to-run noise floor
> (A/A), and apply the veto, which is computed on clean controls alone and is indifferent
> to how large any defect class is.
>
> **Two cases of the same mutant in different modes are not independent.** A `diff`-mode and
> a `code`-mode re-expression of one seeded defect is one case counted twice: the same bug,
> the same lines, the same reason a reviewer would or would not see it. Counting them as two
> toward the ≥5 threshold would let the gate be satisfied by re-encoding rather than by
> evidence. The class counts above are of distinct mutants; the corpus's three `code`-mode
> re-expressions are not among them.
>
> Five is the floor the rule names, not a comfortable sample, which is why the criterion is
> conjunctive — a relative improvement AND ≥2 additional defects. On five cases one case
> flipping is worth a large percentage and exactly one defect, so the absolute half of the
> rule refuses it however good the ratio looks.

### Veto — must hold

For each **clean-control** case, compare the mean count of CRITICAL and HIGH
*adjudicated-false-positive* findings across the N runs of each arm. A gated change ships
only if:

- on **every** clean-control case, the treatment mean exceeds the control mean by no more
  than **0.5 findings**; **and**
- **in aggregate** across clean-control cases, the treatment mean does not exceed the
  control mean at all.

`tier1.py` prints the pre-adjudication form of both numbers directly: `critical+high/run`
restricted to clean controls (the aggregate condition), and again per case with each case
labelled control or defect (the per-case condition). Neither has to be recombined by hand —
the only step left at decision time is the human adjudication itself. `adjudicate.py` then
computes the same two numbers over *adjudicated* false positives, which is the form the
rule is written in; see *Veto arithmetic* below.

A prompt change that finds more real defects while also inventing more high-severity noise
on clean code has not improved `rr`; it has moved the cost from missed bugs to wasted review
cycles, and that trade is refused here by construction.

### Success criterion — must also hold

Defect recall improves on at least one defect class that has **≥5 independent cases**, by
**≥20% relative AND ≥2 additional defects found**. Both conditions, not either.

- Classes with fewer than 5 cases are reported but **cannot certify** a change. They are too
  small to distinguish a real improvement from a run of luck. **`dropped-guard` is the only
  class at the bar today**, so it is the only class `adjudicate.py` gives verdict-grade
  treatment — see *What this release can and cannot decide* above.
- **Independent** means a separate defect, not a separate encoding of one. The same seeded
  mutant reviewed in `diff` mode and again in `code` mode counts once.
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

There is no third answer here, and `adjudicate.py` enforces that: on a clean control a
CRITICAL or HIGH finding is recorded as `false-positive` or as `case-invalidated`, and
nothing else. A "real but unrelated" call there would park a confirmed bug in the corpus as
neither noise nor a removal, which is exactly the gap a number could later be argued into.

## The adjudication artifact

One YAML file per sweep, holding the human calls the decision rules are made of. It is the
audit trail: every number `adjudicate.py` prints is a function of this file, the results
JSONL and the case manifests, so a verdict can be recomputed and disputed line by line.

```yaml
sweep_id: 9f3c1a2b4d5e6f708192a3b4c5d6e7f8   # must match the results file's sweep
results_file: paired-20260802T162519.864653Z-312692e3.jsonl   # provenance only
adjudicator: your-name
decisions:
  - case_id: b-002
    backend: codex
    arm: pre-prompt-rewrite
    arm_role: control
    rep: 1
    finding_index: 0            # position in the parsed findings array of that run
    decision: matches-defect
    rationale: names read_doc_with_links and says the prefix test lets ../ through
  - case_id: c-004
    backend: codex
    arm: current
    arm_role: treatment
    rep: 3
    finding_index: 1
    decision: false-positive
    rationale: claims a race the GIL makes impossible; the dict write is atomic
```

The key is `tier1.py`'s `unit_key` minus the sweep id (which the file carries once) plus the
finding's index, so a decision joins to exactly one finding of exactly one run. `arm_role` is
part of it for the same reason it is part of `unit_key`: an A/A run puts the same arm *name*
on both runs of a repetition, and keying on the name alone would silently merge them.

Four decisions, no more:

- **`matches-defect`** — rule 3 of the scoring rule: the finding's `why` describes the
  injected defect. Only valid on a defect case, and only on a finding that already satisfies
  rules 1 and 2; recording it on a finding that cites another file, or a line outside
  `defect.span`, is refused rather than counted. Rule 3 is a human call *within* the
  mechanical ones, never a way around them.
- **`real-unrelated`** — a genuine finding that is not the injected defect. Ignored, per the
  scoring rule: no recall credit and no false-positive debit.
- **`false-positive`** — a confirmed clean-control false positive. The veto's numerator, and
  refused on a defect case, where the rule ignores unrelated findings rather than debiting
  them.
- **`case-invalidated`** — the case does not measure what it claims to. Removes it from
  every certification number, both arms. It is recorded against the finding that revealed
  the problem, which is where the evidence is; a case no finding ever raised a question
  about is a corpus-construction matter for `verify_cases.py`, not an adjudication.

`rationale` is required and one line. Without it the artifact records that a call was made
but not what it was made on — which is the state this whole scorer exists to make
impossible, since rule 3 cannot be recomputed from anything else.

Artifacts live under `evals/adjudications/` and are **gitignored by default**: they quote
review text for real commits, exactly like `results/`. The path is always passed explicitly
with `--adjudications`, so nothing is picked up implicitly.

`adjudicate.py --pending` writes the skeleton — every finding the rules read, with `decision:
TODO`. The list is derived mechanically from the results, so emitting it decides nothing, and
`TODO` fails the enum check, so the skeleton can never become a default.

## Pre-registered aggregation rules

Committed before any decision sweep ran against them, for the reason at the top of *Decision
rules*: a denominator chosen after seeing results is not evidence.

### Repetition aggregation — majority, not any-run

A defect counts as **FOUND** by an arm on a case iff it was adjudicated `matches-defect` in
**more than half** of that arm's scored repetitions for that case — ≥3 of 5 at the protocol
depth.

Any-run would inflate recall with stochastic hits: at five repetitions a reviewer that
stumbles onto a defect one time in five scores identically to one that finds it every time,
and the difference between those two is the entire thing a prompt change is trying to move.
Majority measures what the reviewer reliably finds.

**Strictly more than half**, which decides the even-*n* case a lost repetition produces: 2 of
4 is **not** found. A tie is not reliable detection, and the burden of proof sits on the
claim that the reviewer finds the defect, not on the claim that it does not.

The denominator is the case's **complete repetitions** — the ones tier 1 scores. A repetition
that lost either arm leaves both, so the two arms are always majority-tested against the same
depth, and a sweep that lost repetitions shrinks its own denominators rather than comparing
four runs against five.

### Twin dedup — code-mode twins never certify

Two cases sharing a patch are one seeded mutation expressed twice. The **`diff`-mode**
expression is the one certification arithmetic counts; the other is excluded from class
recall entirely and reported in a separate *mode sensitivity* section, which is what it
actually is — evidence about whether a defect survives losing its diff framing, not a second
defect.

This is the same rule as the ≥5-independence count under *Corpus B*, applied to the
arithmetic rather than only to the corpus description. Counting a twin would let the gate be
cleared by re-encoding one mutant instead of by evidence.

Which case represents a mutation is decided from the **manifests** — `diff` mode wins, lowest
case id breaks a tie — and never from the results, so it cannot become a choice made after
seeing which expression scored better. Nor can an adjudication call move it: see *Case
invalidation*, which removes whole mutations for exactly that reason.

### Class eligibility

A class is verdict-grade only when it has **≥5 independent cases actually scored in the
sweep**, after twin dedup and after case invalidation, **and** every one of them was scored
at protocol depth (below). Every other class is computed and printed with its numbers, and
cannot produce a CERTIFIED verdict however good they look.

### Protocol depth

Certification requires **≥5 complete repetitions on every case it reads** — each case of the
certifying class, and each clean-control case the veto is computed over.

The majority rule is only the majority rule at depth. At n=1 a strict majority is one run,
so "found in a majority of repetitions" collapses into exactly the any-run rule that
*Repetition aggregation* rejects; at n=2 it takes both. The thresholds above were argued at
five, and a scorer that would apply them to a two-run sweep is a scorer whose depth is
chosen after the fact. So it is checked rather than assumed, and a shallow sweep is reported
with its numbers and refused a certification.

The check is deliberately **one-sided**: depth gates certifying, never blocking. A sweep
that trips the veto over two repetitions has still shown harm, and refusing to act on it
because the sample is thin would be failing in the wrong direction — the whole asymmetry of
*What this release can and cannot decide* is that this harness may block on less evidence
than it may certify on.

**At `--runs 5` there is no slack at all.** Five is a *floor on complete repetitions*, not a
target: a repetition that loses either arm leaves both (see *Complete repetitions*), so one
failed run on any scored case — a defect case of the certifying class or a clean control —
drops that case to four and voids the certification outright. It does not weaken the result;
it removes it. The long, noisy cases are exactly the ones that time out, so this is not a
remote possibility.

**Run decision sweeps at `--runs 7`.** Seven absorbs two lost repetitions and still clears
the floor, and of the depths worth considering it also has the most lenient majority bar: a
case scored at seven needs 4 of 7, where six needs 4 of 6 and five needs 3 of 5. Six buys one
repetition of slack and tightens the bar to do it. The floor stays at five because five is
the depth the thresholds above were argued at — going deeper costs tokens and buys
robustness, which is a budget decision rather than a rule, and each case is majority-tested
at the depth it actually reached rather than the depth that was requested.

### Veto arithmetic

Per clean-control case, the mean count of **adjudicated-false-positive** CRITICAL+HIGH
findings per run, per arm, over complete repetitions only — the same runs tier 1 scores, so
the two reports cannot disagree about which work happened. The change ships only if the
treatment mean exceeds the control mean by no more than **0.5** on **every** case, **and**
does not exceed it **at all** in aggregate (pooled findings over pooled runs across clean
controls).

Means are compared as exact rationals, not floats: a bound of exactly +0.5 is on the passing
side of "by no more than 0.5", and it should not depend on binary rounding whether it lands
there.

A run whose review did not parse contributes a **zero** to its arm's mean rather than
dropping out of the denominator: the runtime parser returns no findings for a schema
violation or a decode failure, so there are no findings to adjudicate and none are counted.
That dilutes the false-positive mean downward for whichever arm produced the unparsable
output. It is deliberate — tier 1's raw `critical+high/run` treats those runs identically,
and the two reports must not disagree about which runs happened — but it means an arm that
degrades into unparsable output looks quieter here, not noisier. `tier1.py`'s strict-valid
rate is where that shows up, and it is the number to read alongside a veto that passed
narrowly.

**An unmeasured veto is a failed veto.** A sweep with no clean-control repetition left is
VETOED, not certified — treating silence as a pass would let a change be certified by running
a sweep with the controls filtered out.

### Case invalidation

One `case-invalidated` decision removes the case from every number in both arms: out of the
veto if it is a control, out of its class's recall *denominator* if it is a defect case. The
class shrinks with it, and a class that drops below five independent cases stops being
verdict-grade — which is the honest consequence, not an edge case to smooth over.

**Invalidation lands on the mutation, not on one encoding of it.** A `diff`-mode case and its
`code`-mode twin are one seeded defect, so invalidating either removes both. That is the
honest reading of the rule — if the mutant's validity is disputed, every expression of it
falls — and it is also the only safe one. Removing just the named case would usually remove
the `diff`-mode representative, leaving the twin alone in its mutation group where the
representative rule elects it: the class would keep its case count and silently substitute
the numbers of the very encoding *Twin dedup* exists to hold out. One adjudication call
could then turn a NOT CERTIFIED sweep into a CERTIFIED one without a single finding
changing.

Because the removal is that consequential, an invalidation on a defect case has to be
recorded against a finding that satisfies scoring rules 1 and 2 — a finding about the seeded
defect. The claim is that the defect is not a defect, so it is made where the evidence for
it is.

Worth naming rather than trusting nobody notices: **invalidation is a general lever on the
arithmetic**, not only the twin hole above. Dropping a case the control found and the
treatment missed raises the treatment's gain; dropping one both arms found shrinks the
baseline the ≥20% is measured against. Two things bound it, and neither is a matter of
taste — the class still needs ≥5 independent cases afterwards, so invalidations run out of
room quickly, and every one on a defect case must be recorded against a rule-1-and-2 finding
with a written rationale in a committed artifact. The incentive exists; the audit trail is
the answer to it.

### Zero baselines and the criterion itself

`≥20%` relative **and** `≥2` absolute, both, exactly as the success criterion is written
above. A class whose control recall is zero is judged on the absolute condition alone, since
a relative improvement on zero is undefined rather than infinite.

### More than one backend

Each backend in the sweep is scored on its own. A veto anywhere vetoes the sweep, and
certification needs **every** backend to certify: a prompt ships to all of them at once, so
one backend getting sharper while another gets noisier is not an improvement to `rr`.

### The completeness gate

Nothing is scored until every finding a rule reads has a recorded call — every CRITICAL/HIGH
finding on a surviving clean control, and every finding on a surviving defect case that
already satisfies rules 1 and 2. The scorer refuses and lists them.

Partial adjudication is the softest version of the failure this whole design is about: the
findings a human has not looked at yet are exactly the ones whose treatment could be settled
by what the totals need. Findings outside those two sets — a MEDIUM on a control, a finding
citing an unrelated file on a defect case — need no call, because no rule reads them.

## Running the scorer

```
mkdir -p evals/adjudications
python evals/adjudicate.py evals/results/paired-<...>.jsonl --pending \
    > evals/adjudications/<sweep>.yaml         # draft, then fill every TODO in
python evals/adjudicate.py evals/results/paired-<...>.jsonl \
    --adjudications evals/adjudications/<sweep>.yaml
```

It reads stored output only, calls no backend and opens no git object; the verdict is a pure
function of the three input files. The report prints the veto with its per-case detail, class
recall per arm with each case's hit count, the mode-sensitivity twins, the success criterion,
and one final line: **CERTIFIED**, **NOT CERTIFIED** or **VETOED**, with the reasons.

Exit status carries the outcome, and **nothing that is not a computed verdict is allowed to
land on a verdict code**:

- `0` CERTIFIED, `1` NOT CERTIFIED, `2` VETOED — the three verdicts, and only those;
- `3` no verdict could be computed: unreadable input, an incomplete adjudication, a sweep
  where no repetition survived;
- `4` `--pending` drafted a work list. Drafting decides nothing, so it must not exit 0;
- `64` a usage error, and `--help`. Both of argparse's own exits are verdicts here — 2 on a
  bad argument is VETOED, 0 after printing help is CERTIFIED — so the parser is overridden
  and neither can reach one. A caller gating a prompt change on exit status must not read a
  mistyped flag as a blocked change or a help screen as a certified one.

A sweep that could not be scored, a sweep that was refused, and a command that was never
run are three different outcomes, and a caller that could not tell them apart would read one
as another.

The report also prints a non-blocking **warning** when identical finding text — same title,
same `why` — was adjudicated differently depending on which arm produced it. That is not
necessarily wrong: the same sentence can be right about one diff and wrong about another.
But the arm role is on screen while the call is made, and an adjudicator reading the
control's finding as noise and the treatment's word-for-word identical finding as real is
the shape a thumb on the scale makes. Printed, so it is at least seen.

A file holding more than one `sweep_id` is refused outright, with no override. `tier1.py` has
one because it only *describes* sweeps and can print two side by side; a certification is one
decision about one paired session, and there is no honest way to make it out of measurements
taken against different conditions.

## Caveats

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
  `repo_commit` is a full oid naming a commit reachable from `main`, every patch parses and
  touches the file its manifest scores against, every referenced path exists at that commit,
  every span both ends inside its file and overlaps a hunk the patch actually changed, every
  mutant carries a `killed_by` naming test files that exist there, and no clean control has
  grown a defect block. All of it reads the git object database; nothing is checked out —
  which is why `ci.yml`'s test job needs `fetch-depth: 0`.
- `test_paired_runner.py` — the injection proof, arm alternation, and complete paired runs
  against a stub `codex` binary: result-file shape, per-row provenance, the retry path, CI
  refusal, and worktree teardown.
- `test_tier1.py` — every tier-1 metric on fixed rows, plus the hand-labelled DO-NOT-FLAG
  fixture.
- `test_adjudicate.py` — the certification arithmetic on synthetic sweeps: the majority rule
  including the even-*n* split a lost repetition produces, twin exclusion, mutation-level
  invalidation (including that invalidating a representative cannot promote its twin into
  the class), the protocol-depth gate in both directions, per-backend combination, every
  artifact validation and rule refusal, the completeness gate, the exit-status separation,
  and two full end-to-end sweeps through the CLI — one that certifies and one whose
  identical recall is vetoed by the noise it was bought with.
