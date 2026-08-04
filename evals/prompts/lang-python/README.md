The `current/` arm at commit `6f8ad78`, plus one insertion and nothing else: a block of
Python-specific checks in the diff body. It is the treatment for the language-checks
experiment — does naming the defects a Python diff can carry find more of them without
adding noise to clean diffs. Frozen input: never re-exported, `test_arms.py` pins its hash
and asserts every file is `current/` plus that one block, and `rocket_review/prompts.py`
stays as it is unless a paired sweep certifies this arm.

Python only, and `diff` mode only, because that is the whole of what this corpus can
measure. Every corpus-B mutant patches `rocket_review/*.py`; the clean controls change
Python, YAML, TOML and Markdown (and `LICENSE`); no `.go`, `.sql` or `.sh` file appears
anywhere in the corpus. A Python block can therefore be scored for recall on the mutants
and for noise on the controls, and a Go, SQL or shell block could be scored for neither —
it would be text that shipped without a single case having run it. The other languages are
absent as a measurement constraint, not an oversight, and the same corpus fact is why the
block goes into the diff body alone: `diff` is the only mode with clean controls to run the
veto against. See *Promotion scope* in `evals/README.md`.

This arm carries the block on every run. The shipping feature it stands for would append
the identical bytes only when a review touches a Python file, and which of those two forms
a certification licenses is pre-registered in `evals/README.md` (*The language-checks arm*)
before any sweep exists to argue about. That section also names the class a gain may be
certified on — `swallowed-error`, three of whose six mutants are `except` bodies this
block's first line is about — and records that a `dropped-guard` gain is reported and
cannot certify, because no member of that class instantiates a line of this block.

Placement. The block closes the numbered REVIEW FOCUS list and precedes SEVERITY LEVELS. It
is a list of checks, so its home is the region naming what a reviewer hunts for, not the
region defining how a finding is labelled; and it follows the general categories rather
than leading them, so its entries read as instances a Python diff can supply instead of as
a rival list. Above REVIEW FOCUS the specifics would frame the general categories, which
inverts what each is for; below SEVERITY LEVELS a check would sit after the instruction the
prompt closes on.

Every entry is a defect a reader can see in the diff itself, and none is on the prompt's
DO-NOT-FLAG list — no formatting, imports, naming or annotations. What each adds over the
text already there differs by entry, and is worth stating exactly:

- the swallowed `except`, the unreleased resource and the `eval`/`exec`/SQL sink each have
  a category-level parent in REVIEW FOCUS — incomplete error handling (2), resource leaks
  (6), injection (5). They name a construct where the prompt names a category, which is a
  specification rather than a restatement; the injection entry is the closest of the three
  to redundant, since its parent already names the vulnerability class by name.
- the mutable default and the `assert` that `python -O` deletes have no counterpart in the
  prompt at any level.

The heading says *change*, not *file*, on purpose: the body's first line confines the
review to what the diff changes, and a block inviting a sweep of every touched Python file
would contradict it — and would make any measured effect partly a scope widening rather
than these five checks.

The `assert` entry says *outside the tests* for a related reason. In a test an `assert` is
the correct construct, and the controls this corpus can charge it against are
test-dominated — `c-004` is almost entirely `tests/`, `c-006` carries about 1.5k test
lines — so an unscoped entry would spend its whole false-positive budget on the one place
the pattern is not a defect. Nothing in this corpus can earn recall on it either (no mutant
instantiates it), which is why the scoping costs nothing: it can only remove noise.

Licensing. No GSD technique is adapted here; the entries are ordinary Python defect
knowledge. The gate is run anyway, because everything added to an arm goes through it: each
added line was compared word-run by word-run against the whole `gsd-build/get-shit-done`
corpus at `bdcaab2`, and the longest run any of them shares with it anywhere is two words.
