import pytest

from rocket_review.backends.base import ReviewJob
from rocket_review.prompts import build_agent_prompt, get_prompt

# Two kinds of pin live here. Most are characterization tests: they pin how prompts are
# assembled today, ahead of a planned refactor of prompt assembly, and assert what IS, not
# what should be — including the quirks below. The rest pin deliberate content decisions
# (which modes ask for praise, that severity is mandatory, that a format promising a line
# citation offers a slot for one), so a later edit cannot quietly reverse them. Assertions
# are substring/ordering only, so internals can be restructured and these break only when
# the externally visible prompt changes.

# Distinctive opening phrase of each mode body; cheaper to keep in sync than the whole body.
MODE_MARKERS = {
    "plan": "stress-testing an implementation plan",
    "code": "expert code reviewer",
    "diff": "reviewing a code diff",
}

# A second marker from deep inside each body, so gutting a body while keeping its opening
# line does not pass as the right prompt.
MODE_INTERIOR_MARKERS = {
    "plan": "PRAGMATISM — Is anything over-engineered",
    "code": "Concurrency: race conditions, deadlocks",
    "diff": "CONTRACTS — Does this break any API contracts",
}

STANDARDS_MARKER = "PROJECT STANDARDS CONTEXT"
JSON_MARKER = "OUTPUT FORMAT OVERRIDE"
DOCS = "# Standards\nno global mutable state"


def job(**kw):
    defaults = dict(mode="diff", content="diff --git a b", docs_content=None,
                    extra=None, commit=None, pr=False, git_cmd=None,
                    model=None, json_output=False)
    defaults.update(kw)
    return ReviewJob(**defaults)


@pytest.mark.parametrize("mode", list(MODE_MARKERS))
def test_mode_selects_only_its_own_body(mode):
    prompt = get_prompt(mode)
    assert MODE_MARKERS[mode] in prompt
    assert MODE_INTERIOR_MARKERS[mode] in prompt
    for other in (m for m in MODE_MARKERS if m != mode):
        assert MODE_MARKERS[other] not in prompt
        assert MODE_INTERIOR_MARKERS[other] not in prompt


def test_only_the_plan_prompt_asks_what_is_good():
    # Asymmetry is deliberate: on a plan, knowing what is solid tells the reader what to
    # keep. On code and diffs an instructed search for praise softens the critical read and
    # spends output budget that belongs to findings.
    assert "**Strengths**" in get_prompt("plan")
    for mode in ("code", "diff"):
        prompt = get_prompt(mode)
        for marker in ("Positive Aspects", "What looks good", "positives"):
            assert marker not in prompt


@pytest.mark.parametrize("mode", list(MODE_MARKERS))
def test_every_mode_makes_severity_mandatory(mode):
    assert "Every finding MUST carry a severity" in get_prompt(mode)


@pytest.mark.parametrize("mode", ["code", "diff"])
def test_finding_format_has_a_line_slot_where_a_line_is_asked_for(mode):
    # These two modes tell the reviewer to cite a file and line; a finding format with no
    # slot for the line would contradict that. Plan findings cite neither, so it is exempt.
    assert "cite a file and line" in get_prompt(mode)
    assert "[SEVERITY] File:Line — Issue description" in get_prompt(mode)


def test_unknown_mode_raises_key_error():
    with pytest.raises(KeyError):
        get_prompt("bogus")


@pytest.mark.parametrize("mode", list(MODE_MARKERS))
def test_prose_mode_keeps_summary_block_and_omits_json_addendum(mode):
    prompt = get_prompt(mode)
    assert "<SUMMARY>" in prompt and "</SUMMARY>" in prompt
    assert JSON_MARKER not in prompt


def test_standards_addendum_only_when_docs_provided():
    assert STANDARDS_MARKER in get_prompt("code", docs_content=DOCS)
    assert STANDARDS_MARKER not in get_prompt("code")


def test_empty_docs_string_is_treated_as_no_docs():
    assert STANDARDS_MARKER not in get_prompt("code", docs_content="")


def test_json_addendum_only_when_json_output():
    assert JSON_MARKER in get_prompt("diff", json_output=True)
    assert JSON_MARKER not in get_prompt("diff")


def test_json_addendum_overrides_rather_than_replaces_the_prose_format():
    # Status quo: the mode body's own OUTPUT FORMAT and <SUMMARY> instructions stay in the
    # prompt and the addendum tells the model to ignore them, so both are sent.
    prompt = get_prompt("diff", json_output=True)
    assert "<SUMMARY>" in prompt
    assert "Ignore the output format instructions above" in prompt
    assert prompt.index("<SUMMARY>") < prompt.index(JSON_MARKER)


def test_json_addendum_describes_the_findings_object_shape():
    prompt = get_prompt("code", json_output=True)
    # Slice to the addendum: the mode bodies talk about verdicts and files too, so a
    # whole-prompt search would not prove the keys come from the JSON instructions.
    addendum = prompt[prompt.index(JSON_MARKER):]
    for key in ('"verdict"', '"summary"', '"findings"', '"severity"', '"file"', '"line"'):
        assert key in addendum


def test_addendum_order_is_body_then_standards_then_json():
    prompt = get_prompt("plan", docs_content=DOCS, json_output=True)
    assert prompt.index(MODE_MARKERS["plan"]) < prompt.index(STANDARDS_MARKER)
    assert prompt.index(STANDARDS_MARKER) < prompt.index(JSON_MARKER)


def test_docs_content_itself_is_not_inlined_by_get_prompt():
    # get_prompt only adds the instruction addendum; the documents themselves are the
    # caller's job (build_agent_prompt appends them in their own block).
    assert "no global mutable state" not in get_prompt("code", docs_content=DOCS)


def test_agent_prompt_leads_with_instructions_and_grants_read_access():
    prompt = build_agent_prompt(job(mode="code"))
    assert prompt == prompt.strip()  # instructions are stripped, so nothing pads the prompt
    assert prompt.index(MODE_MARKERS["code"]) < prompt.index("You have full read access")
    assert "Do not modify any files." in prompt


def test_agent_prompt_carries_get_prompt_addenda():
    prompt = build_agent_prompt(job(docs_content=DOCS, json_output=True))
    assert STANDARDS_MARKER in prompt and JSON_MARKER in prompt


def test_agent_prompt_extra_follows_instructions():
    prompt = build_agent_prompt(job(extra="focus on security"))
    assert "Additional instructions: focus on security" in prompt
    assert prompt.index("Additional instructions") < prompt.index("You have full read access")


def test_agent_prompt_omits_extra_line_when_unset():
    assert "Additional instructions" not in build_agent_prompt(job())


def test_agent_prompt_wraps_docs_in_a_delimited_block_after_the_addendum():
    prompt = build_agent_prompt(job(docs_content=DOCS))
    assert f"=== PROJECT STANDARDS ===\n{DOCS}\n=== END PROJECT STANDARDS ===" in prompt
    assert prompt.index(STANDARDS_MARKER) < prompt.index("=== PROJECT STANDARDS ===")


@pytest.mark.parametrize("mode, label", [("plan", "PLAN"), ("code", "CODE"), ("diff", "DIFF")])
def test_agent_prompt_labels_inline_content_per_mode(mode, label):
    prompt = build_agent_prompt(job(mode=mode, content="BODY"))
    assert f"=== {label} TO REVIEW ===\nBODY\n=== END {label} ===" in prompt


def test_agent_prompt_rejects_unknown_mode_before_labelling_content():
    # The "CONTENT" label fallback in build_agent_prompt is unreachable: get_prompt raises
    # on an unknown mode before the label is chosen.
    with pytest.raises(KeyError):
        build_agent_prompt(job(mode="bogus"))


def test_agent_prompt_commit_asks_the_backend_to_run_git_show():
    prompt = build_agent_prompt(job(content=None, commit="abc123"))
    assert "Run `git show abc123` to see the commit" in prompt
    assert "TO REVIEW ===" not in prompt


def test_agent_prompt_git_cmd_asks_the_backend_to_run_it():
    prompt = build_agent_prompt(job(content=None, git_cmd="git diff --staged"))
    assert "Run `git diff --staged` to see the changes" in prompt


def test_agent_prompt_pr_inlines_content_with_pr_framing():
    prompt = build_agent_prompt(job(pr=True, content="PR BODY AND DIFF"))
    assert "You are reviewing a GitHub pull request." in prompt
    assert prompt.endswith("PR BODY AND DIFF")
    assert "=== DIFF TO REVIEW ===" not in prompt


def test_agent_prompt_pr_wins_over_commit_and_git_cmd():
    prompt = build_agent_prompt(job(pr=True, content="PR BODY", commit="abc123",
                                    git_cmd="git diff"))
    assert "GitHub pull request" in prompt
    assert "git show abc123" not in prompt and "Run `git diff`" not in prompt


def test_agent_prompt_commit_wins_over_git_cmd_and_content():
    prompt = build_agent_prompt(job(commit="abc123", git_cmd="git diff", content="INLINE"))
    assert "git show abc123" in prompt
    assert "Run `git diff`" not in prompt and "INLINE" not in prompt


def test_agent_prompt_with_no_content_source_still_gives_instructions():
    prompt = build_agent_prompt(job(content=None))
    assert MODE_MARKERS["diff"] in prompt
    assert "TO REVIEW ===" not in prompt


def test_agent_prompt_joins_parts_with_blank_lines():
    prompt = build_agent_prompt(job(extra="x"))
    assert "\n\nAdditional instructions: x\n\n" in prompt
