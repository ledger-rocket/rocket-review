"""Arm loading, hashing, and the guards that keep an arm from silently going stale."""

from pathlib import Path

import pytest
import rocket_review.prompts as rr_prompts
from arms import (
    PROMPT_CONSTANTS,
    PROMPTS_DIR,
    ArmError,
    apply_arm,
    arm_directory,
    arm_hash,
    export_arm,
    live_prompt_texts,
    load_arm,
    runtime_prompt_constants,
)
from conftest import (
    PYTHON_CHECKS_HEADING,
    PYTHON_CHECKS_MARKER,
    STANCE_MARKER,
    WEAK_PATTERNS_HEADING,
    WEAK_PLAN_PATTERNS_HEADING,
)


@pytest.fixture
def restore_prompts():
    """apply_arm rebinds module globals; leaving them rebound would poison other tests."""
    saved = {name: getattr(rr_prompts, name) for name in PROMPT_CONSTANTS}
    yield
    for name, text in saved.items():
        setattr(rr_prompts, name, text)


def write_arm(directory, **overrides):
    directory.mkdir(parents=True, exist_ok=True)
    for name in PROMPT_CONSTANTS:
        (directory / f"{name}.txt").write_text(
            overrides.get(name, f"body of {name}\n"), encoding="utf-8"
        )
    return directory


# --- the shipped arms ----------------------------------------------------------------


def test_current_arm_is_byte_identical_to_the_live_prompts():
    # The drift guard. Editing rocket_review/prompts.py without re-exporting would leave
    # every "current" comparison quietly measuring last week's prompts.
    arm = load_arm("current")
    assert arm.texts == live_prompt_texts(), (
        "evals/prompts/current/ is stale; regenerate with "
        "`python evals/arms.py export current`"
    )


#: Computed from the checked-in arm at the commit that froze it. Pinned, not recomputed:
#: this arm is history, and history that can be edited without a test noticing is not a
#: baseline. Every result row records this hash, so a silent edit would retroactively
#: change what past sweeps claim to have measured.
PRE_PROMPT_REWRITE_HASH = (
    "ce5e1a84e5f21131508f374ac977494889c0a4bcddebf56f1b3884f445bac953"
)


def test_the_historical_arm_is_frozen():
    assert load_arm("pre-prompt-rewrite").content_hash == PRE_PROMPT_REWRITE_HASH


def test_pre_prompt_rewrite_arm_loads_and_differs_from_current():
    pre = load_arm("pre-prompt-rewrite")
    assert pre.content_hash != load_arm("current").content_hash
    assert set(pre.texts) == set(PROMPT_CONSTANTS)


#: Same reasoning as above, for the treatment of the adversarial-stance experiment. It is
#: pinned from the day it is written rather than once it has history: an arm whose text can
#: move while a sweep is running is not a treatment, it is two treatments sharing a name.
STANCE_HASH = (
    "f2719b21a68e95de9ca04cb3a24022bb69c971d6ba43cf36c8516de12107f65f"
)


def test_the_stance_arm_is_frozen():
    assert load_arm("stance").content_hash == STANCE_HASH


#: `current/`'s hash at 285ec3b — the text `stance/` was derived from. Pinned as a number
#: rather than read from `current/`, because `current/` tracks HEAD: the next legitimate
#: prompt edit moves it, and a purity check written against a moving base would fail on a
#: change that has nothing to do with this arm.
STANCE_BASE_HASH = (
    "14e3dcae19cb2a5da07bfe4ffda2d578082145acf757a7ff51eda5f3a52ca12e"
)

#: What `stance/` adds, by the heading of each inserted paragraph. Removing exactly these
#: has to give back the base — so a paragraph added, moved between bodies, or left
#: unregistered here is caught, and so is any edit to the text around them.
STANCE_INSERTIONS = {
    "CODE_REVIEW_PROMPT": ("STANCE", WEAK_PATTERNS_HEADING),
    "DIFF_REVIEW_PROMPT": ("STANCE", WEAK_PATTERNS_HEADING),
    "PLAN_REVIEW_PROMPT": (WEAK_PLAN_PATTERNS_HEADING,),
}


def without_paragraph(text: str, heading: str) -> str:
    """Drop the one blank-line-delimited paragraph whose first line starts with `heading`."""
    paragraphs = text.split("\n\n")
    kept = [p for p in paragraphs if not p.split("\n", 1)[0].startswith(heading)]
    assert len(kept) == len(paragraphs) - 1, (
        f"{heading!r} heads {len(paragraphs) - len(kept)} paragraphs, expected exactly 1"
    )
    return "\n\n".join(kept)


def test_the_stance_arm_inherits_its_base_text_unchanged():
    """Strip the registered insertions and what is left must hash to `current/` at 285ec3b.

    What the experiment rests on: the arm is meant to isolate two added blocks, so an edit
    anywhere else — a reworded principle, a dropped bullet — would make a measured
    difference unattributable to the thing under test. Reconstructing the base rather than
    diffing against today's `current/` keeps that guarantee from expiring the next time the
    live prompts legitimately change.
    """
    texts = dict(load_arm("stance").texts)
    for name, headings in STANCE_INSERTIONS.items():
        for heading in headings:
            texts[name] = without_paragraph(texts[name], heading)
    assert arm_hash(texts) == STANCE_BASE_HASH, (
        "stance/ no longer reduces to current/ at 285ec3b: text outside the registered "
        "insertions was edited, or an insertion is missing from STANCE_INSERTIONS"
    )


def test_the_stance_arm_carries_the_two_blocks_where_the_experiment_expects_them():
    # Which body gets which block is the design: the plan reviewer is not reviewing code,
    # so a stance about defective code would not apply to it.
    stance = load_arm("stance")
    for name in ("CODE_REVIEW_PROMPT", "DIFF_REVIEW_PROMPT"):
        assert STANCE_MARKER in stance.texts[name]
        assert WEAK_PATTERNS_HEADING in stance.texts[name]
        assert WEAK_PLAN_PATTERNS_HEADING not in stance.texts[name]
    plan = stance.texts["PLAN_REVIEW_PROMPT"]
    assert STANCE_MARKER not in plan
    assert WEAK_PLAN_PATTERNS_HEADING in plan
    # The plan list is its own text, not the code one under a longer heading.
    assert WEAK_PATTERNS_HEADING not in plan


#: Same reasoning again, for the treatment of the language-checks experiment: pinned from
#: the day it is written, because an arm whose text can move while a sweep is running is
#: not a treatment.
LANG_PYTHON_HASH = (
    "1cbd6a59627bfc680a80bd499694fb1c412883f12a819217386d4a3e1b3df6a9"
)


def test_the_lang_python_arm_is_frozen():
    assert load_arm("lang-python").content_hash == LANG_PYTHON_HASH


#: `current/`'s hash at 6f8ad78, the text `lang-python/` was derived from. Equal to
#: STANCE_BASE_HASH because the live prompts have not moved since 285ec3b; pinned as its
#: own number anyway, since two arms sharing a base by coincidence is no reason for one
#: arm's purity check to be written in terms of another's.
LANG_PYTHON_BASE_HASH = (
    "14e3dcae19cb2a5da07bfe4ffda2d578082145acf757a7ff51eda5f3a52ca12e"
)


def test_the_lang_python_arm_inherits_its_base_text_unchanged():
    """Strip the one inserted paragraph and what is left must hash to `current/` at 6f8ad78.

    The pure-insertion proof for the whole arm rather than for the diff body alone: the
    hash spans all five prompts, so a byte edited in any of the other four fails here too.
    """
    texts = dict(load_arm("lang-python").texts)
    texts["DIFF_REVIEW_PROMPT"] = without_paragraph(
        texts["DIFF_REVIEW_PROMPT"], PYTHON_CHECKS_HEADING
    )
    assert arm_hash(texts) == LANG_PYTHON_BASE_HASH, (
        "lang-python/ no longer reduces to current/ at 6f8ad78: text outside the inserted "
        "block was edited, or the block is no longer a single paragraph"
    )


def test_the_lang_python_arm_carries_its_block_in_the_diff_body_alone():
    # `diff` is the only mode this corpus can score the block in, so the same block in the
    # code or plan body would be text no case can speak to — see *The language-checks arm*
    # in evals/README.md.
    arm = load_arm("lang-python")
    body = arm.texts["DIFF_REVIEW_PROMPT"]
    assert PYTHON_CHECKS_HEADING in body
    assert PYTHON_CHECKS_MARKER in body
    for name in ("CODE_REVIEW_PROMPT", "PLAN_REVIEW_PROMPT"):
        assert PYTHON_CHECKS_HEADING not in arm.texts[name]
    # The seat: after the general checks, before the severity block the prompt closes on.
    assert (
        body.index("REVIEW FOCUS")
        < body.index(PYTHON_CHECKS_HEADING)
        < body.index("SEVERITY LEVELS")
    )


def test_every_shipped_arm_documents_its_provenance():
    for directory in sorted(p for p in PROMPTS_DIR.iterdir() if p.is_dir()):
        assert (directory / "README.md").is_file(), directory


# --- constant coverage ---------------------------------------------------------------


def test_prompt_constants_cover_every_runtime_constant():
    # A prompt added to rocket_review without a matching arm file would never be injected,
    # so both arms would run the live text for it and the comparison would stop being one.
    assert set(runtime_prompt_constants()) == set(PROMPT_CONSTANTS)


def test_apply_arm_refuses_when_the_runtime_grew_a_constant(tmp_path, monkeypatch,
                                                            restore_prompts):
    monkeypatch.setattr(rr_prompts, "NEW_MODE_PROMPT", "invented", raising=False)
    with pytest.raises(ArmError, match="present but carried by no arm: NEW_MODE_PROMPT"):
        apply_arm(load_arm(write_arm(tmp_path / "arm")))


def test_apply_arm_refuses_when_the_runtime_is_missing_a_constant(tmp_path, monkeypatch,
                                                                  restore_prompts):
    # A --python pointing at an older rocket_review. setattr would happily invent the
    # constant, nothing would read it, and the sweep would look fine while measuring the
    # wrong prompts — so the check has to run in both directions.
    monkeypatch.delattr(rr_prompts, "DIFF_REVIEW_PROMPT")
    with pytest.raises(ArmError, match="missing from this rocket_review: DIFF_REVIEW_PROMPT"):
        apply_arm(load_arm(write_arm(tmp_path / "arm")))


def test_an_arm_path_is_absolute_after_loading(tmp_path, monkeypatch):
    # The path is handed to a child whose cwd is a case's worktree, so a relative one
    # would be resolved somewhere this process never was.
    write_arm(tmp_path / "arm")
    monkeypatch.chdir(tmp_path)
    assert load_arm(Path("arm")).path.is_absolute()


# --- hashing -------------------------------------------------------------------------


def test_hash_is_stable_across_loads(tmp_path):
    write_arm(tmp_path / "arm")
    assert load_arm(tmp_path / "arm").content_hash == load_arm(tmp_path / "arm").content_hash


def test_hash_changes_when_any_prompt_changes(tmp_path):
    baseline = load_arm(write_arm(tmp_path / "a")).content_hash
    changed = load_arm(write_arm(tmp_path / "b", DIFF_REVIEW_PROMPT="different\n"))
    assert changed.content_hash != baseline


def test_hash_is_not_fooled_by_moving_text_between_prompts():
    # Why each part is length-prefixed: a bare concatenation would hash these identically.
    texts = dict.fromkeys(PROMPT_CONSTANTS, "")
    left = {**texts, "PLAN_REVIEW_PROMPT": "ab", "CODE_REVIEW_PROMPT": ""}
    right = {**texts, "PLAN_REVIEW_PROMPT": "a", "CODE_REVIEW_PROMPT": "b"}
    assert arm_hash(left) != arm_hash(right)


# --- loading errors ------------------------------------------------------------------


def test_missing_prompt_file_is_rejected(tmp_path):
    arm = write_arm(tmp_path / "arm")
    (arm / "DIFF_REVIEW_PROMPT.txt").unlink()
    with pytest.raises(ArmError, match="missing: DIFF_REVIEW_PROMPT.txt"):
        load_arm(arm)


def test_unknown_prompt_file_is_rejected(tmp_path):
    arm = write_arm(tmp_path / "arm")
    (arm / "REVIEW_OF_REVIEWS_PROMPT.txt").write_text("stray", encoding="utf-8")
    with pytest.raises(ArmError, match="no constant for"):
        load_arm(arm)


def test_missing_directory_is_rejected(tmp_path):
    with pytest.raises(ArmError, match="not found"):
        load_arm(tmp_path / "nope")


def test_non_prompt_files_are_ignored(tmp_path):
    arm = write_arm(tmp_path / "arm")
    (arm / "README.md").write_text("provenance", encoding="utf-8")
    assert load_arm(arm).texts.keys() == set(PROMPT_CONSTANTS)


# --- application ---------------------------------------------------------------------


def test_apply_arm_changes_what_get_prompt_returns(tmp_path, restore_prompts):
    arm = load_arm(write_arm(tmp_path / "arm", DIFF_REVIEW_PROMPT="ARM DIFF TEXT\n"))
    apply_arm(arm)
    # The mode body is the arm's; get_prompt then appends one output-format section.
    assert rr_prompts.get_prompt("diff").startswith("ARM DIFF TEXT\n")
    # Under --json, which is how every measured run is made, the assembled prompt is
    # entirely the arm's: its body plus its JSON section, with no live text in between.
    assert rr_prompts.get_prompt("diff", json_output=True) == (
        "ARM DIFF TEXT\n" + arm.texts["JSON_OUTPUT_ADDENDUM"]
    )


def test_apply_arm_reaches_build_agent_prompt(tmp_path, restore_prompts):
    from rocket_review.backends.base import ReviewJob

    apply_arm(load_arm(write_arm(tmp_path / "arm", PLAN_REVIEW_PROMPT="ARM PLAN TEXT\n")))
    job = ReviewJob(
        mode="plan", content="a plan", docs_content=None, extra=None, commit=None,
        pr=False, git_cmd=None, model=None,
    )
    assert "ARM PLAN TEXT" in rr_prompts.build_agent_prompt(job)


def test_export_round_trips_the_live_prompts(tmp_path):
    exported = export_arm(tmp_path / "exported")
    assert exported.texts == live_prompt_texts()
    assert exported.content_hash == load_arm("current").content_hash


def test_a_freshly_exported_arm_satisfies_the_provenance_requirement(tmp_path):
    # Otherwise `python evals/arms.py export <new-arm>` produces something that fails CI.
    readme = export_arm(tmp_path / "exported").path / "README.md"
    assert readme.is_file()
    assert "TODO" in readme.read_text(encoding="utf-8")


def test_export_does_not_overwrite_an_arms_recorded_provenance(tmp_path):
    directory = tmp_path / "exported"
    export_arm(directory)
    (directory / "README.md").write_text("Taken from commit abc1234.\n", encoding="utf-8")
    export_arm(directory)
    assert (directory / "README.md").read_text(encoding="utf-8") == "Taken from commit abc1234.\n"


@pytest.mark.parametrize(
    "name", ["../escaped", "..", ".", "", "nested/arm", "/tmp/absolute"],
)
def test_export_refuses_an_arm_name_that_is_a_path(name):
    # export deletes *.txt it considers stale, so a name that escapes evals/prompts would
    # let one typo remove files somewhere else entirely.
    with pytest.raises(ArmError, match="single directory name"):
        arm_directory(name)


def test_export_accepts_a_plain_arm_name():
    assert arm_directory("candidate-b").parent == PROMPTS_DIR


def test_export_clears_a_prompt_file_the_runtime_no_longer_has(tmp_path):
    # A renamed or removed constant would otherwise leave a file behind that load_arm
    # rejects, so the export would produce an arm that cannot be loaded.
    directory = tmp_path / "exported"
    export_arm(directory)
    (directory / "RETIRED_PROMPT.txt").write_text("old", encoding="utf-8")
    exported = export_arm(directory)
    assert not (directory / "RETIRED_PROMPT.txt").exists()
    assert exported.texts == live_prompt_texts()
