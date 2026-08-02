"""Arm loading, hashing, and the guards that keep an arm from silently going stale."""

import pytest
import rocket_review.prompts as rr_prompts
from arms import (
    PROMPT_CONSTANTS,
    PROMPTS_DIR,
    ArmError,
    apply_arm,
    arm_hash,
    export_arm,
    live_prompt_texts,
    load_arm,
    runtime_prompt_constants,
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


def test_pre_m3a_arm_loads_and_differs_from_current():
    pre = load_arm("pre-m3a")
    assert pre.content_hash != load_arm("current").content_hash
    assert set(pre.texts) == set(PROMPT_CONSTANTS)


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
    with pytest.raises(ArmError, match="NEW_MODE_PROMPT"):
        apply_arm(load_arm(write_arm(tmp_path / "arm")))


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
    assert rr_prompts.get_prompt("diff") == "ARM DIFF TEXT\n"
    assert "ARM DIFF TEXT" in rr_prompts.get_prompt("diff", json_output=True)


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
