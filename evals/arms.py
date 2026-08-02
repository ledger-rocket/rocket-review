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
# renames every existing arm's hash and breaks comparison with recorded results. Adding or
# renaming a name is heavier still: every shipped arm needs a matching file, and the frozen
# historical arm cannot grow one. That is why `rocket_review.prompts` keeps its prose
# format sections private — under `--json`, which is how every measured run is made, the
# assembled prompt is the arm's mode body plus the arm's JSON section and nothing else.
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
    # Absolute from here on. The arm's path is handed to a child process whose cwd is a
    # case's worktree, so a relative one would be resolved somewhere this process never was.
    path = path.resolve()

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
    # Symmetric on purpose. An *extra* runtime constant would run live text for that prompt
    # in both arms, so the comparison would quietly stop covering it. A *missing* one means
    # this interpreter's rocket_review is not the version the arm was written for — likely
    # a --python pointing at another environment — and setattr would happily create a
    # constant nothing reads, so every run would look fine and measure the wrong prompts.
    runtime = set(runtime_prompt_constants())
    expected = set(PROMPT_CONSTANTS)
    if runtime != expected:
        problems = []
        if missing := sorted(expected - runtime):
            problems.append(f"missing from this rocket_review: {', '.join(missing)}")
        if unknown := sorted(runtime - expected):
            problems.append(f"present but carried by no arm: {', '.join(unknown)}")
        raise ArmError(
            "rocket_review.prompts does not match the prompt surface arms are written "
            f"against ({'; '.join(problems)}). Re-export every arm against this "
            "rocket_review, or point --python at the one they were built for."
        )
    for name, text in arm.texts.items():
        setattr(rr_prompts, name, text)


PROVENANCE_STUB = """\
TODO: replace this line with where this arm came from — the commit its prompts were taken
from, or what change it represents. Every shipped arm is required to document that.
"""


def export_arm(directory: Path) -> Arm:
    """Write the live prompt constants out as a complete, loadable arm directory."""
    directory.mkdir(parents=True, exist_ok=True)
    texts = live_prompt_texts()
    for name, text in texts.items():
        (directory / f"{name}{SUFFIX}").write_text(text, encoding="utf-8")
    # A renamed or removed prompt constant leaves its old file behind, and load_arm rejects
    # an arm carrying a file it has no constant for — so the export would produce an arm
    # that cannot be loaded.
    for stale in directory.glob(f"*{SUFFIX}"):
        if stale.stem not in PROMPT_CONSTANTS:
            stale.unlink()
    # Never overwritten: re-exporting an arm must not silently discard its provenance.
    readme = directory / "README.md"
    if not readme.exists():
        readme.write_text(PROVENANCE_STUB, encoding="utf-8")
    return load_arm(directory)


def arm_directory(name: str) -> Path:
    """Turn an arm name into its directory under evals/prompts/, or refuse.

    A single path component only. `export_arm` deletes files it considers stale, so a name
    like `../..` or an absolute path would let a typo remove *.txt somewhere outside the
    arm store entirely.
    """
    if name != Path(name).name or name in ("", ".", ".."):
        raise ArmError(
            f"arm name {name!r} must be a single directory name under {PROMPTS_DIR}, "
            "not a path"
        )
    return PROMPTS_DIR / name


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "export":
        print("usage: python evals/arms.py export <arm-name>", file=sys.stderr)
        return 1
    try:
        arm = export_arm(arm_directory(args[1]))
    except ArmError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"{arm.path}  {arm.content_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
