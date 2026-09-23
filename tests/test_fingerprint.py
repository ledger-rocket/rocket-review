"""`rr --fingerprint`: one stable hash of everything that decides a review's verdict.

A caller (a pre-push hook keeping review receipts) reuses a verdict when the reviewed trees
and this fingerprint are both unchanged, so the two failure directions are not equal. A
fingerprint that stays put across a change that moves the verdict hands back a stale
verdict; one that moves on a change that cannot matter only costs a fresh review.
"""

import json
import re
import subprocess
import types
from pathlib import Path

import pytest

from rocket_review import fingerprint, prompts, repo
from rocket_review.backends import BACKENDS, api, claude, codex, opencode
from rocket_review.cli import main
from rocket_review.models import REVIEW_SCHEMA

BASE_ARGS = ["--fingerprint", "--mode", "diff", "--json", "--backend", "codex:m1,claude:m2"]


@pytest.fixture(autouse=True)
def no_review(monkeypatch):
    """Every backend CLI is "installed", and none of them may be asked to review."""
    def refuse(job):
        raise AssertionError("--fingerprint started a review")

    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr(
        "rocket_review.cli.run_one",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("--fingerprint started a review")),
    )
    for mod in BACKENDS.values():
        monkeypatch.setattr(mod, "review", refuse)


def run(monkeypatch, capsys, argv):
    """(exit code, parsed stdout or None, stderr) for one `rr` invocation."""
    monkeypatch.setattr("sys.argv", ["rr", *argv])
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    out, err = capsys.readouterr()
    try:
        doc = json.loads(out) if out.strip() else None
    except json.JSONDecodeError:
        doc = None
    return code, doc, err


def fp(monkeypatch, capsys, argv=None) -> dict:
    code, doc, err = run(monkeypatch, capsys, argv if argv is not None else BASE_ARGS)
    assert code == 0, err
    assert doc is not None, "no JSON document on stdout"
    return doc


def test_prints_a_versioned_sha256_fingerprint_and_reviews_nothing(monkeypatch, capsys):
    doc = fp(monkeypatch, capsys)
    assert doc["fingerprint_version"] == "1"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", doc["fingerprint"])
    assert doc["mode"] == "diff"
    assert doc["backends"] == [
        {"name": "codex", "model": "m1"}, {"name": "claude", "model": "m2"},
    ]
    assert doc["models_pinned"] is True


def test_is_stable_across_runs(monkeypatch, capsys):
    assert fp(monkeypatch, capsys)["fingerprint"] == fp(monkeypatch, capsys)["fingerprint"]


def test_the_fingerprint_is_the_hash_of_the_rest_of_the_document(monkeypatch, capsys):
    # A reader can recompute it, so the document is evidence of what was hashed rather
    # than a label next to an opaque value.
    doc = fp(monkeypatch, capsys)
    assert doc["fingerprint"] == fingerprint.digest(fingerprint.material(doc))


def test_ignores_stdin_rather_than_treating_it_as_a_second_source(monkeypatch, capsys):
    # A hook runs this with the push's ref lines, or a diff, on stdin. Neither is read: the
    # fingerprint describes the reviewer, and the content is the caller's to key on. With a
    # source flag beside it, a pipe counted as a source would refuse as "two sources".
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: True)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(
        isatty=lambda: False,
        read=lambda *a: (_ for _ in ()).throw(AssertionError("stdin was read")),
    ))
    assert fp(monkeypatch, capsys)["mode"] == "diff"
    assert fp(monkeypatch, capsys, ["--fingerprint", "--diff", "--backend", "claude:m"])["mode"] == "diff"


def test_needs_a_mode_when_no_source_names_one(monkeypatch, capsys):
    code, doc, err = run(monkeypatch, capsys, ["--fingerprint", "--json", "--backend", "codex"])
    assert code == 1
    assert doc is None
    assert "--fingerprint needs --mode" in err


def test_a_source_flag_decides_the_mode_without_being_read(monkeypatch, capsys):
    monkeypatch.setattr(
        "rocket_review.cli.ensure_diff_exists",
        lambda staged: (_ for _ in ()).throw(AssertionError("the diff was inspected")),
    )
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--diff", "--backend", "claude:m"])
    assert doc["mode"] == "diff"


def test_refuses_the_settings_a_review_would_refuse(monkeypatch, capsys):
    # A fingerprint of a review that cannot start would key a receipt nobody can mint.
    code, doc, _ = run(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--fail-on", "high"])
    assert code == 1
    assert doc is None


def test_refuses_a_backend_that_is_not_installed(monkeypatch, capsys):
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: "install it")
    code, doc, err = run(monkeypatch, capsys, BASE_ARGS)
    assert code == 1
    assert doc is None
    assert "unavailable" in err


def test_an_unpinned_model_is_reported_as_unpinned(monkeypatch, capsys):
    # codex and claude with no pin run whatever the CLI's own default is, which can change
    # under an unchanged fingerprint. The caller is told, so it can decline to reuse.
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--backend", "codex,claude:m"])
    assert doc["backends"] == [{"name": "codex", "model": None}, {"name": "claude", "model": "m"}]
    assert doc["models_pinned"] is False


def test_api_counts_as_pinned_through_its_own_default(monkeypatch, capsys):
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--backend", "api"])
    assert doc["backends"] == [{"name": "api", "model": api.DEFAULT_MODEL}]
    assert doc["models_pinned"] is True


def test_a_models_table_in_the_user_config_pins_the_model(monkeypatch, capsys, tmp_path):
    user = tmp_path / "config-home" / "rocket-review" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text('[models]\ncodex = "from-config"\n')
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--backend", "codex"])
    assert doc["backends"] == [{"name": "codex", "model": "from-config"}]


# Each of these changes what a reviewer is asked or who asks it, so each must move the hash.
MOVES = {
    "backend list": BASE_ARGS[:-1] + ["codex:m1"],
    "backend model": BASE_ARGS[:-1] + ["codex:m1,claude:other"],
    "effort": BASE_ARGS + ["--effort", "high"],
    "codex sandbox": BASE_ARGS + ["--codex-sandbox", "workspace-write"],
    "fail-on threshold": BASE_ARGS + ["--fail-on", "medium"],
    "json mode": [a for a in BASE_ARGS if a != "--json"],
    "mode": ["--fingerprint", "--mode", "code", *BASE_ARGS[3:]],
    "extra instructions": BASE_ARGS + ["--prompt", "check the locking"],
}


@pytest.mark.parametrize("change", sorted(MOVES))
def test_moves_on_every_input_that_decides_the_verdict(monkeypatch, capsys, change):
    before = fp(monkeypatch, capsys)["fingerprint"]
    after = fp(monkeypatch, capsys, MOVES[change])["fingerprint"]
    assert before != after, f"the fingerprint did not move on a change of {change}"


def test_moves_with_the_standards_doc_content(monkeypatch, capsys):
    Path("llms.txt").write_text("rule one\n")
    before = fp(monkeypatch, capsys, BASE_ARGS + ["--docs", "llms.txt"])["fingerprint"]
    Path("llms.txt").write_text("rule two\n")
    after = fp(monkeypatch, capsys, BASE_ARGS + ["--docs", "llms.txt"])["fingerprint"]
    assert before != after


def test_moves_with_a_config_file_setting(monkeypatch, capsys, tmp_path):
    before = fp(monkeypatch, capsys)["fingerprint"]
    user = tmp_path / "config-home" / "rocket-review" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text('effort = "high"\n')
    assert fp(monkeypatch, capsys)["fingerprint"] != before


def test_moves_with_the_rr_version(monkeypatch, capsys):
    before = fp(monkeypatch, capsys)["fingerprint"]
    monkeypatch.setattr("rocket_review.fingerprint.rr_version", lambda: "999.0.0")
    assert fp(monkeypatch, capsys)["fingerprint"] != before


@pytest.mark.parametrize("constant", ["DIFF_REVIEW_PROMPT", "JSON_OUTPUT_ADDENDUM",
                                      "_REVIEW_EVIDENCE_RULE"])
def test_moves_with_the_assembled_prompt(monkeypatch, capsys, constant):
    # The version string does not move in a source checkout or an editable install, and a
    # prompt edit there is still a different reviewer.
    before = fp(monkeypatch, capsys)["fingerprint"]
    monkeypatch.setattr(prompts, constant, getattr(prompts, constant) + " changed")
    assert fp(monkeypatch, capsys)["fingerprint"] != before


def test_moves_with_the_output_schema_in_json_mode(monkeypatch, capsys):
    # codex and api hand the schema to the model as a constraint on its answer.
    before = fp(monkeypatch, capsys)["fingerprint"]
    monkeypatch.setitem(REVIEW_SCHEMA, "description", "a different contract")
    assert fp(monkeypatch, capsys)["fingerprint"] != before


def test_moves_with_extra_instructions_the_prompt_does_not_carry(monkeypatch, capsys):
    # api sends --prompt in the user message, not in the system prompt, so only the
    # fingerprint's own record of it can see the change.
    args = ["--fingerprint", "--mode", "diff", "--backend", "api"]
    before = fp(monkeypatch, capsys, args + ["--prompt", "one"])["fingerprint"]
    assert fp(monkeypatch, capsys, args + ["--prompt", "two"])["fingerprint"] != before


def test_moves_with_a_backend_sandbox_description(monkeypatch, capsys):
    before = fp(monkeypatch, capsys)["fingerprint"]
    monkeypatch.setitem(codex.SANDBOX_ENVIRONMENTS, "read-only", "a different sandbox")
    assert fp(monkeypatch, capsys)["fingerprint"] != before


def test_moves_with_the_git_command_a_diff_job_is_told_to_run(monkeypatch, capsys):
    # The per-source instruction is chosen at assembly time. A fingerprint built from one
    # source shape would miss a change to the text another shape reads.
    before = fp(monkeypatch, capsys)["fingerprint"]
    original = prompts.build_agent_prompt

    def reworded(job, environment):
        text = original(job, environment)
        return text.replace("to see the changes, then review them", "and review it")

    for mod in (codex, claude, opencode):
        monkeypatch.setattr(mod, "build_agent_prompt", reworded)
    assert fp(monkeypatch, capsys)["fingerprint"] != before


def test_holds_still_for_the_timeout(monkeypatch, capsys):
    # The timeout decides whether a backend answers, never what it answers, and a timed-out
    # review is not a verdict anyone keeps.
    assert (fp(monkeypatch, capsys)["fingerprint"]
            == fp(monkeypatch, capsys, BASE_ARGS + ["--timeout", "3300"])["fingerprint"])


def test_holds_still_for_where_the_same_checkout_sits(monkeypatch, capsys, tmp_path):
    # A hook reviews inside a fresh temporary worktree every time, so the same repository
    # content at two paths must be one fingerprint, project config and docs included.
    results = []
    for name in ("wt-a", "wt-b"):
        root = tmp_path / name
        root.mkdir()
        (root / ".rocket-review.toml").write_text('effort = "low"\ndocs = ["STANDARDS.md"]\n')
        (root / "STANDARDS.md").write_text("rule\n")
        # A project config's docs are read only when the repository tracks them at HEAD.
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"]):
            subprocess.run(["git", "-C", str(root), *cmd], check=True, capture_output=True)
        repo.clear_caches()
        monkeypatch.chdir(root)
        results.append(fp(monkeypatch, capsys, BASE_ARGS))
    assert results[0]["fingerprint"] == results[1]["fingerprint"]
    assert results[0]["effort"] == "low"
    assert results[0]["docs_sha256"] is not None


def test_holds_still_for_a_comment_in_a_config_file(monkeypatch, capsys, tmp_path):
    user = tmp_path / "config-home" / "rocket-review" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text('effort = "high"\n')
    before = fp(monkeypatch, capsys)["fingerprint"]
    user.write_text('# the everyday setting\neffort = "high"\n')
    assert fp(monkeypatch, capsys)["fingerprint"] == before
