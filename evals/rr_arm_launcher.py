"""Run rr's own CLI with one prompt arm's text substituted for the built-in prompts.

This is the injection seam. The paired runner never calls a backend itself; it launches

    python evals/rr_arm_launcher.py <normal rr arguments>

with `RR_EVAL_ARM` naming an arm directory. The launcher rebinds the prompt constants on
`rocket_review.prompts` and then hands control to `rocket_review.cli.main`, so everything
downstream — source materialization, `ReviewJob`, the backend module, the subprocess, the
`--json` envelope — is the production code path, unmodified and unaware.

One process per run is what makes this safe: the rebinding is module-global state, so two
arms could not share an interpreter. The runner's concurrency is therefore process-level.

`rr` on PATH is deliberately not used. The prompts patched here belong to the
`rocket_review` importable by *this* interpreter, so injecting into a different
installation is not merely unsupported, it is unrepresentable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arms import ArmError, apply_arm, load_arm  # noqa: E402

ARM_ENV = "RR_EVAL_ARM"


def main() -> int:
    arm_dir = os.environ.get(ARM_ENV)
    if not arm_dir:
        print(
            f"Error: {ARM_ENV} is not set. This launcher exists to run rr under a named "
            "prompt arm; without one it would just be a slower `rr`.",
            file=sys.stderr,
        )
        return 1
    try:
        apply_arm(load_arm(Path(arm_dir)))
    except ArmError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Imported after patching only for symmetry with the ordering requirement; the
    # constants are read per call, so import order is not what makes this work.
    from rocket_review.cli import main as rr_main

    # `or 0` rather than a bare call: rr's main currently signals failure by raising
    # SystemExit and returns None on success, but if it ever returns a status instead,
    # discarding it here would report every failed review as a clean exit.
    return rr_main() or 0


if __name__ == "__main__":
    sys.exit(main())
