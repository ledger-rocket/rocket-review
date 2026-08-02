Byte-exact export of the `rocket_review/prompts.py` constants at HEAD. `test_arms.py`
asserts it still matches the live constants, so editing the runtime prompts without
re-exporting (`python evals/arms.py export current`) fails CI.
