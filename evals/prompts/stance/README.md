The `current/` arm at commit `285ec3b`, plus two insertions and nothing else: a STANCE
paragraph in the code and diff bodies, and a list of weak-review patterns in all three. It
is the treatment for the adversarial-stance experiment — does presuming defects and naming
the ways a reviewer goes soft make `rr` sharper without making it noisier. Frozen input:
never re-exported, `test_arms.py` pins its hash and asserts every file is `current/` plus
insertions only, and `rocket_review/prompts.py` stays as it is unless a paired sweep
certifies this arm.

Promotion scope, pre-registered before any sweep. A CERTIFIED verdict on this arm licenses
promoting the `DIFF_REVIEW_PROMPT` changes to `rocket_review/prompts.py` and nothing else.
The corpus can only certify `diff` mode — the one class at the ≥5 bar has all five of its
independent cases there, and `code` mode has no clean controls to run the veto against — so
the code and plan blocks stay eval-only until their own modes have certifying evidence.
`get_prompt` composes one body per mode with no cross-mode interaction, which is what makes
the partial promotion exact rather than a compromise: every mode then runs text that was
either measured or left alone. See *Promotion scope* in `evals/README.md`.

Placement. STANCE follows the role paragraph directly, because it qualifies who the
reviewer is rather than what a finding must carry; placed after the evidence rule instead
it would separate the posture from the role it modifies and read as a rider on evidence.
The weak-pattern list closes each body's numbered methodology (REVIEW APPROACH, REVIEW
FOCUS, REVIEW METHODOLOGY) and precedes the severity block, which puts all three files on
one shape: the method, then how the method is failed, then what a finding must look like.
The severity bullet lands within a screen of the severity definitions it refers to.

Licensing. Both blocks adapt GSD's adversarial-review technique (see
THIRD_PARTY_NOTICES.md), so every added sentence was compared word-run by word-run against
the whole `gsd-build/get-shit-done` corpus and reworded until the longest run shared with
it anywhere was two words — technique adapted, no wording reproduced. The borrowing runs
deeper than the idea of a stance: which soft-failure modes the lists name, and the order
they come in, follow GSD's own enumeration. The expression here is this project's.
