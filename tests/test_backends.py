import threading
import types

import pytest

from rocket_review.backends import BACKENDS, api, base, claude, codex, opencode
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.models import REVIEW_SCHEMA


def job(**kw):
    defaults = dict(mode="diff", content="diff --git a b", docs_content=None,
                    extra=None, commit=None, pr=False, git_cmd=None,
                    model=None, json_output=False)
    defaults.update(kw)
    return ReviewJob(**defaults)


def test_registry_has_codex_and_api():
    assert "codex" in BACKENDS and "api" in BACKENDS


def test_codex_builds_readonly_exec_command(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        # emulate codex writing the -o output file
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write("REVIEW TEXT")
        return ""

    monkeypatch.setattr(base, "run_command", fake_run)
    result = codex.review(job(model="gpt-5.5"))
    assert result == "REVIEW TEXT"
    assert captured["cmd"][:4] == ["codex", "exec", "-s", "read-only"]
    assert "-m" in captured["cmd"] and "gpt-5.5" in captured["cmd"]


def test_codex_default_models():
    assert codex.DEFAULT_MODEL is None
    assert api.DEFAULT_MODEL == "gpt-5.6-terra"


def test_codex_default_model_omits_flag(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write("REVIEW")
        return ""

    monkeypatch.setattr(base, "run_command", fake_run)
    codex.review(job())
    assert "-m" not in captured["cmd"]


def test_codex_effort_inserts_reasoning_config(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write("REVIEW")
        return ""

    monkeypatch.setattr(base, "run_command", fake_run)
    codex.review(job(effort="high"))
    cmd = captured["cmd"]
    idx = cmd.index("-c")
    assert cmd[idx + 1] == "model_reasoning_effort=high"
    # the -c pair precedes the positional prompt (the last argv element)
    assert idx + 1 < len(cmd) - 1


def test_codex_no_effort_omits_reasoning_config(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write("REVIEW")
        return ""

    monkeypatch.setattr(base, "run_command", fake_run)
    codex.review(job())
    assert "model_reasoning_effort=" not in " ".join(captured["cmd"])


def test_claude_effort_appends_flag(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(base, "run_command", fake_run)
    claude.review(job(effort="xhigh"))
    cmd = captured["cmd"]
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "xhigh"


def test_claude_no_effort_omits_flag(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(base, "run_command", fake_run)
    claude.review(job())
    assert "--effort" not in captured["cmd"]


class _FakeOpenAI:
    """Captures constructor + responses.create kwargs and any models.list() call so the
    api backend's request bounding and canonical-model short-circuit can be asserted."""

    last_init_kwargs = None
    last_create_kwargs = None
    listed = False
    next_response = None

    def __init__(self, **kwargs):
        _FakeOpenAI.last_init_kwargs = kwargs

    class models:
        @staticmethod
        def list():
            _FakeOpenAI.listed = True
            return []

    class responses:
        @staticmethod
        def create(**kwargs):
            _FakeOpenAI.last_create_kwargs = kwargs
            if _FakeOpenAI.next_response is not None:
                return _FakeOpenAI.next_response
            return type("R", (), {"output_text": "ok"})()


def _response(**fields):
    """A stand-in Response carrying only the fields the backend reads off one."""
    return types.SimpleNamespace(
        **{"status": "completed", "output": [], "output_text": "", **fields}
    )


def _install_fake_openai(monkeypatch):
    _FakeOpenAI.last_init_kwargs = None
    _FakeOpenAI.last_create_kwargs = None
    _FakeOpenAI.listed = False
    monkeypatch.setattr(_FakeOpenAI, "next_response", None)
    fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAI)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(api, "_load_env_file", lambda: None)
    monkeypatch.setattr(api, "extract_referenced_files", lambda content: "")


def test_api_effort_passes_reasoning_kwarg(monkeypatch):
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "gpt-5.6-terra", None, "low")
    assert _FakeOpenAI.last_create_kwargs["reasoning"] == {"effort": "low"}


def test_api_no_effort_omits_reasoning_kwarg(monkeypatch):
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "gpt-5.6-terra", None, None)
    assert "reasoning" not in _FakeOpenAI.last_create_kwargs


def test_api_timeout_passes_timeout_kwarg(monkeypatch):
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "gpt-5.6-terra", None, None, 1800)
    # the response call gets the budget left after (near-instant) resolution
    assert 0 < _FakeOpenAI.last_create_kwargs["timeout"] <= 1800


def test_api_no_timeout_omits_timeout_kwarg(monkeypatch):
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "gpt-5.6-terra", None, None, None)
    assert "timeout" not in _FakeOpenAI.last_create_kwargs


def test_api_missing_openai_sdk_raises_precise_error(monkeypatch):
    # Base install carries no OpenAI SDK; the api backend must fail with an actionable
    # BackendError, not leak a bare ImportError. sys.modules[...] = None makes the lazy
    # `from openai import OpenAI` raise ImportError.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(api, "_load_env_file", lambda: None)
    monkeypatch.setitem(__import__("sys").modules, "openai", None)
    with pytest.raises(BackendError) as exc:
        api._call_openai("content", "instructions", "gpt-5.6-terra", None)
    msg = str(exc.value)
    assert "OpenAI SDK" in msg and "rocket-review[api]" in msg


def test_api_review_threads_job_timeout_into_call(monkeypatch):
    _install_fake_openai(monkeypatch)
    api.review(job(timeout=42))
    assert 0 < _FakeOpenAI.last_create_kwargs["timeout"] <= 42


def test_api_timeout_deadline_shared_across_resolution_and_create(monkeypatch):
    # Resolution and the response call share one budget: time spent listing models is
    # subtracted from what responses.create() gets, so the total stays within --timeout.
    _install_fake_openai(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

    def slow_list():
        clock[0] += 20  # resolution burns 20s of the 30s budget
        _FakeOpenAI.listed = True
        return []

    monkeypatch.setattr(_FakeOpenAI.models, "list", slow_list)
    api._call_openai("content", "instructions", "o5-mini", None, None, 30)
    assert _FakeOpenAI.last_create_kwargs["timeout"] == pytest.approx(10)


def test_api_timeout_exhausted_by_resolution_fails_closed(monkeypatch):
    # If resolution alone consumes the whole budget, fail rather than starting an
    # unbounded response call.
    _install_fake_openai(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

    def slow_list():
        clock[0] += 30
        return []

    monkeypatch.setattr(_FakeOpenAI.models, "list", slow_list)
    with pytest.raises(BackendError, match="timed out"):
        api._call_openai("content", "instructions", "o5-mini", None, None, 30)


def test_api_client_bounds_every_call_when_timeout_set(monkeypatch):
    # The whole backend (incl. any listing) must sit under --timeout: the client carries
    # the deadline and disables retries so nothing silently backs off past it.
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "gpt-5.6-sol", None, None, 1800)
    assert _FakeOpenAI.last_init_kwargs == {
        "api_key": "sk-test", "max_retries": 0, "timeout": 1800,
    }


def test_api_client_keeps_retries_without_timeout(monkeypatch):
    # No deadline to protect: the SDK's default retries stay on for reliability.
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "gpt-5.6-sol", None, None, None)
    assert _FakeOpenAI.last_init_kwargs == {"api_key": "sk-test"}


def test_api_timeout_preserves_alias_resolution(monkeypatch):
    # A deadline must not change which model is selected: a non-canonical alias still
    # resolves (the list call is simply bounded by the client timeout), so adding --timeout
    # can never turn a working model name into a failing one.
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "o5-mini", None, None, 30)
    assert _FakeOpenAI.listed


def test_api_canonical_model_skips_models_list(monkeypatch):
    _install_fake_openai(monkeypatch)
    for canonical in ("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        _FakeOpenAI.listed = False
        api._call_openai("content", "instructions", canonical, None, None, None)
        assert not _FakeOpenAI.listed
        assert _FakeOpenAI.last_create_kwargs["model"] == canonical


def test_api_dated_snapshot_skips_models_list(monkeypatch):
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "gpt-5.6-2026-01-01", None, None, None)
    assert not _FakeOpenAI.listed


def test_api_aliased_model_still_lists(monkeypatch):
    # A genuinely short/aliased name that isn't canonical keeps the resolution path.
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "o5-mini", None, None, None)
    assert _FakeOpenAI.listed


def test_api_alias_resolves_to_the_newest_dated_snapshot(monkeypatch):
    # With several snapshots of one alias available, the newest wins — an alias must not
    # silently pin the review to a stale build.
    _install_fake_openai(monkeypatch)
    monkeypatch.setattr(_FakeOpenAI.models, "list", lambda: [
        types.SimpleNamespace(id=model_id)
        for model_id in ("o5-mini-2026-01-01", "o5-mini-2026-06-01", "o5-mini-2025-09-01")
    ])
    api._call_openai("content", "instructions", "o5-mini", None)
    assert _FakeOpenAI.last_create_kwargs["model"] == "o5-mini-2026-06-01"


def test_codex_empty_output_raises(monkeypatch):
    def fake_run(cmd, *, stdin=None, timeout=900):
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write("")
        return ""

    monkeypatch.setattr(base, "run_command", fake_run)
    with pytest.raises(BackendError):
        codex.review(job())


def test_api_review_prepends_docs_and_passes_model_extra(monkeypatch):
    captured = {}

    def fake_call(content, system_prompt, model, extra, effort=None, timeout=None,
                  json_output=False):
        captured.update(content=content, system_prompt=system_prompt, model=model, extra=extra)
        return "ok"

    monkeypatch.setattr(api, "_call_openai", fake_call)
    result = api.review(job(
        content="diff", docs_content="standard", extra="focus security", model="gpt-test",
    ))
    assert result == "ok"
    assert captured["content"].startswith("=== PROJECT STANDARDS ===\nstandard")
    assert captured["content"].endswith("diff")
    assert captured["model"] == "gpt-test"
    assert captured["extra"] == "focus security"
    assert captured["system_prompt"]


def test_api_missing_key_raises_backend_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(api, "_load_env_file", lambda: None)
    with pytest.raises(BackendError):
        api._call_openai("content", "instructions", "gpt-test", None)


def test_codex_json_mode_passes_output_schema(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write('{"verdict": "approve", "summary": "s", "findings": []}')
        return ""

    monkeypatch.setattr(base, "run_command", fake_run)
    codex.review(job(json_output=True))
    assert "--output-schema" in captured["cmd"]


def test_api_json_mode_puts_the_json_format_in_the_system_prompt(monkeypatch):
    captured = {}

    def fake_call(content, system_prompt, model, extra, effort=None, timeout=None,
                  json_output=False):
        captured["system_prompt"] = system_prompt
        captured["json_output"] = json_output
        return "ok"

    monkeypatch.setattr(api, "_call_openai", fake_call)
    api.review(job(json_output=True))
    assert "JSON RESPONSE FORMAT" in captured["system_prompt"]
    assert captured["json_output"] is True


def test_api_json_mode_requests_structured_output_against_review_schema(monkeypatch):
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "gpt-5.6-terra", None, json_output=True)
    fmt = _FakeOpenAI.last_create_kwargs["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    # Identity, not equality: a copy could drift from the schema the envelope parser and
    # the other backends validate against.
    assert fmt["schema"] is REVIEW_SCHEMA


def test_api_prose_mode_requests_no_response_format(monkeypatch):
    _install_fake_openai(monkeypatch)
    api._call_openai("content", "instructions", "gpt-5.6-terra", None)
    assert "text" not in _FakeOpenAI.last_create_kwargs


def test_api_json_mode_reaches_the_request_from_a_job(monkeypatch):
    _install_fake_openai(monkeypatch)
    api.review(job(json_output=True))
    assert _FakeOpenAI.last_create_kwargs["text"]["format"]["schema"] is REVIEW_SCHEMA


def test_api_refusal_raises_instead_of_returning_empty_text(monkeypatch):
    _install_fake_openai(monkeypatch)
    _FakeOpenAI.next_response = _response(output=[
        types.SimpleNamespace(type="reasoning"),  # no content parts at all
        types.SimpleNamespace(content=[
            types.SimpleNamespace(type="refusal", refusal="cannot review this"),
        ]),
    ])
    with pytest.raises(BackendError, match="refused the request: cannot review this"):
        api._call_openai("content", "instructions", "gpt-5.6-terra", None, json_output=True)


@pytest.mark.parametrize(
    "response, fragments",
    [
        (_response(status="incomplete", output_text="half a review",
                   incomplete_details=types.SimpleNamespace(reason="max_output_tokens")),
         ("unfinished", "incomplete", "max_output_tokens", "partial output discarded")),
        (_response(status="failed",
                   error=types.SimpleNamespace(message="server had a bad day")),
         ("unfinished", "failed", "server had a bad day", "partial output discarded")),
        (_response(status="incomplete"), ("unfinished", "incomplete", "partial output discarded")),
    ],
)
def test_api_unfinished_response_raises_naming_the_cause(monkeypatch, response, fragments):
    # A truncated or failed response must not reach the caller as a review: its partial
    # text would be reported as the model's answer. Prose loses that fragment, so the
    # error has to say the output was dropped rather than imply nothing was produced.
    _install_fake_openai(monkeypatch)
    _FakeOpenAI.next_response = response
    with pytest.raises(BackendError) as exc:
        api._call_openai("content", "instructions", "gpt-5.6-terra", None, json_output=True)
    for fragment in fragments:
        assert fragment in str(exc.value)
    assert "half a review" not in str(exc.value)


def test_api_empty_output_raises(monkeypatch):
    _install_fake_openai(monkeypatch)
    _FakeOpenAI.next_response = _response(output_text="   ")
    with pytest.raises(BackendError, match="empty response"):
        api._call_openai("content", "instructions", "gpt-5.6-terra", None)


def test_api_call_failure_becomes_a_backend_error(monkeypatch):
    _install_fake_openai(monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(_FakeOpenAI.responses, "create", boom)
    with pytest.raises(BackendError, match="OpenAI API call failed: upstream exploded"):
        api._call_openai("content", "instructions", "gpt-5.6-terra", None)


def test_claude_uses_readonly_allowlist_and_stdin(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        captured["stdin"] = stdin
        return "CLAUDE REVIEW"

    monkeypatch.setattr(base, "run_command", fake_run)
    assert claude.review(job(model="claude-sonnet-5")) == "CLAUDE REVIEW"
    cmd = captured["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    allow = cmd[cmd.index("--allowedTools") + 1]
    assert "Read" in allow and "Write" not in allow and "Edit" not in allow
    # The allowlist is only restrictive under manual permission mode: without it,
    # headless Claude Code auto-approves unlisted (mutating) tools.
    assert cmd[cmd.index("--permission-mode") + 1] == "manual"
    # Never a wildcard git rule: git diff/show/log all accept --output=<file>, so a
    # `Bash(git diff:*)` allow rule would be a write vector. This job inlines content
    # (no git_cmd/commit), so no Bash rule at all.
    assert "Bash(git" not in allow
    assert "--model" in cmd and "claude-sonnet-5" in cmd
    assert "DIFF TO REVIEW" in captured["stdin"]  # prompt travels via stdin, not argv


def test_claude_git_view_rule_is_exact_match_not_wildcard(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(base, "run_command", fake_run)

    # --diff/--staged: the exact git command is allow-listed so --output can't be appended.
    claude.review(job(content=None, git_cmd="git diff HEAD"))
    allow = captured["cmd"][captured["cmd"].index("--allowedTools") + 1]
    assert "Bash(git diff HEAD)" in allow and "Bash(git diff:*)" not in allow

    # --commit: git-show the exact reviewed OID, nothing wider.
    claude.review(job(content=None, commit="deadbeef"))
    allow = captured["cmd"][captured["cmd"].index("--allowedTools") + 1]
    assert "Bash(git show deadbeef)" in allow and "Bash(git show:*)" not in allow


def test_claude_default_model_omits_flag(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(base, "run_command", fake_run)
    claude.review(job())
    assert "--model" not in captured["cmd"]


def test_opencode_run_command_with_model(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        captured["stdin"] = stdin
        return "OPENCODE REVIEW"

    monkeypatch.setattr(base, "run_command", fake_run)
    assert opencode.review(job(model="google/gemini-3-pro")) == "OPENCODE REVIEW"
    cmd = captured["cmd"]
    assert cmd[:2] == ["opencode", "run"]
    assert "--agent" in cmd and "plan" in cmd
    assert "--model" in cmd and "google/gemini-3-pro" in cmd
    assert "Do not modify any files" in captured["stdin"]  # prompt travels via stdin


def test_opencode_empty_output_raises(monkeypatch):
    monkeypatch.setattr(base, "run_command", lambda cmd, *, stdin=None, timeout=900: "  ")
    with pytest.raises(BackendError):
        opencode.review(job())


def test_opencode_delivers_materialized_content_via_stdin(monkeypatch):
    # The prompt (with the diff embedded) reaches the model on stdin — not as a path the
    # read-only plan agent must open (which it may be denied, then review nothing), not
    # inline in argv (ps-visible, ARG_MAX-bounded), and not via --file (opencode's
    # array-valued flag swallows the message, and its Read tool truncates large attachments).
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        captured["stdin"] = stdin
        return "OPENCODE REVIEW"

    monkeypatch.setattr(base, "run_command", fake_run)
    marker = "MATERIALIZED-DIFF-9f3a2b"
    opencode.review(job(content=f"diff --git a b\n+{marker}"))
    assert marker in captured["stdin"]  # full diff delivered on stdin
    assert "--file" not in captured["cmd"]
    assert marker not in " ".join(captured["cmd"])  # not exposed in argv
    # no positional message, so opencode reads the prompt from stdin
    assert captured["cmd"][-1] == "plan"


def test_extract_referenced_files_blocks_sibling_prefix_escape(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    sibling = tmp_path / "repo-secret"
    repo.mkdir()
    sibling.mkdir()
    (sibling / "leak.py").write_text("SECRET = 1")
    (repo / "ok.py").write_text("OK = 1")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(api, "_get_repo_root", lambda: repo.resolve())
    out = api.extract_referenced_files("see `ok.py` and `../repo-secret/leak.py`")
    assert "OK = 1" in out
    assert "SECRET" not in out  # /repo-secret must not pass as inside /repo


def test_extract_referenced_files_includes_a_file_of_exactly_max_size(tmp_path, monkeypatch):
    # The cap is inclusive: a file landing exactly on the limit is still attached, so the
    # boundary can't quietly drop context the reviewer was told to read.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "exact.py").write_text("E" * 64)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(api, "_get_repo_root", lambda: repo.resolve())
    out = api.extract_referenced_files("see `exact.py`", max_size=64)
    assert "E" * 64 in out


def test_run_command_replaces_non_utf8_output():
    import sys

    out = base.run_command([
        sys.executable, "-c",
        "import sys; sys.stdout.buffer.write(b'review \\xff done')",
    ])
    assert "review" in out and "done" in out  # no UnicodeDecodeError


class _FakeProc:
    """Stands in for a Popen handle. `first` is raised by the initial communicate();
    later communicate() calls (the reap after a kill) return quietly."""

    def __init__(self, pid=4321, first=None, returncode=0):
        self.pid = pid
        self.returncode = returncode
        self._first = first
        self._calls = 0
        self.communicate_timeouts = []

    def communicate(self, input=None, timeout=None):
        self._calls += 1
        if self._calls == 1:
            self.communicate_timeouts.append(timeout)
            if self._first is not None:
                raise self._first
        return ("", "")


def _patch_group_signals(monkeypatch):
    """Record every real (pgid, signal) sent; report the fake group dead to liveness probes
    (killpg(pid, 0)) so terminate escalation doesn't spin on a nonexistent process."""
    signals = []

    def fake_killpg(pgid, sig):
        if sig == 0:
            raise ProcessLookupError
        signals.append((pgid, sig))

    monkeypatch.setattr(base.os, "killpg", fake_killpg)
    return signals


def test_run_command_timeout_message_uses_seconds_when_not_whole_minutes(monkeypatch):
    proc = _FakeProc(first=base.subprocess.TimeoutExpired(cmd="x", timeout=30))
    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **k: proc)
    _patch_group_signals(monkeypatch)
    with pytest.raises(BackendError, match=r"timed out after 30 seconds"):
        base.run_command(["x"], timeout=30)


def test_run_command_timeout_message_uses_minutes_for_whole_minutes(monkeypatch):
    proc = _FakeProc(first=base.subprocess.TimeoutExpired(cmd="x", timeout=120))
    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **k: proc)
    _patch_group_signals(monkeypatch)
    with pytest.raises(BackendError, match=r"timed out after 2 minutes"):
        base.run_command(["x"], timeout=120)


def test_run_command_timeout_kills_process_group(monkeypatch):
    proc = _FakeProc(first=base.subprocess.TimeoutExpired(cmd="x", timeout=30))
    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **k: proc)
    signals = _patch_group_signals(monkeypatch)
    with pytest.raises(BackendError):
        base.run_command(["x"], timeout=30)
    assert (proc.pid, base.signal.SIGTERM) in signals


def test_run_command_keyboardinterrupt_kills_group_and_propagates(monkeypatch):
    proc = _FakeProc(first=KeyboardInterrupt())
    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **k: proc)
    signals = _patch_group_signals(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        base.run_command(["sleep"], timeout=900)
    assert (proc.pid, base.signal.SIGTERM) in signals  # group torn down, not left running


def test_run_command_unexpected_error_kills_and_unregisters(monkeypatch):
    # An OSError (or any exception) from communicate() must still tear the group down and
    # drop it from the registry, not leak a running child.
    proc = _FakeProc(first=OSError("boom"))
    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **k: proc)
    signals = _patch_group_signals(monkeypatch)
    with pytest.raises(OSError):
        base.run_command(["x"], timeout=30)
    assert (proc.pid, base.signal.SIGTERM) in signals
    assert proc not in base._active_procs


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0 seconds"),
        (1, "1 second"),
        (45, "45 seconds"),
        (60, "1 minute"),
        (120, "2 minutes"),
        (900, "15 minutes"),
    ],
)
def test_format_duration_boundaries(seconds, expected):
    assert base.format_duration(seconds) == expected


def test_run_command_zero_timeout_falsy_still_honored_by_codex_and_claude_null_check(monkeypatch):
    # Regression: backends must use an explicit `is None` check, not truthiness,
    # so a programmatically-constructed job(timeout=0) is not silently coerced to 900.
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["timeout"] = timeout
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write("REVIEW")
        return ""

    monkeypatch.setattr(base, "run_command", fake_run)
    codex.review(job(timeout=0))
    assert captured["timeout"] == 0


def test_terminate_active_commands_signals_registered_groups(monkeypatch):
    signals = _patch_group_signals(monkeypatch)  # group reports dead after SIGTERM
    proc = _FakeProc(pid=555)
    base._active_procs.add(proc)
    try:
        base.terminate_active_commands()
    finally:
        base._active_procs.discard(proc)
    assert (555, base.signal.SIGTERM) in signals  # main-thread teardown reaches the group
    assert (555, base.signal.SIGKILL) not in signals  # died on SIGTERM, no escalation


class _LockWatcher:
    """The real lock, announcing when a named thread is about to block on it.

    Lets a test park inside the launch section and still know the teardown has reached the
    lock, without sleeping to guess at it.
    """

    def __init__(self, real, thread_name, at_lock):
        self._real = real
        self._thread_name = thread_name
        self._at_lock = at_lock

    def __enter__(self):
        if threading.current_thread().name == self._thread_name:
            self._at_lock.set()
        self._real.acquire()
        return self

    def __exit__(self, *exc):
        self._real.release()


def test_teardown_cannot_snapshot_between_a_launch_and_its_registration(monkeypatch):
    # The invariant behind the gate: check, launch and register happen under ONE acquisition
    # of the lock terminate_active_commands() snapshots under. A teardown arriving mid-launch
    # therefore waits and then sees the new process. Split into two acquisitions it would
    # snapshot the gap instead and leave that process running — which every other test in
    # this suite still passes through, so it is pinned here explicitly.
    proc_pid = 9090
    in_launch = threading.Event()
    teardown_at_lock = threading.Event()
    snapshot_done = threading.Event()
    signals = _patch_group_signals(monkeypatch)

    class _ParkedProc(_FakeProc):
        def communicate(self, input=None, timeout=None):
            # Holds the worker where a real backend sits while the teardown runs, so the
            # registration cannot be undone before the snapshot is taken.
            snapshot_done.wait(5)
            return super().communicate(input=input, timeout=timeout)

    proc = _ParkedProc(pid=proc_pid)

    def parked_popen(*args, **kwargs):
        in_launch.set()
        teardown_at_lock.wait(5)  # give the teardown its chance to reach the lock
        return proc

    monkeypatch.setattr(base.subprocess, "Popen", parked_popen)
    monkeypatch.setattr(
        base, "_active_lock", _LockWatcher(base._active_lock, "teardown", teardown_at_lock)
    )

    def teardown():
        assert in_launch.wait(5), "the launch never started"
        base.terminate_active_commands()
        snapshot_done.set()

    thread = threading.Thread(target=teardown, name="teardown")
    thread.start()
    try:
        base.run_command(["backend"])
    finally:
        thread.join(10)
    assert not thread.is_alive()
    # Signalled at all == it was in the snapshot: the teardown waited out the launch section.
    assert (proc_pid, base.signal.SIGTERM) in signals


def test_run_command_refuses_to_launch_once_interrupted(monkeypatch):
    # The teardown only reaches processes that already exist, so a worker arriving at the
    # gate afterwards has to give up rather than start one nobody is left to reap.
    launched = []
    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **k: launched.append(a))
    base.request_interrupt()
    with pytest.raises(BackendError, match="interrupted before launch"):
        base.run_command(["echo", "hi"])
    assert not launched


def test_api_refuses_to_call_once_interrupted(monkeypatch):
    # An HTTP request has no process group to signal, so refusing to start one is the only
    # way the api backend can honour an interrupt.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    base.request_interrupt()
    with pytest.raises(BackendError, match="interrupted before launch"):
        api._call_openai("content", "instructions", "gpt-5.6-terra", None)


def test_begin_fanout_reopens_the_gate():
    base.request_interrupt()
    assert base.interrupted()
    base.begin_fanout()
    assert not base.interrupted()


def test_terminate_active_commands_escalates_to_sigkill(monkeypatch):
    # A group that survives SIGTERM must be SIGKILLed so it can't block the exit.
    monkeypatch.setattr(base, "_TERM_GRACE_SECONDS", 0.0)
    signals = []
    monkeypatch.setattr(  # every probe/ signal succeeds → group stays "alive"
        base.os, "killpg", lambda pgid, sig: signals.append((pgid, sig))
    )
    proc = _FakeProc(pid=777)
    base._active_procs.add(proc)
    try:
        base.terminate_active_commands()
    finally:
        base._active_procs.discard(proc)
    assert (777, base.signal.SIGTERM) in signals
    assert (777, base.signal.SIGKILL) in signals


def test_run_command_nonzero_exit_raises_at_the_base_layer(monkeypatch):
    # The failure is caught where the process is run, not only where a backend is called:
    # a non-zero exit must never return the child's partial stdout as a review.
    proc = _FakeProc(returncode=3)
    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **k: proc)
    _patch_group_signals(monkeypatch)
    with pytest.raises(BackendError, match=r"x failed \(exit 3\)"):
        base.run_command(["x"])


def test_run_command_unregisters_proc_after_run(monkeypatch):
    proc = _FakeProc(returncode=0)
    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **k: proc)
    _patch_group_signals(monkeypatch)
    base.run_command(["echo", "hi"])
    assert proc not in base._active_procs  # no leak into the active-process registry


def test_run_command_passes_timeout_to_subprocess(monkeypatch):
    proc = _FakeProc(returncode=0)
    monkeypatch.setattr(base.subprocess, "Popen", lambda *a, **k: proc)
    _patch_group_signals(monkeypatch)
    base.run_command(["echo", "hi"], timeout=1800)
    assert proc.communicate_timeouts == [1800]  # deadline bounds the wait


def test_run_command_uses_new_session_for_group_kill(monkeypatch):
    captured = {}
    proc = _FakeProc(returncode=0)

    def fake_popen(*a, **k):
        captured.update(k)
        return proc

    monkeypatch.setattr(base.subprocess, "Popen", fake_popen)
    _patch_group_signals(monkeypatch)
    base.run_command(["echo", "hi"])
    assert captured["start_new_session"] is True


def test_codex_default_timeout_is_900(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["timeout"] = timeout
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write("REVIEW")
        return ""

    monkeypatch.setattr(base, "run_command", fake_run)
    codex.review(job())
    assert captured["timeout"] == 900


def test_codex_custom_timeout_passed_through(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["timeout"] = timeout
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as f:
            f.write("REVIEW")
        return ""

    monkeypatch.setattr(base, "run_command", fake_run)
    codex.review(job(timeout=1800))
    assert captured["timeout"] == 1800


def test_claude_default_timeout_is_900(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(base, "run_command", fake_run)
    claude.review(job())
    assert captured["timeout"] == 900


def test_claude_custom_timeout_passed_through(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(base, "run_command", fake_run)
    claude.review(job(timeout=1800))
    assert captured["timeout"] == 1800


def test_opencode_default_timeout_is_900(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(base, "run_command", fake_run)
    opencode.review(job())
    assert captured["timeout"] == 900


def test_opencode_custom_timeout_passed_through(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(base, "run_command", fake_run)
    opencode.review(job(timeout=1800))
    assert captured["timeout"] == 1800
