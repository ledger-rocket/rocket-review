import io
import json
import os
import shutil
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError

import pytest

from rocket_review.backends import _openai_sdk_installed, available, base
from rocket_review.backends.base import BackendError
from rocket_review.cli import (
    RAW_TRUNCATE_LIMIT,
    detect_mode,
    ensure_diff_exists,
    get_commit_diff,
    get_diff,
    main,
    parse_backend_arg,
    resolve_commit,
    rr_version,
    run_capture,
    stdin_has_input,
    truncate_raw,
)
from rocket_review.models import BackendResult

TEXT_HINT = "help: rr --diff --json --fail-on high (CI gate)"

REVIEW_JSON = (
    '{{"verdict": "needs_fixes", "summary": "s", "findings": '
    '[{{"severity": "high", "title": "t", "file": null, "line": null, '
    '"why": "w", "fix": "{body}"}}]}}'
)


def run_cli(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["rr", *argv])
    with pytest.raises(SystemExit) as e:
        main()
    return e.value.code


def run_main(monkeypatch, argv):
    """Drive main() tolerating the success path, which returns instead of exiting."""
    monkeypatch.setattr("sys.argv", ["rr", *argv])
    try:
        main()
    except SystemExit as e:
        return e.code if e.code is not None else 0
    return 0


def patch_backends(monkeypatch, reviews):
    """Swap in fake backends. reviews maps name -> output str, or a BackendError to raise."""
    def make_review(behavior):
        def review(job):
            if isinstance(behavior, BackendError):
                raise behavior
            return behavior
        return review

    fakes = {name: types.SimpleNamespace(review=make_review(b)) for name, b in reviews.items()}
    monkeypatch.setattr("rocket_review.cli.BACKENDS", fakes)
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)


def test_fail_on_requires_json(monkeypatch, capsys):
    code = run_cli(monkeypatch, ["--diff", "--fail-on", "high"])
    assert code == 1
    assert "--fail-on requires --json" in capsys.readouterr().err


def test_detect_mode_plan_for_markdown():
    assert detect_mode(["docs/plan.md"]) == "plan"
    assert detect_mode(["a.md", "b.txt", "c.plan"]) == "plan"


def test_detect_mode_code_for_source_files():
    assert detect_mode(["src/auth.py"]) == "code"


def test_detect_mode_mixed_is_code():
    assert detect_mode(["plan.md", "src/auth.py"]) == "code"


def test_backend_arg_single_without_model():
    assert parse_backend_arg("codex", None) == [("codex", None)]


def test_backend_arg_list_with_permodel():
    assert parse_backend_arg("codex:gpt-5.5,claude", None) == [
        ("codex", "gpt-5.5"), ("claude", None),
    ]


def test_backend_arg_global_model_single():
    assert parse_backend_arg("claude", "claude-opus-4-8") == [("claude", "claude-opus-4-8")]


def test_backend_arg_global_model_multi_errors():
    with pytest.raises(SystemExit):
        parse_backend_arg("codex,claude", "gpt-5.5")


def test_backend_arg_unknown_errors():
    with pytest.raises(SystemExit):
        parse_backend_arg("gemini", None)


def test_backend_arg_duplicate_errors():
    with pytest.raises(SystemExit):
        parse_backend_arg("codex,codex", None)


# --- per-mode default backends -------------------------------------------------------

# The exact line users see; asserted verbatim so a silent reword can't hide the fallback.
FALLBACK_NOTICE = (
    "Note: default backend '{default}' for {mode} review is unavailable; using "
    "'{chosen}'. Pass --backend to choose explicitly and silence this."
)

MODE_SOURCES = [("plan", ["plan.md"]), ("code", ["mod.py"]), ("diff", ["--diff"])]


def _mode_sources(tmp_path, monkeypatch):
    """A cwd holding one source file per file-driven mode."""
    (tmp_path / "plan.md").write_text("# Plan\nstep one\n")
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n")
    monkeypatch.chdir(tmp_path)


def _record_backends(monkeypatch):
    """Fake all four backends; returns the (name, mode) pairs that actually ran.

    Deliberately leaves missing_binary and available unpatched: backend selection is what
    is under test, so it must run for real against the stubbed binaries below.
    """
    ran = []

    def make(name):
        def review(job):
            ran.append((name, job.mode))
            return "REVIEW"
        return review

    monkeypatch.setattr(
        "rocket_review.cli.BACKENDS",
        {n: types.SimpleNamespace(review=make(n)) for n in ("codex", "claude", "opencode", "api")},
    )
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    monkeypatch.setattr("rocket_review.cli.get_diff", lambda staged: "diff --git a b\n+x")
    return ran


def _installed(monkeypatch, *names, api_key=None, api_sdk=True):
    """Make exactly these backends look available to the real availability check."""
    monkeypatch.setattr(
        shutil, "which", lambda binary: f"/usr/bin/{binary}" if binary in names else None
    )
    # Pin both halves of the api probe: neither a developer's real ~/.env nor whether the
    # openai extra happens to be installed in this environment may decide the test.
    monkeypatch.setattr("rocket_review.backends.api._load_env_file", lambda: None)
    monkeypatch.setattr("rocket_review.backends._openai_sdk_installed", lambda: api_sdk)
    if api_key:
        monkeypatch.setenv("OPENAI_API_KEY", api_key)
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.mark.parametrize("mode,argv,expected", [
    ("plan", ["plan.md"], "codex"),
    ("code", ["mod.py"], "claude"),
    ("diff", ["--diff"], "claude"),
])
def test_default_backend_follows_the_mode(monkeypatch, tmp_path, capsys, mode, argv, expected):
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "codex", "claude", "opencode")
    assert run_main(monkeypatch, argv) == 0
    assert ran == [(expected, mode)]
    assert "Note: default backend" not in capsys.readouterr().err  # nothing to announce


def test_mode_flag_selects_the_default_backend(monkeypatch, tmp_path):
    # --mode is applied before the backend is chosen, so an overridden mode brings its own
    # default with it rather than the auto-detected mode's.
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "codex", "claude")
    assert run_main(monkeypatch, ["plan.md", "--mode", "code"]) == 0
    assert ran == [("claude", "code")]


@pytest.mark.parametrize("mode,argv", MODE_SOURCES)
def test_explicit_backend_overrides_every_mode_default(monkeypatch, tmp_path, capsys, mode, argv):
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "codex", "claude", "opencode")
    assert run_main(monkeypatch, [*argv, "--backend", "opencode"]) == 0
    assert ran == [("opencode", mode)]
    assert "Note: default backend" not in capsys.readouterr().err


@pytest.mark.parametrize("mode,argv", MODE_SOURCES)
def test_explicit_backend_list_overrides_every_mode_default(monkeypatch, tmp_path, mode, argv):
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "codex", "claude")
    assert run_main(monkeypatch, [*argv, "--backend", "claude,codex"]) == 0
    assert sorted(ran) == [("claude", mode), ("codex", mode)]


def test_model_flag_applies_to_the_mode_default(monkeypatch, tmp_path):
    # --model names a model, not a backend, so it has to land on whichever backend the
    # mode selected.
    _mode_sources(tmp_path, monkeypatch)
    jobs = _capture_jobs(monkeypatch, "codex", "claude")
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    _installed(monkeypatch, "codex", "claude")
    assert run_main(monkeypatch, ["--diff", "--model", "claude-opus-4-8"]) == 0
    assert jobs["claude"].model == "claude-opus-4-8"
    assert "codex" not in jobs


def test_api_alias_conflicts_with_an_explicit_backend(monkeypatch, tmp_path, capsys):
    # --api is shorthand for one backend, so pairing it with any explicit --backend is a
    # contradiction and is refused — including --backend codex, which neither wins nor is
    # silently discarded.
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "codex", "claude")
    assert run_cli(monkeypatch, ["--diff", "--api", "--backend", "codex"]) == 1
    assert "--api conflicts with --backend" in capsys.readouterr().err
    assert ran == []


def test_api_alias_still_overrides_the_mode_default(monkeypatch, tmp_path):
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "codex", "claude")
    assert run_main(monkeypatch, ["--diff", "--api"]) == 0
    assert ran == [("api", "diff")]


@pytest.mark.parametrize("mode,argv,default,chosen,installed", [
    ("diff", ["--diff"], "claude", "codex", ("codex",)),
    ("plan", ["plan.md"], "codex", "claude", ("claude",)),
    ("diff", ["--diff"], "claude", "opencode", ("opencode",)),  # past the peer CLI
])
def test_missing_default_falls_back_loudly(
    monkeypatch, tmp_path, capsys, mode, argv, default, chosen, installed
):
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, *installed)
    assert run_main(monkeypatch, argv) == 0
    assert ran == [(chosen, mode)]
    notice = FALLBACK_NOTICE.format(default=default, mode=mode, chosen=chosen)
    assert capsys.readouterr().err.count(notice) == 1  # announced once, not per attempt


def test_fallback_reaches_api_only_when_a_key_is_configured(monkeypatch, tmp_path, capsys):
    # api ships no binary, so "installed" cannot mean anything for it: a key is the test.
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, api_key="sk-test")
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert ran == [("api", "diff")]
    notice = FALLBACK_NOTICE.format(default="claude", mode="diff", chosen="api")
    assert capsys.readouterr().err.count(notice) == 1


def test_fallback_notice_names_the_model_that_rides_along(monkeypatch, tmp_path, capsys):
    # --model follows the substituted backend, where a model from the absent vendor fails
    # at the backend, so the notice has to say what will actually run.
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "codex")
    assert run_main(monkeypatch, ["--diff", "--model", "claude-opus-4-8"]) == 0
    assert ran == [("codex", "diff")]
    notice = (
        "Note: default backend 'claude' for diff review is unavailable; using 'codex' "
        "with --model claude-opus-4-8. Pass --backend to choose explicitly and silence this."
    )
    assert capsys.readouterr().err.count(notice) == 1


def test_fallback_skips_api_without_the_sdk(monkeypatch, tmp_path, capsys):
    # A key alone does not make api runnable: on a base install the SDK is absent, and
    # announcing a fallback that cannot run would break the notice's promise.
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, api_key="sk-test", api_sdk=False)
    assert run_cli(monkeypatch, ["--diff"]) == 1
    err = capsys.readouterr().err
    assert "backend 'claude' unavailable" in err
    assert "Note: default backend" not in err
    assert ran == []


def test_fallback_skips_opencode_when_effort_is_set(monkeypatch, tmp_path, capsys):
    # opencode rejects --effort, so substituting it would turn the user's request into an
    # error they did not cause; with nothing else left the default's own error stands.
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "opencode")
    assert run_cli(monkeypatch, ["--diff", "--effort", "high"]) == 1
    err = capsys.readouterr().err
    assert "backend 'claude' unavailable" in err
    assert "Note: default backend" not in err
    assert "--effort is not supported" not in err  # not an error the user provoked
    assert ran == []


def test_fallback_uses_opencode_without_effort(monkeypatch, tmp_path, capsys):
    # The same install without --effort: the opencode skip is conditional, not a removal.
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "opencode")
    assert run_main(monkeypatch, ["--diff"]) == 0
    assert ran == [("opencode", "diff")]
    notice = FALLBACK_NOTICE.format(default="claude", mode="diff", chosen="opencode")
    assert capsys.readouterr().err.count(notice) == 1


def test_piped_stdin_resolves_the_diff_default(monkeypatch, tmp_path, capsys):
    # stdin is the one mode-block branch keyed on isatty() rather than a source flag, so
    # it needs its own proof that the diff default is reached.
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("diff --git a b\n+piped\n"))
    _installed(monkeypatch, "codex", "claude")
    assert run_main(monkeypatch, []) == 0
    assert ran == [("claude", "diff")]
    assert "Note: default backend" not in capsys.readouterr().err


def test_no_fallback_notice_when_the_backend_is_explicit(monkeypatch, tmp_path, capsys):
    # The default's absence is irrelevant once the user has named a backend.
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "codex")
    assert run_main(monkeypatch, ["--diff", "--backend", "codex"]) == 0
    assert ran == [("codex", "diff")]
    assert "Note: default backend" not in capsys.readouterr().err


def test_explicit_missing_backend_errors_without_falling_back(monkeypatch, tmp_path, capsys):
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch, "codex")
    assert run_cli(monkeypatch, ["--diff", "--backend", "claude"]) == 1
    err = capsys.readouterr().err
    assert "backend 'claude' unavailable" in err
    assert "Note: default backend" not in err  # an explicit choice is never substituted
    assert ran == []


def test_nothing_available_keeps_the_existing_error(monkeypatch, tmp_path, capsys):
    _mode_sources(tmp_path, monkeypatch)
    ran = _record_backends(monkeypatch)
    _installed(monkeypatch)  # no CLI on PATH, no API key
    assert run_cli(monkeypatch, ["--diff"]) == 1
    err = capsys.readouterr().err
    # Names the backend the mode asked for, with its install hint — the pre-existing error.
    assert "Error: backend 'claude' unavailable — npm install -g @anthropic-ai/claude-code" in err
    assert "Note: default backend" not in err
    assert ran == []


def test_available_requires_both_a_key_and_the_sdk_for_the_api_backend(monkeypatch):
    monkeypatch.setattr("rocket_review.backends.api._load_env_file", lambda: None)
    monkeypatch.setattr("rocket_review.backends._openai_sdk_installed", lambda: True)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert not available("api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert available("api")
    monkeypatch.setattr("rocket_review.backends._openai_sdk_installed", lambda: False)
    assert not available("api")  # a key without the SDK is still not runnable


def test_openai_sdk_probe_fails_closed_on_a_spec_less_module(monkeypatch):
    # A stand-in module (the api backend tests install one) carries no __spec__ and
    # find_spec raises on it; the probe answers "no" instead of propagating.
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace())
    assert not _openai_sdk_installed()


def test_version_flag_prints_the_installed_version(monkeypatch, capsys):
    def fake_version(distribution):
        assert distribution == "rocket-review"
        return "9.8.7"

    monkeypatch.setattr("rocket_review.cli.version", fake_version)
    monkeypatch.setattr("sys.argv", ["rr", "--version"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0
    assert capsys.readouterr().out == "rr 9.8.7\n"


def test_version_falls_back_outside_an_installed_distribution(monkeypatch):
    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr("rocket_review.cli.version", missing)
    assert rr_version() == "unknown (source checkout)"


def test_fanout_single_success_is_byte_identical(monkeypatch, capsys):
    patch_backends(monkeypatch, {"codex": "PROSE REVIEW"})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex"])
    out = capsys.readouterr()
    assert code == 0
    assert out.out == "PROSE REVIEW\n"  # no "## codex" header for a single backend


def test_fanout_all_fail_exits_1(monkeypatch, capsys):
    patch_backends(monkeypatch, {
        "codex": BackendError("codex boom"),
        "claude": BackendError("claude boom"),
    })
    code = run_main(monkeypatch, ["--diff", "--backend", "codex,claude"])
    err = capsys.readouterr().err
    assert code == 1
    assert "codex boom" in err and "claude boom" in err
    assert "some backends failed" not in err  # all-fail exits 1, no partial warning


def test_fanout_partial_fail_warns_and_exits_0(monkeypatch, capsys):
    patch_backends(monkeypatch, {
        "codex": "OK REVIEW",
        "claude": BackendError("claude boom"),
    })
    code = run_main(monkeypatch, ["--diff", "--backend", "codex,claude"])
    out = capsys.readouterr()
    assert code == 0
    assert "OK REVIEW" in out.out  # successful prose stays on stdout
    assert "claude boom" in out.err  # failed backend routed to stderr
    assert "some backends failed" in out.err


def test_effort_with_opencode_errors_before_backends_run(monkeypatch, capsys):
    ran = []

    def review(job):
        ran.append(job)
        return "REVIEW"

    fakes = {
        "codex": types.SimpleNamespace(review=review),
        "opencode": types.SimpleNamespace(review=review),
    }
    monkeypatch.setattr("rocket_review.cli.BACKENDS", fakes)
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    code = run_cli(monkeypatch, ["--diff", "--backend", "codex,opencode", "--effort", "high"])
    err = capsys.readouterr().err
    assert code == 1
    assert "--effort is not supported by the opencode backend" in err
    assert ran == []  # errored before any backend executed


def test_effort_without_opencode_threads_to_job(monkeypatch, capsys):
    captured = {}

    def review(job):
        captured["effort"] = job.effort
        return "REVIEW"

    monkeypatch.setattr("rocket_review.cli.BACKENDS", {"codex": types.SimpleNamespace(review=review)})
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    code = run_main(monkeypatch, ["--diff", "--backend", "codex", "--effort", "medium"])
    assert code == 0
    assert captured["effort"] == "medium"


def test_timeout_zero_errors(monkeypatch, capsys):
    code = run_cli(monkeypatch, ["--diff", "--timeout", "0"])
    assert code != 0
    assert "positive integer" in capsys.readouterr().err


def test_timeout_negative_errors(monkeypatch, capsys):
    code = run_cli(monkeypatch, ["--diff", "--timeout", "-5"])
    assert code != 0
    assert "positive integer" in capsys.readouterr().err


def test_timeout_threads_to_job(monkeypatch, capsys):
    captured = {}

    def review(job):
        captured["timeout"] = job.timeout
        return "REVIEW"

    monkeypatch.setattr("rocket_review.cli.BACKENDS", {"codex": types.SimpleNamespace(review=review)})
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    code = run_main(monkeypatch, ["--diff", "--backend", "codex", "--timeout", "1800"])
    assert code == 0
    assert captured["timeout"] == 1800


def test_timeout_unset_leaves_job_timeout_none(monkeypatch, capsys):
    captured = {}

    def review(job):
        captured["timeout"] = job.timeout
        return "REVIEW"

    monkeypatch.setattr("rocket_review.cli.BACKENDS", {"codex": types.SimpleNamespace(review=review)})
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    code = run_main(monkeypatch, ["--diff", "--backend", "codex"])
    assert code == 0
    assert captured["timeout"] is None


def test_fanout_gate_exits_2(monkeypatch):
    review_json = (
        '{"verdict": "needs_fixes", "summary": "s", "findings": '
        '[{"severity": "high", "title": "t", "file": null, "line": null, '
        '"why": "w", "fix": "f"}]}'
    )
    patch_backends(monkeypatch, {"codex": review_json})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex", "--json", "--fail-on", "high"])
    assert code == 2


def test_fanout_results_in_backend_order(monkeypatch, capsys):
    patch_backends(monkeypatch, {"codex": "FROM_CODEX", "claude": "FROM_CLAUDE"})
    code = run_main(monkeypatch, ["--diff", "--backend", "claude,codex"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.index("## claude") < out.index("## codex")  # --backend order preserved


def test_fanout_unexpected_worker_exception_surfaces_as_backend_error(monkeypatch, capsys):
    def make_review(behavior):
        def review(job):
            if isinstance(behavior, Exception):
                raise behavior
            return behavior
        return review

    fakes = {
        "codex": types.SimpleNamespace(review=make_review(ValueError("boom"))),
        "claude": types.SimpleNamespace(review=make_review("OK REVIEW")),
    }
    monkeypatch.setattr("rocket_review.cli.BACKENDS", fakes)
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)

    code = run_main(monkeypatch, ["--diff", "--backend", "codex,claude"])
    out = capsys.readouterr()
    assert code == 0  # not a traceback crash
    assert "OK REVIEW" in out.out  # sibling's completed review still delivered
    assert "ValueError: boom" in out.err  # surfaced as a backend error, not a crash
    assert "some backends failed" in out.err


def test_blank_backend_output_surfaces_as_an_error(monkeypatch, capsys):
    # A backend that hands back only whitespace produced no review: the CLI must report a
    # failure, not print an empty review and exit 0.
    patch_backends(monkeypatch, {"codex": "   "})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex"])
    out = capsys.readouterr()
    assert code == 1
    assert "codex produced no review output" in out.err


def _capture_jobs(monkeypatch, *backend_names):
    """Swap in fake backends that each record the ReviewJob they are handed, keyed by name."""
    jobs = {}

    def make(name):
        def review(job):
            jobs[name] = job
            return "OK REVIEW"
        return review

    monkeypatch.setattr(
        "rocket_review.cli.BACKENDS",
        {name: types.SimpleNamespace(review=make(name)) for name in backend_names},
    )
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    return jobs


def test_opencode_diff_materializes_content_at_cli(monkeypatch, capsys):
    # opencode can't be trusted to run git itself in a locked-down plan agent, so the CLI
    # must hand it the diff materialized (content set), never content=None.
    marker = "UNIQUE-DIFF-MARKER-9f3a"
    monkeypatch.setattr(
        "rocket_review.cli.get_diff", lambda staged: f"diff --git a b\n+{marker}"
    )
    jobs = _capture_jobs(monkeypatch, "opencode")
    code = run_main(monkeypatch, ["--diff", "--backend", "opencode"])
    assert code == 0
    job = jobs["opencode"]
    assert job.content is not None
    assert marker in job.content
    assert job.git_cmd is None  # not told to fetch the diff itself


def test_codex_diff_stays_agentic_at_cli(monkeypatch, capsys):
    # codex keeps the agentic path (content=None + a git_cmd) — Fix 1 must not regress it.
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    jobs = _capture_jobs(monkeypatch, "codex")
    code = run_main(monkeypatch, ["--diff", "--backend", "codex"])
    assert code == 0
    job = jobs["codex"]
    assert job.content is None
    assert job.git_cmd == "git diff HEAD"


def test_mixed_diff_fanout_shares_single_snapshot(monkeypatch, capsys):
    # When one backend forces materialization, the diff is captured ONCE and the identical
    # bytes go to every backend — no per-backend git re-run that could see a different tree.
    calls = {"n": 0}

    def fake_get_diff(staged):
        calls["n"] += 1
        return f"diff --git a b\n+SNAPSHOT-{calls['n']}"

    monkeypatch.setattr("rocket_review.cli.get_diff", fake_get_diff)
    jobs = _capture_jobs(monkeypatch, "codex", "opencode")
    code = run_main(monkeypatch, ["--diff", "--backend", "codex,opencode"])
    assert code == 0
    assert calls["n"] == 1  # captured once, so no post-capture divergence is possible
    assert jobs["codex"].content == jobs["opencode"].content  # identical bytes
    assert "SNAPSHOT-1" in jobs["codex"].content
    assert jobs["codex"].git_cmd is None and jobs["opencode"].git_cmd is None


def test_get_diff_preserves_trailing_whitespace(monkeypatch):
    # The materialized snapshot must be byte-identical: trailing whitespace on the last
    # changed line is part of the patch and must survive to the reviewer.
    raw = "diff --git a b\n@@ -1 +1 @@\n-x\n+line with trailing spaces   \n"
    monkeypatch.setattr(
        "rocket_review.cli.run_capture",
        lambda cmd: types.SimpleNamespace(returncode=0, stdout=raw, stderr=""),
    )
    assert get_diff(staged=False) == raw


def test_get_commit_diff_preserves_trailing_whitespace(monkeypatch):
    raw = "commit abc123\n@@ -1 +1 @@\n+trailing   \n"
    monkeypatch.setattr(
        "rocket_review.cli.run_capture",
        lambda cmd: types.SimpleNamespace(returncode=0, stdout=raw, stderr=""),
    )
    assert get_commit_diff("abc123") == raw


def test_mixed_commit_fanout_specializes_per_backend(monkeypatch):
    # A commit OID is immutable, so codex keeps it and `git show`s the exact commit while
    # opencode (which can't run git) gets the commit diff materialized.
    oid = "a" * 40
    monkeypatch.setattr("rocket_review.cli.resolve_commit", lambda rev: oid)
    monkeypatch.setattr(
        "rocket_review.cli.get_commit_diff", lambda o: f"commit {o}\n+CSNAP-5d1e"
    )
    jobs = _capture_jobs(monkeypatch, "codex", "opencode")
    code = run_main(monkeypatch, ["--commit", "deadbeef", "--backend", "codex,opencode"])
    assert code == 0
    assert jobs["codex"].content is None
    assert jobs["codex"].commit == oid  # codex git-shows the immutable commit
    assert "CSNAP-5d1e" in jobs["opencode"].content
    assert jobs["opencode"].commit is None  # opencode gets it materialized


def test_keyboardinterrupt_during_fanout_terminates_active_commands(monkeypatch):
    # Ctrl-C reaches the main thread at f.result(); the CLI must tear down the backend
    # process groups before the executor waits on the workers, or it hangs until timeout.
    killed = {"called": False}
    monkeypatch.setattr(
        "rocket_review.cli.base.terminate_active_commands",
        lambda: killed.__setitem__("called", True),
    )

    def review(job):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "rocket_review.cli.BACKENDS",
        {"codex": types.SimpleNamespace(review=review)},
    )
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    monkeypatch.setattr("sys.argv", ["rr", "--diff", "--backend", "codex"])
    with pytest.raises(KeyboardInterrupt):
        main()
    assert killed["called"]


def test_keyboardinterrupt_during_submit_terminates_active_commands(monkeypatch):
    # Ctrl-C can also land part-way through the submit loop, once an earlier worker has
    # already launched a billed backend but before any f.result() is reached. The teardown
    # has to cover that window too, or the backend outlives the CLI and keeps billing.
    launched = threading.Event()
    torn_down = threading.Event()
    killed = {"called": False}

    def terminate():
        killed["called"] = True
        torn_down.set()  # stands in for the signal that unblocks the worker's communicate()

    monkeypatch.setattr("rocket_review.cli.base.terminate_active_commands", terminate)

    def review(job):
        launched.set()
        torn_down.wait(5)  # a live backend process: only the teardown ends it
        return "review"

    monkeypatch.setattr(
        "rocket_review.cli.BACKENDS",
        {
            "codex": types.SimpleNamespace(review=review),
            "claude": types.SimpleNamespace(review=lambda job: "unreached"),
        },
    )

    submits = {"n": 0}

    class InterruptingPool(ThreadPoolExecutor):
        """Raises on the second submit, forcing the window without real signals."""

        def submit(self, *args, **kwargs):
            if submits["n"]:
                assert launched.wait(5), "first backend never started"
                raise KeyboardInterrupt
            submits["n"] += 1
            return super().submit(*args, **kwargs)

    monkeypatch.setattr("rocket_review.cli.ThreadPoolExecutor", InterruptingPool)
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    monkeypatch.setattr("sys.argv", ["rr", "--diff", "--backend", "codex,claude"])
    with pytest.raises(KeyboardInterrupt):
        main()
    assert killed["called"]


def test_keyboardinterrupt_before_launch_stops_a_worker_from_starting_a_backend(monkeypatch):
    # The other half of the leak: a worker accepted by the pool but not yet at its Popen()
    # is invisible to the teardown snapshot. Unless it is told to give up it launches a
    # backend nothing is left to reap, and the executor's non-daemon threads then hold the
    # CLI open — still billing — for that backend's full timeout.
    MARKER = "rr-test-backend"
    parked = threading.Event()
    torn_down = threading.Event()
    launches = {"n": 0}

    real_popen = subprocess.Popen

    def spy_popen(cmd, *args, **kwargs):
        # Counts only this test's backend, so unrelated subprocess use (git, etc.) can't
        # be mistaken for the launch under test.
        if any(MARKER in part for part in cmd):
            launches["n"] += 1
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr("rocket_review.backends.base.subprocess.Popen", spy_popen)

    real_terminate = base.terminate_active_commands

    def terminate():
        real_terminate()
        torn_down.set()  # releases the parked worker exactly after the teardown snapshot

    monkeypatch.setattr("rocket_review.cli.base.terminate_active_commands", terminate)

    def interrupting_review(job):
        assert parked.wait(5), "the other worker never parked"
        raise KeyboardInterrupt

    def parked_review(job):
        parked.set()
        assert torn_down.wait(5), "teardown never ran"
        return base.run_command([sys.executable, "-c", f"pass  # {MARKER}"])

    monkeypatch.setattr(
        "rocket_review.cli.BACKENDS",
        {
            "codex": types.SimpleNamespace(review=interrupting_review),
            "claude": types.SimpleNamespace(review=parked_review),
        },
    )
    monkeypatch.setattr("rocket_review.cli.missing_binary", lambda name: None)
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: False)
    monkeypatch.setattr("rocket_review.cli.ensure_diff_exists", lambda staged: None)
    monkeypatch.setattr("sys.argv", ["rr", "--diff", "--backend", "codex,claude"])
    with pytest.raises(KeyboardInterrupt):
        main()
    assert launches["n"] == 0  # gave up at the gate instead of launching post-teardown


def test_interrupt_gate_is_cleared_for_each_fanout(monkeypatch):
    # The gate latches, so a run that ended in an interrupt must not make the next run in
    # the same process refuse to launch anything.
    base.request_interrupt()
    patch_backends(monkeypatch, {"codex": "review"})
    assert run_main(monkeypatch, ["--diff", "--backend", "codex"]) == 0
    assert not base.interrupted()


def _json_envelope(monkeypatch, capsys, review_output, extra_args=()):
    patch_backends(monkeypatch, {"codex": review_output})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex", "--json", *extra_args])
    return code, json.loads(capsys.readouterr().out)


def test_json_truncates_long_raw_inline_without_spilling_to_disk(monkeypatch, capsys):
    import tempfile

    def boom(*a, **k):
        raise AssertionError("truncation must not spill review text to a temp file")

    # Any attempt to create a spill file fails the test outright — no reliance on scanning
    # the shared system temp dir (which a concurrent process could perturb).
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", boom)
    long = REVIEW_JSON.format(body="x" * 5000)
    assert len(long) > RAW_TRUNCATE_LIMIT
    code, env = _json_envelope(monkeypatch, capsys, long)
    assert code == 0
    r = env["results"][0]
    assert "(truncated," in r["raw"] and "use --full to inline" in r["raw"]
    assert str(len(long)) in r["raw"]  # marker names the full length
    assert len(r["raw"]) < len(long)
    assert r["raw_file"] is None  # nothing written to disk; no secret-leaking spill file


def test_json_short_raw_untouched(monkeypatch, capsys):
    short = REVIEW_JSON.format(body="short fix")
    assert len(short) <= RAW_TRUNCATE_LIMIT
    code, env = _json_envelope(monkeypatch, capsys, short)
    r = env["results"][0]
    assert r["raw"] == short
    assert r["raw_file"] is None


def test_truncate_raw_keeps_exactly_the_limit_prefix():
    # The kept text is the first RAW_TRUNCATE_LIMIT characters verbatim: an off-by-one
    # would drop a character off the tail of every truncated review unnoticed.
    raw = "".join(str(i % 10) for i in range(RAW_TRUNCATE_LIMIT + 500))
    result = BackendResult(backend="codex", model=None, raw=raw)
    truncate_raw([result])
    kept, _, _ = result.raw.partition("\n(truncated,")
    assert kept == raw[:RAW_TRUNCATE_LIMIT]


def test_json_full_flag_inlines_everything(monkeypatch, capsys):
    long = REVIEW_JSON.format(body="x" * 5000)
    code, env = _json_envelope(monkeypatch, capsys, long, extra_args=["--full"])
    r = env["results"][0]
    assert r["raw"] == long
    assert r["raw_file"] is None
    assert "(truncated" not in r["raw"]


def test_help_shows_examples(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["rr", "--help"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0
    assert "examples:" in capsys.readouterr().out


def test_text_mode_prints_stderr_hint(monkeypatch, capsys):
    patch_backends(monkeypatch, {"codex": "PROSE REVIEW"})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex"])
    out = capsys.readouterr()
    assert code == 0
    assert TEXT_HINT in out.err  # next-step hint on stderr
    assert TEXT_HINT not in out.out  # never pollutes piped stdout


def test_json_mode_omits_hint(monkeypatch, capsys):
    review = REVIEW_JSON.format(body="f")
    patch_backends(monkeypatch, {"codex": review})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex", "--json"])
    out = capsys.readouterr()
    assert code == 0
    assert TEXT_HINT not in out.err and TEXT_HINT not in out.out


def test_text_mode_all_fail_omits_hint(monkeypatch, capsys):
    patch_backends(monkeypatch, {"codex": BackendError("codex boom")})
    code = run_main(monkeypatch, ["--diff", "--backend", "codex"])
    out = capsys.readouterr()
    assert code == 1
    assert TEXT_HINT not in out.err  # no next-step hint on the error path


def test_diff_and_staged_conflict(monkeypatch, capsys):
    code = run_cli(monkeypatch, ["--diff", "--staged"])
    assert code == 1
    assert "only one of --diff or --staged" in capsys.readouterr().err


def test_full_requires_json(monkeypatch, capsys):
    code = run_cli(monkeypatch, ["--diff", "--full"])
    assert code == 1
    assert "--full requires --json" in capsys.readouterr().err


def test_repo_requires_pr(monkeypatch, capsys):
    code = run_cli(monkeypatch, ["--diff", "--repo", "acme/api"])
    assert code == 1
    assert "--repo only applies with --pr" in capsys.readouterr().err


def test_piped_stdin_with_an_explicit_source_is_rejected(monkeypatch, capsys):
    # Piped content is a review source of its own, so it counts alongside --diff and the
    # pair must be refused instead of one of them being silently ignored.
    patch_backends(monkeypatch, {"codex": "PROSE REVIEW"})
    monkeypatch.setattr("rocket_review.cli.stdin_has_input", lambda: True)
    code = run_main(monkeypatch, ["--diff", "--backend", "codex"])
    assert code == 1
    assert "only one review source" in capsys.readouterr().err


def test_stdin_has_input_true_for_a_pipe(monkeypatch):
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd) as piped_stdin:
            monkeypatch.setattr(sys, "stdin", piped_stdin)
            assert stdin_has_input()
    finally:
        os.close(write_fd)


def test_stdin_has_input_true_for_a_redirected_regular_file(monkeypatch, tmp_path):
    path = tmp_path / "changes.diff"
    path.write_text("diff --git a b\n")
    with path.open() as redirected_stdin:
        monkeypatch.setattr(sys, "stdin", redirected_stdin)
        assert stdin_has_input()


def test_stdin_has_input_false_for_a_tty(monkeypatch):
    # A real pty: an interactive stdin carries nothing to review, so it must not count as
    # a source and collide with an explicit one.
    controller_fd, terminal_fd = os.openpty()
    try:
        with os.fdopen(terminal_fd) as tty_stdin:
            monkeypatch.setattr(sys, "stdin", tty_stdin)
            assert not stdin_has_input()
    finally:
        os.close(controller_fd)


def _git_repo(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.email=t@t.io", "-c", "user.name=t",
             "-c", "commit.gpgsign=false", *args],
            cwd=tmp_path, check=True, capture_output=True,
        )
    (tmp_path / "f.txt").write_text("one\n")
    git("init", "-q")
    git("add", "f.txt")
    git("commit", "-q", "-m", "init")


def test_ensure_diff_exists_clean_tree_errors(tmp_path, monkeypatch, capsys):
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        ensure_diff_exists(False)
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "no uncommitted changes" in err
    # An empty diff most often means the work was just committed, so the error
    # names the flag that reviews it rather than leaving the reader to find
    # --commit in --help.
    assert "rr --commit HEAD" in err


def test_ensure_diff_exists_clean_index_points_at_all_three_places(
    tmp_path, monkeypatch, capsys
):
    # An empty --staged is ambiguous in a way an empty --diff is not: the work
    # may be unstaged or already committed, so the hint has to cover both.
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        ensure_diff_exists(True)
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "no staged changes" in err
    assert "git add" in err
    assert "rr --diff" in err
    assert "rr --commit HEAD" in err


def test_ensure_diff_exists_dirty_tree_passes(tmp_path, monkeypatch):
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("two\n")
    ensure_diff_exists(False)  # must not exit


def test_resolve_commit_unknown_sha_errors(tmp_path, monkeypatch, capsys):
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        resolve_commit("deadbeef")
    assert "unknown commit" in capsys.readouterr().err


def test_resolve_commit_head_returns_full_oid(tmp_path, monkeypatch):
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    oid = resolve_commit("HEAD")
    assert len(oid) == 40 and all(c in "0123456789abcdef" for c in oid)


def test_resolve_commit_rejects_option_shaped_revision(tmp_path, monkeypatch, capsys):
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        resolve_commit("--no-patch")
    assert "invalid commit revision" in capsys.readouterr().err


def test_run_capture_replaces_non_utf8_output():
    result = run_capture([
        sys.executable, "-c",
        "import sys; sys.stdout.buffer.write(b'ok \\xff end')",
    ])
    assert result.returncode == 0
    assert "ok" in result.stdout and "end" in result.stdout  # no UnicodeDecodeError
