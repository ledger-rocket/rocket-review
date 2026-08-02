"""Prompt arms: immutable prompt sets the paired runner compares against each other.

An arm is a directory of plain-text files, one per prompt constant in
`rocket_review/prompts.py`. It is input, never output: the runner loads it, hashes it, and
records the hash on every result row, so a row can always be traced back to the exact
prompt bytes that produced it.

Injection happens by rebinding those constants on the `rocket_review.prompts` module before
`rr`'s CLI runs — see `rr_arm_launcher.py`. That works because `get_prompt` reads the
constants out of module globals at call time; it is also why only the constants may be
rebound and not `get_prompt`/`build_agent_prompt` themselves, which the backend modules
import by value at import time.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

import rocket_review.prompts as rr_prompts

# The full prompt surface an arm has to supply. Order is canonical: the content hash
# concatenates in exactly this sequence, so it must not be reordered casually — doing so
# renames every existing arm's hash and breaks comparison with recorded results.
PROMPT_CONSTANTS = (
    "PLAN_REVIEW_PROMPT",
    "CODE_REVIEW_PROMPT",
    "DIFF_REVIEW_PROMPT",
    "PROJECT_STANDARDS_ADDENDUM",
    "JSON_OUTPUT_ADDENDUM",
)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

SUFFIX = ".txt"


class ArmError(Exception):
    """An arm directory does not describe a usable prompt set."""


@dataclass(frozen=True)
class Arm:
    name: str
    path: Path
    texts: dict[str, str]
    content_hash: str


def runtime_prompt_constants() -> tuple[str, ...]:
    """Every public string constant `rocket_review.prompts` currently defines.

    The drift guard: a prompt added to the runtime that no arm carries would be silently
    left un-injected, so every arm would still be running the live text for that mode and
    the comparison would quietly stop measuring what it claims to.
    """
    return tuple(
        name for name, value in vars(rr_prompts).items()
        if isinstance(value, str) and not name.startswith("_")
    )


def live_prompt_texts() -> dict[str, str]:
    """The prompt text compiled into the rocket-review importable here."""
    return {name: getattr(rr_prompts, name) for name in PROMPT_CONSTANTS}


def arm_hash(texts: dict[str, str]) -> str:
    """sha256 over a canonical concatenation of the arm's prompt text.

    Each part is length-prefixed so no two different arms can concatenate to the same
    bytes — without it, moving a line from the end of one prompt to the start of the next
    would hash identically.
    """
    digest = hashlib.sha256()
    for name in PROMPT_CONSTANTS:
        body = texts[name].encode("utf-8")
        digest.update(f"{name}:{len(body)}\n".encode())
        digest.update(body)
    return digest.hexdigest()


def load_arm(name_or_path: str | Path) -> Arm:
    """Load an arm by name (a directory under evals/prompts/) or by explicit path."""
    path = Path(name_or_path)
    if not path.is_absolute() and not path.exists():
        path = PROMPTS_DIR / str(name_or_path)
    if not path.is_dir():
        raise ArmError(f"arm {name_or_path!r} not found: {path} is not a directory")

    texts: dict[str, str] = {}
    missing: list[str] = []
    for name in PROMPT_CONSTANTS:
        file = path / f"{name}{SUFFIX}"
        if not file.is_file():
            missing.append(file.name)
            continue
        texts[name] = file.read_text(encoding="utf-8")
    if missing:
        raise ArmError(f"arm {path} is missing: {', '.join(missing)}")

    # An unrecognised prompt file means the arm was written against a different constant
    # set than this rocket-review has. Loading it anyway would inject a partial arm.
    extra = sorted(
        f.name for f in path.glob(f"*{SUFFIX}")
        if f.stem not in PROMPT_CONSTANTS
    )
    if extra:
        raise ArmError(
            f"arm {path} carries prompt files this rocket-review has no constant for: "
            f"{', '.join(extra)}"
        )

    return Arm(name=path.name, path=path, texts=texts, content_hash=arm_hash(texts))


def apply_arm(arm: Arm) -> None:
    """Rebind `rocket_review.prompts`' constants to this arm's text.

    Must run before anything assembles a prompt. `get_prompt` resolves the constants from
    module globals on every call, so rebinding them here reaches every backend without any
    runtime code being aware an eval is happening.
    """
    unknown = set(runtime_prompt_constants()) - set(PROMPT_CONSTANTS)
    if unknown:
        raise ArmError(
            "rocket_review.prompts defines constants no arm carries: "
            f"{', '.join(sorted(unknown))}. Add them to PROMPT_CONSTANTS and re-export "
            "every arm, or the comparison silently runs live text for those prompts."
        )
    for name, text in arm.texts.items():
        setattr(rr_prompts, name, text)


def export_arm(directory: Path) -> Arm:
    """Write the live prompt constants out as an arm directory."""
    directory.mkdir(parents=True, exist_ok=True)
    texts = live_prompt_texts()
    for name, text in texts.items():
        (directory / f"{name}{SUFFIX}").write_text(text, encoding="utf-8")
    return load_arm(directory)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "export":
        print("usage: python evals/arms.py export <arm-name>", file=sys.stderr)
        return 1
    arm = export_arm(PROMPTS_DIR / args[1])
    print(f"{arm.path}  {arm.content_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
