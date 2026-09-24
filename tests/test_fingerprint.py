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

#: The real probe, kept before the autouse fixture replaces it, for the cases that test it.
REAL_CLI_VERSION = fingerprint.cli_version

BASE_ARGS = ["--fingerprint", "--mode", "diff", "--json", "--effort", "medium",
             "--backend", "codex:m1,claude:claude-m2"]


@pytest.fixture(autouse=True)
def no_review(monkeypatch):
    """Every backend CLI is "installed", and none of them may be asked to review."""
    def refuse(job):
        raise AssertionError("--fingerprint started a review")

    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.fingerprint.cli_version", lambda binary: f"{binary} 1.0")
    monkeypatch.setattr("rocket_review.fingerprint.sdk_version", lambda: "openai 1.0")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
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
        {"name": "codex", "model": "m1", "cli_version": "codex 1.0"},
        {"name": "claude", "model": "claude-m2", "cli_version": "claude 1.0"},
    ]
    assert doc["pinned"] is True


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


@pytest.mark.parametrize("source, mode", [
    (["--staged"], "diff"), (["--commit", "HEAD"], "diff"), (["--pr", "5"], "diff"),
    (["plan.md"], "plan"), (["src/app.py"], "code"),
])
def test_no_source_is_resolved_or_read(monkeypatch, capsys, source, mode):
    # The fingerprint exits before the content is gathered: no git, no gh, no file read.
    for name in ("ensure_diff_exists", "resolve_commit", "get_pr_content", "read_files",
                 "get_diff", "get_commit_diff"):
        monkeypatch.setattr(
            f"rocket_review.cli.{name}",
            lambda *a, _n=name, **k: (_ for _ in ()).throw(AssertionError(f"{_n} ran")),
        )
    doc = fp(monkeypatch, capsys, ["--fingerprint", *source, "--backend", "claude:m"])
    assert doc["mode"] == mode


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
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--effort", "high",
                                   "--backend", "codex,claude:m"])
    assert [b["model"] for b in doc["backends"]] == [None, "m"]
    assert doc["pinned"] is False


@pytest.mark.parametrize("effort", [[], ["--effort", ""]])
def test_an_unset_effort_is_reported_as_unpinned(monkeypatch, capsys, effort):
    # With no effort — or an empty one, which every backend drops — rr passes none, and
    # each CLI applies a default of its own.
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", *effort,
                                   "--backend", "codex:m"])
    assert doc["pinned"] is False


def test_api_without_the_sdk_is_unpinned(monkeypatch, capsys):
    monkeypatch.setattr("rocket_review.fingerprint.sdk_version", lambda: None)
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--effort", "high",
                                   "--backend", "api"])
    assert doc["pinned"] is False


@pytest.mark.parametrize("backend", ["claude:claude-3-7-sonnet-latest", "codex:codex-mini-latest"])
def test_a_latest_alias_is_unpinned_on_every_backend(monkeypatch, capsys, backend):
    # A -latest name says in itself that it moves.
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--effort", "high",
                                   "--backend", backend])
    assert doc["pinned"] is False


def test_a_cli_that_cannot_say_its_version_is_reported_as_unpinned(monkeypatch, capsys):
    monkeypatch.setattr("rocket_review.fingerprint.cli_version", lambda binary: None)
    doc = fp(monkeypatch, capsys)
    assert [b["cli_version"] for b in doc["backends"]] == [None, None]
    assert doc["pinned"] is False


def test_api_counts_as_pinned_through_its_own_default(monkeypatch, capsys):
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--effort", "high",
                                   "--backend", "api"])
    assert doc["backends"] == [{
        "name": "api", "model": api.DEFAULT_MODEL, "cli_version": None,
        "sdk_version": "openai 1.0", "endpoint_sha256": None,
    }]
    assert doc["pinned"] is True


@pytest.mark.parametrize("model, pinned", [
    ("gpt-5.6-sol", True), ("gpt-5.6-sol-2026-01-01", True), ("custom-2026-01-01", True),
    # rr's own api backend documents the bare family name as one OpenAI can remap, and a
    # suffix outside the known tiers is no promise of one model.
    ("gpt-5.6", False), ("gpt-5.6-latest", False),
])
def test_an_api_model_is_pinned_only_when_it_names_one_model(monkeypatch, capsys, model, pinned):
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--effort", "high",
                                   "--backend", f"api:{model}"])
    assert doc["pinned"] is pinned


@pytest.mark.parametrize("model, pinned", [
    ("claude-opus-5-5", True),
    # Claude Code's own aliases pick whatever the account's current mapping is.
    ("default", False), ("opus", False), ("sonnet", False),
])
def test_a_claude_code_alias_is_unpinned(monkeypatch, capsys, model, pinned):
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--effort", "high",
                                   "--backend", f"claude:{model}"])
    assert doc["pinned"] is pinned


@pytest.mark.parametrize("model, pinned", [
    ("gpt-6-astra", True), ("gpt-5.6-sol", True),
    # The bare family name is the one rr's api backend documents as remappable, and codex
    # sends it to the same vendor.
    ("gpt-5.6", False), ("gpt-6", False),
])
def test_a_bare_openai_family_name_is_unpinned_for_codex_too(monkeypatch, capsys, model, pinned):
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--effort", "high",
                                   "--backend", f"codex:{model}"])
    assert doc["pinned"] is pinned


def test_opencode_is_never_pinned(monkeypatch, capsys):
    # opencode takes no --effort, so its effort is always its own config's.
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff",
                                   "--backend", "opencode:provider/model"])
    assert doc["pinned"] is False


def test_moves_with_the_api_endpoint_without_printing_it(monkeypatch, capsys):
    # The OpenAI SDK sends to OPENAI_BASE_URL when it is set: another gateway, another
    # reviewer. The URL can carry a credential, so only its hash is printed.
    args = ["--fingerprint", "--mode", "diff", "--effort", "high", "--backend", "api"]
    before = fp(monkeypatch, capsys, args)["fingerprint"]
    monkeypatch.setenv("OPENAI_BASE_URL", "https://user:s3cret@gateway.example/v1")
    code, doc, err = run(monkeypatch, capsys, args)
    assert code == 0, err
    assert doc["fingerprint"] != before
    assert "s3cret" not in json.dumps(doc)


def test_the_endpoint_hash_ignores_only_the_credentials(monkeypatch, capsys):
    # A rotated password in the URL is not another reviewer. Everything the SDK sends the
    # request with is: the host, the port, the path as written, a routing query.
    args = ["--fingerprint", "--mode", "diff", "--effort", "high", "--backend", "api"]

    def at(url):
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        return fp(monkeypatch, capsys, args)["fingerprint"]

    base = at("https://user:one@gateway.example:8443/v1?deployment=a")
    assert at("https://user:two@gateway.example:8443/v1?deployment=a") == base
    for other in ("https://gateway.example:9443/v1?deployment=a",
                  "https://gateway.example:8443/v1?deployment=b",
                  "https://gateway.example:8443/v1/?deployment=a",
                  "https://other.example:8443/v1?deployment=a"):
        assert at(other) != base, other


@pytest.mark.parametrize("url", ["https://gateway.example:bad/v1", "https://[::1/v1"])
def test_a_malformed_endpoint_is_hashed_not_raised(monkeypatch, capsys, url):
    # The SDK rejects it when a review runs; the fingerprint has no reason to crash first.
    monkeypatch.setenv("OPENAI_BASE_URL", url)
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--effort", "high",
                                   "--backend", "api"])
    assert doc["backends"][0]["endpoint_sha256"] is not None


@pytest.mark.parametrize("source, name", [
    ([], "unspecified"), (["--diff"], "diff"), (["--staged"], "staged"),
    (["--commit", "HEAD"], "commit"), (["--pr", "5"], "pr"), (["a.py"], "files"),
])
def test_records_which_source_the_review_reads(monkeypatch, capsys, source, name):
    # The same content arrives with different instructions per source: a PR is inlined with
    # PR wording, a --diff is a command to run. Hashing every shape says what could run; the
    # source says which one does.
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    args = ["--fingerprint", "--mode", "diff", *source, "--backend", "claude:m"]
    assert fp(monkeypatch, capsys, args)["source"] == name


def test_moves_with_the_source(monkeypatch, capsys):
    monkeypatch.setattr("rocket_review.cli.get_pr_content", lambda *a, **k: None)
    assert (fp(monkeypatch, capsys, BASE_ARGS)["fingerprint"]
            != fp(monkeypatch, capsys, BASE_ARGS + ["--pr", "5"])["fingerprint"])


def test_moves_with_the_api_sdk_version(monkeypatch, capsys):
    args = ["--fingerprint", "--mode", "diff", "--effort", "high", "--backend", "api"]
    before = fp(monkeypatch, capsys, args)["fingerprint"]
    monkeypatch.setattr("rocket_review.fingerprint.sdk_version", lambda: "openai 2.0")
    assert fp(monkeypatch, capsys, args)["fingerprint"] != before


def _fake_cli(tmp_path, body):
    path = tmp_path / "fake-cli"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("body, expected", [
    ("echo 'fake 1.2.3'", "fake 1.2.3"),
    ("echo 'fake 1.2.3'; exit 1", None),
    ("echo 'fake 1.2.3' >&2", None),
    ("sleep 10", None),
])
def test_cli_version_answers_only_from_a_clean_version_line(monkeypatch, tmp_path, body, expected):
    monkeypatch.setattr(fingerprint, "VERSION_TIMEOUT", 0.5)
    assert REAL_CLI_VERSION(_fake_cli(tmp_path, body)) == expected


def test_cli_version_of_a_missing_binary_is_none(tmp_path):
    assert REAL_CLI_VERSION(str(tmp_path / "absent")) is None


def test_an_api_alias_rr_resolves_at_run_time_is_unpinned(monkeypatch, capsys):
    # A non-canonical name is resolved to the newest dated snapshot the account lists, so the
    # model can change between two runs under the same name.
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--effort", "high",
                                   "--backend", "api:custom"])
    assert doc["pinned"] is False


def test_a_models_table_in_the_user_config_pins_the_model(monkeypatch, capsys, tmp_path):
    user = tmp_path / "config-home" / "rocket-review" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text('[models]\ncodex = "from-config"\n')
    doc = fp(monkeypatch, capsys, ["--fingerprint", "--mode", "diff", "--backend", "codex"])
    assert doc["backends"][0]["model"] == "from-config"


# Each of these changes what a reviewer is asked or who asks it, so each must move the hash.
MOVES = {
    "backend list": BASE_ARGS[:-1] + ["codex:m1"],
    "backend model": BASE_ARGS[:-1] + ["codex:m1,claude:other"],
    "effort": BASE_ARGS + ["--effort", "high"],
    # api drops its file attachments when too little of the timeout is left for them.
    "timeout": BASE_ARGS + ["--timeout", "3300"],
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
    user.write_text('codex_sandbox = "workspace-write"\n')
    assert fp(monkeypatch, capsys)["fingerprint"] != before


def test_moves_with_a_backend_cli_version(monkeypatch, capsys):
    # Each CLI release brings its own system prompt, tools and defaults.
    before = fp(monkeypatch, capsys)["fingerprint"]
    monkeypatch.setattr("rocket_review.fingerprint.cli_version", lambda binary: f"{binary} 2.0")
    assert fp(monkeypatch, capsys)["fingerprint"] != before


def test_moves_when_the_text_under_review_is_from_another_repository(monkeypatch, capsys):
    # --repo turns off api's file attachments, so the same PR text is reviewed with less.
    monkeypatch.setattr(
        "rocket_review.cli.get_pr_content",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the PR was fetched")),
    )
    args = ["--fingerprint", "--pr", "5", "--effort", "high", "--backend", "api"]
    assert (fp(monkeypatch, capsys, args)["fingerprint"]
            != fp(monkeypatch, capsys, args + ["--repo", "acme/api"])["fingerprint"])


def test_moves_with_rr_own_code(monkeypatch, capsys):
    # The version string does not move in a source checkout or an editable install, and the
    # code that parses and gates a review decides the verdict as much as the prompt does.
    before = fp(monkeypatch, capsys)["fingerprint"]
    monkeypatch.setattr("rocket_review.fingerprint.code_digest", lambda: "0" * 64)
    assert fp(monkeypatch, capsys)["fingerprint"] != before


def test_the_code_digest_covers_every_module_in_the_package(tmp_path):
    (tmp_path / "backends").mkdir()
    (tmp_path / "cli.py").write_text("a = 1\n")
    (tmp_path / "backends" / "api.py").write_text("b = 1\n")
    before = fingerprint.code_digest(tmp_path)
    (tmp_path / "backends" / "api.py").write_text("b = 2\n")
    assert fingerprint.code_digest(tmp_path) != before


def test_moves_with_the_staged_git_command(monkeypatch, capsys):
    # The per-source instruction for --staged names its own command; the fingerprint probes
    # the constant the review uses rather than a copy of it.
    before = fp(monkeypatch, capsys)["fingerprint"]
    monkeypatch.setattr("rocket_review.cli.STAGED_GIT_CMD", "git diff --cached")
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


def test_holds_still_for_full_output(monkeypatch, capsys):
    # --full only decides how much of an answer is printed.
    assert (fp(monkeypatch, capsys)["fingerprint"]
            == fp(monkeypatch, capsys, BASE_ARGS + ["--full"])["fingerprint"])


def test_holds_still_for_where_the_same_checkout_sits(monkeypatch, capsys, tmp_path):
    # A hook reviews inside a fresh temporary worktree every time, so the same repository
    # content at two paths must be one fingerprint, project config and docs included.
    results = []
    for name in ("wt-a", "wt-b"):
        root = tmp_path / name
        root.mkdir()
        (root / ".rocket-review.toml").write_text('timeout = 1234\ndocs = ["STANDARDS.md"]\n')
        (root / "STANDARDS.md").write_text("rule\n")
        # A project config's docs are read only when the repository tracks them at HEAD.
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                     "commit", "-qm", "c"]):
            subprocess.run(["git", "-C", str(root), *cmd], check=True, capture_output=True)
        repo.clear_caches()
        monkeypatch.chdir(root)
        results.append(fp(monkeypatch, capsys, BASE_ARGS))
    assert results[0]["fingerprint"] == results[1]["fingerprint"]
    assert results[0]["timeout"] == 1234
    assert results[0]["docs_sha256"] is not None


def test_holds_still_for_a_comment_in_a_config_file(monkeypatch, capsys, tmp_path):
    user = tmp_path / "config-home" / "rocket-review" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text('effort = "high"\n')
    before = fp(monkeypatch, capsys)["fingerprint"]
    user.write_text('# the everyday setting\neffort = "high"\n')
    assert fp(monkeypatch, capsys)["fingerprint"] == before
