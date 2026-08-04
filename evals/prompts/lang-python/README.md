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
before any sweep exists to argue about.

Placement. The block closes the numbered REVIEW FOCUS list and precedes SEVERITY LEVELS. It
is a list of checks, so its home is the region naming what a reviewer hunts for, not the
region defining how a finding is labelled; and it follows the general categories rather
than leading them, so its entries read as instances a Python diff can supply instead of as
a rival list. Above REVIEW FOCUS the specifics would frame the general categories, which
inverts what each is for; below SEVERITY LEVELS a check would sit after the instruction the
prompt closes on.

Every entry is a defect a reader can see in the diff itself, and a Python form that the
general categories reach only in the abstract: an `except` that hides its failure, a
mutable default, a resource freed on no path, an `eval`/`exec`/SQL sink, an `assert`
carrying validation that `python -O` deletes. None of them restates something the prompt
already says, and none is on its DO-NOT-FLAG list — no formatting, imports, naming or
annotations.

Licensing. No GSD technique is adapted here; the entries are ordinary Python defect
knowledge. The gate is run anyway, because everything added to an arm goes through it: each
added line was compared word-run by word-run against the whole `gsd-build/get-shit-done`
corpus at `bdcaab2`, and the longest run any of them shares with it anywhere is two words.
