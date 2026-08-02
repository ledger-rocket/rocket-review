The `rocket_review/prompts.py` constants as of commit `41da0e8`, the last commit before
the review prompts were rewritten to drop their self-contradictory instructions. Frozen
history: this arm is never re-exported, and the drift test that guards `current/` does not
apply to it.
