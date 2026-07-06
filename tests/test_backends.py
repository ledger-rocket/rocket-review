import pytest

from rocket_review.backends import BACKENDS, api, base, claude, codex, opencode
from rocket_review.backends.base import BackendError, ReviewJob


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

    def fake_call(content, system_prompt, model, extra):
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


def test_api_json_mode_adds_output_override_to_system_prompt(monkeypatch):
    captured = {}

    def fake_call(content, system_prompt, model, extra):
        captured["system_prompt"] = system_prompt
        return "ok"

    monkeypatch.setattr(api, "_call_openai", fake_call)
    api.review(job(json_output=True))
    assert "OUTPUT FORMAT OVERRIDE" in captured["system_prompt"]


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
    assert "--model" in cmd and "claude-sonnet-5" in cmd
    assert "DIFF TO REVIEW" in captured["stdin"]  # prompt travels via stdin, not argv


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
        return "OPENCODE REVIEW"

    monkeypatch.setattr(base, "run_command", fake_run)
    assert opencode.review(job(model="google/gemini-3-pro")) == "OPENCODE REVIEW"
    cmd = captured["cmd"]
    assert cmd[:2] == ["opencode", "run"]
    assert "--agent" in cmd and "plan" in cmd
    assert "--model" in cmd and "google/gemini-3-pro" in cmd
    assert "Do not modify any files" in cmd[-1]


def test_opencode_empty_output_raises(monkeypatch):
    monkeypatch.setattr(base, "run_command", lambda cmd, *, stdin=None, timeout=900: "  ")
    with pytest.raises(BackendError):
        opencode.review(job())
