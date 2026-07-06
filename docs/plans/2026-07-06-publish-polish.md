# rocket-review Publish & Multi-Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `rr` publishable and add multi-backend reviews (Codex + Claude Code + opencode + API) with a unified `--docs` standards flag, structured JSON output, and a CI-gateable `--fail-on` exit code.

**Architecture:** Extract the two existing backends into a `backends/` package behind a single `review(job) -> str` protocol, then add `claude` and `opencode` backends on the same harness. A new `models.py` owns the Finding/BackendResult dataclasses, tolerant JSON extraction, and the severity gate. `cli.py` orchestrates N backends concurrently via `ThreadPoolExecutor` and renders text sections or a merged JSON envelope. `--docs` becomes the primary standards flag (auto-discovery + link-following), with `--llms` kept as a compatibility alias.

**Tech Stack:** Python 3.13, stdlib only for new code (`openai` SDK stays the sole runtime dep, used only by the `api` backend), pytest + ruff as dev deps.

## Global Constraints

- Python `>=3.13`; no new runtime dependencies — new features use stdlib only.
- Backends must never modify the reviewed project: codex uses `-s read-only`, claude uses a read-only `--allowedTools` list, opencode is prompt-instructed read-only (+ agent flag if its CLI offers one).
- Existing CLI surface keeps working unchanged: `rr --diff`, `--staged`, `--commit`, `--pr`, `--docs`, `--mode`, `--prompt`, positional files, stdin pipe. `--llms` keeps working as an alias of `--docs llms.txt`; `--api` remains as an alias for `--backend api`.
- Tests run offline: every test mocks `rocket_review.backends.base.run_command` (or higher); pytest must never invoke a real CLI or the network.
- Exit codes: `0` = success, `1` = operational failure (bad args, all backends failed), `2` = `--fail-on` severity gate tripped.
- Per-backend subprocess timeout stays 900 seconds.
- Commits: plain Conventional Commits (`feat:`/`fix:`/`test:`/`refactor:`/`docs:`/`chore:`) — this repo does not use Linear scopes (see git history).
- All work happens in `/Users/stepan/Projects/rocket-review` on a feature branch off `main`.

---

### Task 1: Commit the pending working-tree changes

The tree already contains good uncommitted work: the `--repo` flag for `--pr`, the stdin/explicit-source mutual-exclusivity fix, and the `gpt-5.5` default bump. Land it before anything else so later refactors diff cleanly.

**Files:**
- Modify (already modified, just commit): `rocket_review/cli.py`, `rocket_review/review.py`

**Interfaces:**
- Produces: baseline `main` state all later tasks branch from.

- [ ] **Step 1: Inspect exactly what is pending**

Run: `git -C /Users/stepan/Projects/rocket-review diff`
Expected: hunks limited to `--repo` argparse plumbing + `get_pr_content(pr_ref, repo)`, the `explicit_sources`/`has_stdin` counting logic, and `DEFAULT_MODEL = "gpt-5.5"`. If anything else appears, stop and report.

- [ ] **Step 2: Smoke-test the CLI**

Run: `cd /Users/stepan/Projects/rocket-review && python -m rocket_review --help`
Expected: exit 0, help text shows `--repo OWNER/REPO`.

- [ ] **Step 3: Create the working branch and commit**

```bash
cd /Users/stepan/Projects/rocket-review
git checkout -b publish-polish
git add rocket_review/cli.py rocket_review/review.py
git commit -m "feat: add --repo for cross-repo PR review, fix stdin source detection, default gpt-5.5"
```

---

### Task 2: Test scaffolding + self-contained CI

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/test_cli.py`, `tests/test_llms.py`, `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `rocket_review.cli.detect_mode(paths) -> str`, `rocket_review.cli.read_llms(llms_path) -> str` (existing; renamed in Task 4).
- Produces: `pytest` + `ruff` runnable via `pip install -e ".[dev]"`; CI workflow that runs them (later tasks rely on `pytest -q` passing).

- [ ] **Step 1: Add dev extras and license metadata to pyproject.toml**

Append to `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.6"]

[tool.ruff]
line-length = 100
target-version = "py313"
```

and add `license = "MIT"` plus `readme = "README.md"` under `[project]`.

- [ ] **Step 2: Install dev deps**

Run: `cd /Users/stepan/Projects/rocket-review && pip install -e ".[dev]"`
Expected: exit 0.

- [ ] **Step 3: Write tests for the existing pure helpers**

`tests/test_cli.py`:

```python
from rocket_review.cli import detect_mode


def test_detect_mode_plan_for_markdown():
    assert detect_mode(["docs/plan.md"]) == "plan"
    assert detect_mode(["a.md", "b.txt", "c.plan"]) == "plan"


def test_detect_mode_code_for_source_files():
    assert detect_mode(["src/auth.py"]) == "code"


def test_detect_mode_mixed_is_code():
    assert detect_mode(["plan.md", "src/auth.py"]) == "code"
```

`tests/test_llms.py` (renamed to `tests/test_docs.py` with updated imports in Task 4):

```python
import pytest

from rocket_review.cli import read_llms


def test_read_llms_follows_relative_links(tmp_path):
    (tmp_path / "standards.md").write_text("# Standards\nno global mutable state")
    llms = tmp_path / "llms.txt"
    llms.write_text("# Project\n[standards](standards.md)\n[web](https://x.io/a.md)")
    out = read_llms(llms)
    assert "no global mutable state" in out
    assert "--- standards.md ---" in out


def test_read_llms_skips_traversal_outside_base(tmp_path, capsys):
    secret = tmp_path / "secret.md"
    secret.write_text("s3cret")
    project = tmp_path / "project"
    project.mkdir()
    llms = project / "llms.txt"
    llms.write_text("[up](../secret.md)")
    out = read_llms(llms)
    assert "s3cret" not in out
    assert "outside project" in capsys.readouterr().err


def test_read_llms_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        read_llms(tmp_path / "nope.txt")
```

- [ ] **Step 4: Run the tests**

Run: `pytest -q`
Expected: 6 passed.

- [ ] **Step 5: Run ruff and fix anything it flags**

Run: `ruff check .`
Expected: exit 0 (fix trivial findings inline if any; do not add ignores).

- [ ] **Step 6: Add self-contained CI workflow**

`.github/workflows/ci.yml` (leave the org-synced `minimal-ci.yml` in place — it is fleet-managed; swapping it out is a publish-time decision, Task 11):

```yaml
---
name: CI

'on':
  pull_request:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - run: pip install -e ".[dev]"
      - run: ruff check .
      - run: pytest -q
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml tests/ .github/workflows/ci.yml
git commit -m "test: add pytest+ruff scaffolding and self-contained CI"
```

---

### Task 3: Extract the backends package (pure refactor)

**Files:**
- Create: `rocket_review/backends/__init__.py`, `rocket_review/backends/base.py`, `rocket_review/backends/codex.py`, `rocket_review/backends/api.py`
- Modify: `rocket_review/cli.py`, `rocket_review/prompts.py`
- Delete: `rocket_review/review.py`
- Test: `tests/test_backends.py`

**Interfaces:**
- Produces (later tasks depend on these exact names):
  - `base.ReviewJob` dataclass: `mode: str`, `content: str | None`, `docs_content: str | None`, `extra: str | None`, `commit: str | None`, `pr: bool`, `git_cmd: str | None`, `model: str | None`, `json_output: bool = False`
  - `base.run_command(cmd: list[str], *, stdin: str | None = None, timeout: int = 900) -> str` (raises `base.BackendError`)
  - `base.write_prompt_file(prompt: str) -> Path`
  - Each backend module exposes: `NAME: str`, `BINARY: str | None`, `INSTALL_HINT: str`, `review(job: ReviewJob) -> str`
  - `backends.BACKENDS: dict[str, module]`, `backends.missing_binary(name) -> str | None`
  - `prompts.build_agent_prompt(job: ReviewJob) -> str` (renamed from `build_codex_prompt`)

- [ ] **Step 1: Write the failing tests**

`tests/test_backends.py`:

```python
import pytest

from rocket_review.backends import BACKENDS, base, codex
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_backends.py -q`
Expected: FAIL — `ModuleNotFoundError: rocket_review.backends`.

- [ ] **Step 3: Create `rocket_review/backends/base.py`**

```python
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

TIMEOUT = 900


class BackendError(Exception):
    """A backend failed to produce a review."""


@dataclass
class ReviewJob:
    mode: str
    content: str | None
    docs_content: str | None
    extra: str | None
    commit: str | None
    pr: bool
    git_cmd: str | None
    model: str | None
    json_output: bool = False


def run_command(cmd: list[str], *, stdin: str | None = None, timeout: int = TIMEOUT) -> str:
    try:
        result = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise BackendError(f"{cmd[0]} timed out after {timeout // 60} minutes")
    except FileNotFoundError:
        raise BackendError(f"{cmd[0]} not found on PATH")
    if result.returncode != 0:
        raise BackendError(
            f"{cmd[0]} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def write_prompt_file(prompt: str) -> Path:
    # File indirection instead of argv keeps large prompts under ARG_MAX.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, prefix="rr-prompt-",
    ) as f:
        f.write(prompt)
        return Path(f.name)
```

- [ ] **Step 4: Create `rocket_review/backends/codex.py`** (logic moved verbatim from `review.py`, reshaped to the protocol; `--output-schema` comes in Task 6)

```python
import tempfile
from pathlib import Path

from rocket_review.backends import base
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.prompts import build_agent_prompt

NAME = "codex"
BINARY = "codex"
INSTALL_HINT = "npm install -g @openai/codex (https://github.com/openai/codex)"
DEFAULT_MODEL = "gpt-5.5"


def review(job: ReviewJob) -> str:
    prompt_file = base.write_prompt_file(build_agent_prompt(job))
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        outfile = Path(f.name)
    try:
        cmd = ["codex", "exec", "-s", "read-only", "-o", str(outfile),
               "-m", job.model or DEFAULT_MODEL,
               f"Read the file {prompt_file} for your full instructions, then follow them."]
        base.run_command(cmd)
        output = outfile.read_text().strip()
        if not output:
            raise BackendError("codex produced no output")
        return output
    finally:
        outfile.unlink(missing_ok=True)
        prompt_file.unlink(missing_ok=True)
```

- [ ] **Step 5: Create `rocket_review/backends/api.py`**

Move `_load_env_file`, `_get_repo_root`, `extract_referenced_files`, `_resolve_model`, and the body of `review_with_api` from `review.py` unchanged, wrapped as:

```python
NAME = "api"
BINARY = None  # SDK, not a CLI
INSTALL_HINT = "set OPENAI_API_KEY (or put it in .env)"
DEFAULT_MODEL = "gpt-5.5"


def review(job: ReviewJob) -> str:
    content = job.content or ""
    if job.docs_content:
        content = (f"=== PROJECT STANDARDS ===\n{job.docs_content}\n"
                   f"=== END PROJECT STANDARDS ===\n\n{content}")
    system_prompt = get_prompt(job.mode, job.docs_content, job.json_output)
    return _call_openai(content, system_prompt, job.model or DEFAULT_MODEL, job.extra)
```

where `_call_openai` is the existing `review_with_api` body (env loading, model resolution, referenced-file extraction, `client.responses.create`), changed to `raise BackendError(...)` instead of `sys.exit(1)` on failure.

- [ ] **Step 6: Create `rocket_review/backends/__init__.py`**

```python
import shutil

from rocket_review.backends import api, codex

BACKENDS = {m.NAME: m for m in (codex, api)}


def missing_binary(name: str) -> str | None:
    """Return an install hint if the backend's CLI is absent, else None."""
    mod = BACKENDS[name]
    if mod.BINARY and shutil.which(mod.BINARY) is None:
        return mod.INSTALL_HINT
    return None
```

(Tasks 7–8 extend the import and the `BACKENDS` tuple with `claude` and `opencode`.)

- [ ] **Step 7: Rename `build_codex_prompt` → `build_agent_prompt` in `prompts.py`**

New signature `build_agent_prompt(job: ReviewJob) -> str` — same body, reading `job.mode`, `job.content`, `job.docs_content`, `job.extra`, `job.commit`, `job.pr`, `job.git_cmd`, and add the sentence `"Do not modify any files."` to the read-access paragraph. `prompts.py` imports `ReviewJob` from `backends.base` (one-directional, no cycle).

- [ ] **Step 8: Rewire `cli.py` and delete `review.py`**

In `cli.py`: build a `ReviewJob` from the parsed args, replace `review_with_codex(...)`/`review_with_api(...)` calls with `BACKENDS["codex"].review(job)` / `BACKENDS["api"].review(job)`; replace the `HAS_CODEX` check with `missing_binary("codex")`; catch `BackendError` at the top level and exit 1 with its message. Then `git rm rocket_review/review.py`.

- [ ] **Step 9: Run the full suite**

Run: `pytest -q && ruff check .`
Expected: all pass (Task 2's tests still green — behavior unchanged).

- [ ] **Step 10: Live smoke test (behavior parity)**

Run: `cd /Users/stepan/Projects/rocket-review && rr README.md --mode plan --model gpt-5.5-mini 2>&1 | tail -5`
Expected: a real review arrives (proves the codex path still works end-to-end).

- [ ] **Step 11: Commit**

```bash
git add rocket_review/ tests/test_backends.py
git rm rocket_review/review.py 2>/dev/null || true
git commit -m "refactor: extract backends package behind ReviewJob protocol"
```

---

### Task 4: Unify `--docs`/`--llms` into one standards flag

`--docs` becomes the primary flag: explicit paths, or bare for auto-discovery of the standards files people actually have (`llms.txt`, `AGENTS.md`, `CLAUDE.md`). Every doc read follows its relative markdown links one level (the current llms.txt behavior, generalized). `--llms` stays as a compatibility alias so existing invocations (`rr --diff --llms`) keep working byte-for-byte.

**Files:**
- Modify: `rocket_review/cli.py`
- Test: rename `tests/test_llms.py` → `tests/test_docs.py` (updated imports + new cases)

**Interfaces:**
- Consumes: nothing new.
- Produces (used by `main()` and documented in Task 10's README):
  - `cli.read_doc_with_links(doc_path: Path) -> str` (renamed from `read_llms`; same traversal guard)
  - `cli.collect_docs(docs_args: list[str] | None, llms_arg: str | None) -> str | None`
  - `cli.DISCOVERY_CANDIDATES = ["llms.txt", "AGENTS.md", "CLAUDE.md"]`

- [ ] **Step 1: Write the failing tests**

`git mv tests/test_llms.py tests/test_docs.py`, change imports from `read_llms` to `read_doc_with_links` (keeping the three existing cases, renamed `test_read_doc_*`), and append:

```python
from rocket_review.cli import collect_docs


def test_collect_docs_bare_discovers_candidates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("agents rules")
    (tmp_path / "CLAUDE.md").write_text("claude rules")
    out = collect_docs([], None)
    assert "agents rules" in out and "claude rules" in out


def test_collect_docs_bare_none_found_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        collect_docs([], None)
    assert "none of" in capsys.readouterr().err


def test_collect_docs_explicit_missing_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        collect_docs(["nope.md"], None)


def test_collect_docs_llms_alias_combines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "llms.txt").write_text("llms index")
    (tmp_path / "extra.md").write_text("extra doc")
    out = collect_docs(["extra.md"], "llms.txt")
    assert "llms index" in out and "extra doc" in out


def test_collect_docs_dedupes_repeated_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "llms.txt").write_text("llms index")
    out = collect_docs(["llms.txt"], "llms.txt")
    assert out.count("llms index") == 1


def test_collect_docs_nothing_given_is_none():
    assert collect_docs(None, None) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_docs.py -q`
Expected: FAIL — `ImportError: cannot import name 'read_doc_with_links'` (and `collect_docs`).

- [ ] **Step 3: Implement in `cli.py`**

Rename `read_llms` → `read_doc_with_links` (generalize the first part's label from the hardcoded `--- llms.txt ---` to `f"--- {doc_path.name} ---"`; error message becomes `f"Error: {doc_path} not found."`). Delete the old plain `read_docs()` helper. Add:

```python
DISCOVERY_CANDIDATES = ["llms.txt", "AGENTS.md", "CLAUDE.md"]


def collect_docs(docs_args: list[str] | None, llms_arg: str | None) -> str | None:
    """Assemble standards context from --docs (explicit or auto-discovered) and --llms."""
    paths: list[Path] = []
    if docs_args is not None and len(docs_args) == 0:
        found = [Path(c) for c in DISCOVERY_CANDIDATES if Path(c).is_file()]
        if not found:
            print(
                "Error: --docs given without paths and none of "
                f"{', '.join(DISCOVERY_CANDIDATES)} found in the current directory.",
                file=sys.stderr,
            )
            sys.exit(1)
        paths.extend(found)
    elif docs_args:
        paths.extend(Path(p) for p in docs_args)
    if llms_arg:
        paths.append(Path(llms_arg))
    if not paths:
        return None
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return "\n\n".join(read_doc_with_links(p) for p in unique)
```

argparse changes:

```python
parser.add_argument(
    "--docs", nargs="*", metavar="PATH",
    help="Project standards docs to review against; relative markdown links inside them are "
         "followed one level. With no PATH, auto-discovers llms.txt / AGENTS.md / CLAUDE.md.",
)
parser.add_argument(
    "--llms", nargs="?", const="llms.txt", metavar="PATH",
    help="Alias for --docs llms.txt (kept for compatibility)",
)
```

and in `main()` replace the current two-branch docs block with:

```python
docs_content = collect_docs(args.docs, args.llms)
```

Behavior notes (deliberate changes vs today): explicit `--docs` paths that don't exist now **error** instead of warn-and-skip (you asked for them; silently reviewing without them is a silent fallback), and explicit `--docs` files now get one level of link-following (an index doc behaves like llms.txt).

- [ ] **Step 4: Run the suite**

Run: `pytest -q && ruff check .`
Expected: all pass.

- [ ] **Step 5: Backward-compat verification**

Run: `cd /Users/stepan/Projects/rocket-review && rr --help | grep -A2 -E 'docs|llms'` and, in a repo with an `llms.txt` (e.g. any LedgerRocket service checkout), `rr <somefile> --llms --mode code --model gpt-5.5-mini | tail -3`.
Expected: help shows both flags with `--llms` marked as alias; the `--llms` invocation still produces a standards-aware review.

- [ ] **Step 6: Commit**

```bash
git add rocket_review/cli.py tests/
git commit -m "feat: unify --docs/--llms into one standards flag with auto-discovery"
```

---

### Task 5: Findings model + JSON extraction + severity gate

**Files:**
- Create: `rocket_review/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `Finding` dataclass: `severity, title, file, line, why, fix, backend, model`
  - `BackendResult` dataclass: `backend, model, verdict, summary, findings, raw, error, parse_error`
  - `extract_json(text: str) -> dict | None`
  - `parse_backend_output(text: str, backend: str, model: str | None) -> BackendResult`
  - `should_fail(results: list[BackendResult], threshold: str) -> bool`
  - `to_envelope(results: list[BackendResult]) -> dict`
  - `SEVERITIES = ["critical", "high", "medium", "low"]`
  - `REVIEW_SCHEMA: dict` (JSON Schema for the findings object, used by codex `--output-schema` in Task 6)

- [ ] **Step 1: Write the failing tests**

`tests/test_models.py`:

```python
from rocket_review.models import (
    BackendResult, Finding, extract_json, parse_backend_output, should_fail, to_envelope,
)

GOOD = '{"verdict": "needs_fixes", "summary": "s", "findings": [{"severity": "HIGH", "title": "t", "file": "a.py", "line": 3, "why": "w", "fix": "f"}]}'


def test_extract_json_plain():
    assert extract_json(GOOD)["verdict"] == "needs_fixes"


def test_extract_json_fenced():
    assert extract_json(f"preamble\n```json\n{GOOD}\n```\ntrailer")["summary"] == "s"


def test_extract_json_prose_wrapped():
    assert extract_json(f"Here is my review: {GOOD} Hope it helps!") is not None


def test_extract_json_garbage_is_none():
    assert extract_json("no json here { broken") is None


def test_parse_normalizes_severity_and_tags_backend():
    r = parse_backend_output(GOOD, "codex", "gpt-5.5")
    assert not r.parse_error
    assert r.findings[0].severity == "high"
    assert r.findings[0].backend == "codex" and r.findings[0].model == "gpt-5.5"


def test_parse_failure_keeps_raw():
    r = parse_backend_output("plain text review", "claude", None)
    assert r.parse_error and r.raw == "plain text review" and r.findings == []


def test_should_fail_threshold():
    r = parse_backend_output(GOOD, "codex", None)
    assert should_fail([r], "high")
    assert should_fail([r], "low")          # high finding trips a lower bar too
    assert not should_fail([r], "critical")  # bar above the worst finding


def test_should_fail_unknown_severity_is_conservative():
    txt = GOOD.replace("HIGH", "bananas")
    assert should_fail([parse_backend_output(txt, "codex", None)], "critical")


def test_should_fail_closed_on_errors():
    assert should_fail([BackendResult(backend="codex", model=None, error="boom")], "critical")
    assert should_fail([parse_backend_output("not json", "codex", None)], "critical")


def test_envelope_merges_and_tags():
    r1 = parse_backend_output(GOOD, "codex", "gpt-5.5")
    r2 = parse_backend_output(GOOD, "claude", "claude-sonnet-5")
    env = to_envelope([r1, r2])
    assert len(env["results"]) == 2
    assert len(env["findings"]) == 2
    assert {f["backend"] for f in env["findings"]} == {"codex", "claude"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_models.py -q`
Expected: FAIL — `ModuleNotFoundError: rocket_review.models`.

- [ ] **Step 3: Implement `rocket_review/models.py`**

```python
import json
import re
from dataclasses import asdict, dataclass, field

SEVERITIES = ["critical", "high", "medium", "low"]

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "needs_fixes", "blocker"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "title": {"type": "string"},
                    "file": {"type": ["string", "null"]},
                    "line": {"type": ["integer", "null"]},
                    "why": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "title", "file", "line", "why", "fix"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "summary", "findings"],
    "additionalProperties": False,
}


@dataclass
class Finding:
    severity: str
    title: str
    file: str | None = None
    line: int | None = None
    why: str | None = None
    fix: str | None = None
    backend: str | None = None
    model: str | None = None


@dataclass
class BackendResult:
    backend: str
    model: str | None
    verdict: str | None = None
    summary: str | None = None
    findings: list[Finding] = field(default_factory=list)
    raw: str = ""
    error: str | None = None
    parse_error: bool = False


def extract_json(text: str) -> dict | None:
    candidates = []
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_backend_output(text: str, backend: str, model: str | None) -> BackendResult:
    obj = extract_json(text)
    if obj is None or not isinstance(obj.get("findings"), list):
        return BackendResult(backend=backend, model=model, raw=text, parse_error=True)
    findings = []
    for f in obj["findings"]:
        if not isinstance(f, dict) or "severity" not in f or "title" not in f:
            return BackendResult(backend=backend, model=model, raw=text, parse_error=True)
        findings.append(Finding(
            severity=str(f["severity"]).lower(),
            title=str(f["title"]),
            file=f.get("file"),
            line=f["line"] if isinstance(f.get("line"), int) else None,
            why=f.get("why"),
            fix=f.get("fix"),
            backend=backend,
            model=model,
        ))
    return BackendResult(
        backend=backend, model=model,
        verdict=obj.get("verdict"), summary=obj.get("summary"),
        findings=findings, raw=text,
    )


def _severity_rank(severity: str) -> int:
    # Unknown severities rank as critical: an LLM inventing a label must not slip the gate.
    s = severity.lower()
    return SEVERITIES.index(s) if s in SEVERITIES else 0


def should_fail(results: list[BackendResult], threshold: str) -> bool:
    # Fail closed: a backend that errored or produced unparsable output blocks the gate.
    if any(r.error or r.parse_error for r in results):
        return True
    limit = SEVERITIES.index(threshold)
    return any(
        _severity_rank(f.severity) <= limit
        for r in results for f in r.findings
    )


def to_envelope(results: list[BackendResult]) -> dict:
    return {
        "results": [asdict(r) for r in results],
        "findings": [asdict(f) for r in results for f in r.findings],
    }
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_models.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add rocket_review/models.py tests/test_models.py
git commit -m "feat: findings model, tolerant JSON extraction, fail-closed severity gate"
```

---

### Task 6: `--json` output mode and `--fail-on` gate

**Files:**
- Modify: `rocket_review/prompts.py`, `rocket_review/backends/codex.py`, `rocket_review/cli.py`
- Test: `tests/test_cli.py`, `tests/test_backends.py`

**Interfaces:**
- Consumes: Task 5's `parse_backend_output`, `should_fail`, `to_envelope`, `REVIEW_SCHEMA`.
- Produces: `get_prompt(mode, docs_content=None, json_output=False)`; `rr ... --json [--fail-on SEV]` CLI behavior; codex passes `--output-schema` when `job.json_output`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backends.py`:

```python
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
```

Append to `tests/test_cli.py`:

```python
import pytest

from rocket_review.cli import main


def run_cli(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["rr", *argv])
    with pytest.raises(SystemExit) as e:
        main()
    return e.value.code


def test_fail_on_requires_json(monkeypatch, capsys):
    code = run_cli(monkeypatch, ["--diff", "--fail-on", "high"])
    assert code == 1
    assert "--fail-on requires --json" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify failures**

Run: `pytest tests/test_cli.py tests/test_backends.py -q`
Expected: the two new tests FAIL (unknown `--fail-on` flag; no `--output-schema` in cmd).

- [ ] **Step 3: Add the JSON addendum to `prompts.py`**

```python
JSON_OUTPUT_ADDENDUM = """\
OUTPUT FORMAT OVERRIDE
Ignore the output format instructions above. Output ONLY a single JSON object — no prose
before or after it, no markdown fence — matching exactly this shape:
{
  "verdict": "approve" | "needs_fixes" | "blocker",
  "summary": "recap in at most 200 words",
  "findings": [
    {
      "severity": "critical" | "high" | "medium" | "low",
      "title": "one-line issue statement",
      "file": "path/to/file or null",
      "line": 123 or null,
      "why": "why it matters",
      "fix": "concrete suggested fix, copy-pasteable when possible"
    }
  ]
}
An empty findings array with verdict "approve" is a valid review.
"""
```

and change `get_prompt`:

```python
def get_prompt(mode: str, docs_content: str | None = None, json_output: bool = False) -> str:
    prompt = {"plan": PLAN_REVIEW_PROMPT, "code": CODE_REVIEW_PROMPT, "diff": DIFF_REVIEW_PROMPT}[mode]
    if docs_content:
        prompt += PROJECT_STANDARDS_ADDENDUM
    if json_output:
        prompt += JSON_OUTPUT_ADDENDUM
    return prompt
```

`build_agent_prompt` passes `job.json_output` through to `get_prompt`.

- [ ] **Step 4: Wire `--output-schema` into the codex backend**

In `codex.py`, when `job.json_output`, dump `REVIEW_SCHEMA` to a temp file and extend the command before the prompt argument:

```python
schema_file = None
if job.json_output:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as sf:
        json.dump(REVIEW_SCHEMA, sf)
        schema_file = Path(sf.name)
    cmd += ["--output-schema", str(schema_file)]
```

(add `schema_file.unlink(missing_ok=True)` to the `finally` block when set).

- [ ] **Step 5: Add `--json` and `--fail-on` to `cli.py`**

```python
parser.add_argument("--json", action="store_true",
                    help="Emit findings as a JSON envelope instead of prose")
parser.add_argument("--fail-on", choices=["critical", "high", "medium", "low"],
                    help="Exit 2 if any finding is at or above this severity (requires --json)")
```

validation right after parsing:

```python
if args.fail_on and not args.json:
    print("Error: --fail-on requires --json (findings must be parsed to be gated).", file=sys.stderr)
    sys.exit(1)
```

and at the end of `main()` (single-backend for now; fan-out generalizes this in Task 9):

```python
raw = backend_module.review(job)
if args.json:
    result = parse_backend_output(raw, backend_name, job.model)
    print(json.dumps(to_envelope([result]), indent=2))
    if args.fail_on and should_fail([result], args.fail_on):
        sys.exit(2)
else:
    print(raw)
```

- [ ] **Step 6: Run the suite**

Run: `pytest -q && ruff check .`
Expected: all pass.

- [ ] **Step 7: Live verification of the JSON path**

Run: `cd /Users/stepan/Projects/rocket-review && rr --staged --json 2>/dev/null | python -m json.tool | head -20` (stage any small change first, e.g. a README line)
Expected: valid JSON envelope with `results` and `findings` keys.

- [ ] **Step 8: Commit**

```bash
git add rocket_review/ tests/
git commit -m "feat: --json structured output and --fail-on severity gate"
```

---

### Task 7: Claude Code backend

**Files:**
- Create: `rocket_review/backends/claude.py`
- Modify: `rocket_review/backends/__init__.py`
- Test: `tests/test_backends.py`

**Interfaces:**
- Consumes: `base.run_command` (stdin variant), `prompts.build_agent_prompt`.
- Produces: `BACKENDS["claude"]` with `NAME="claude"`, `BINARY="claude"`, `review(job)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backends.py`:

```python
from rocket_review.backends import claude


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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_backends.py -q`
Expected: FAIL — `ImportError: cannot import name 'claude'`.

- [ ] **Step 3: Implement `rocket_review/backends/claude.py`**

```python
from rocket_review.backends import base
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.prompts import build_agent_prompt

NAME = "claude"
BINARY = "claude"
INSTALL_HINT = "npm install -g @anthropic-ai/claude-code (https://claude.com/claude-code)"
DEFAULT_MODEL = None  # honor the user's Claude Code default model

# Read-only review sandbox: exploration tools plus non-mutating git.
READ_ONLY_TOOLS = (
    "Read Glob Grep "
    "Bash(git diff:*) Bash(git show:*) Bash(git log:*) Bash(git status:*) Bash(ls:*)"
)


def review(job: ReviewJob) -> str:
    cmd = ["claude", "-p", "--allowedTools", READ_ONLY_TOOLS]
    if job.model:
        cmd += ["--model", job.model]
    # Prompt goes via stdin: no ARG_MAX concern and no temp file needed.
    output = base.run_command(cmd, stdin=build_agent_prompt(job)).strip()
    if not output:
        raise BackendError("claude produced no output")
    return output
```

- [ ] **Step 4: Register it**

In `backends/__init__.py` add `claude` to the import and to the `BACKENDS` tuple: `(codex, claude, api)`.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_backends.py -q`
Expected: all pass.

- [ ] **Step 6: Live verification**

`--backend` arrives in Task 9; until then verify via a direct call:

Run: `cd /Users/stepan/Projects/rocket-review && python -c "from rocket_review.backends import claude; from rocket_review.backends.base import ReviewJob; print(claude.review(ReviewJob(mode='code', content=open('rocket_review/models.py').read(), docs_content=None, extra=None, commit=None, pr=False, git_cmd=None, model=None)))" | tail -20`
Expected: a real Claude review of `models.py` prints.

- [ ] **Step 7: Commit**

```bash
git add rocket_review/backends/ tests/test_backends.py
git commit -m "feat: Claude Code backend (claude -p, read-only tool allowlist)"
```

---

### Task 8: opencode backend

**Files:**
- Create: `rocket_review/backends/opencode.py`
- Modify: `rocket_review/backends/__init__.py`
- Test: `tests/test_backends.py`

**Interfaces:**
- Consumes: `base.run_command`, `base.write_prompt_file`, `prompts.build_agent_prompt`.
- Produces: `BACKENDS["opencode"]`; models addressed as `provider/model` (e.g. `anthropic/claude-sonnet-5`, `google/gemini-3-pro`, `ollama/qwen3`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backends.py`:

```python
from rocket_review.backends import opencode


def test_opencode_run_command_with_model(monkeypatch):
    captured = {}

    def fake_run(cmd, *, stdin=None, timeout=900):
        captured["cmd"] = cmd
        return "OPENCODE REVIEW"

    monkeypatch.setattr(base, "run_command", fake_run)
    assert opencode.review(job(model="google/gemini-3-pro")) == "OPENCODE REVIEW"
    cmd = captured["cmd"]
    assert cmd[:2] == ["opencode", "run"]
    assert "--model" in cmd and "google/gemini-3-pro" in cmd
    assert "Do not modify any files" in cmd[-1]


def test_opencode_empty_output_raises(monkeypatch):
    monkeypatch.setattr(base, "run_command", lambda cmd, *, stdin=None, timeout=900: "  ")
    with pytest.raises(BackendError):
        opencode.review(job())
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_backends.py -q`
Expected: FAIL — `ImportError: cannot import name 'opencode'`.

- [ ] **Step 3: Implement `rocket_review/backends/opencode.py`**

```python
from rocket_review.backends import base
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.prompts import build_agent_prompt

NAME = "opencode"
BINARY = "opencode"
INSTALL_HINT = "brew install anomalyco/tap/opencode or npm i -g opencode-ai (https://opencode.ai)"
DEFAULT_MODEL = None  # honor the user's configured opencode default


def review(job: ReviewJob) -> str:
    prompt_file = base.write_prompt_file(build_agent_prompt(job))
    try:
        cmd = ["opencode", "run"]
        if job.model:
            cmd += ["--model", job.model]
        cmd.append(
            f"Read the file {prompt_file} for your full instructions, then follow them. "
            "You are performing a code review: do not modify any files."
        )
        output = base.run_command(cmd).strip()
        if not output:
            raise BackendError("opencode produced no output")
        return output
    finally:
        prompt_file.unlink(missing_ok=True)
```

- [ ] **Step 4: Register it** — `BACKENDS` tuple becomes `(codex, claude, opencode, api)`.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_backends.py -q`
Expected: all pass.

- [ ] **Step 6: Install opencode and probe for a read-only mode**

Run: `brew install anomalyco/tap/opencode || npm i -g opencode-ai`; then `opencode run --help`
Check the output for an agent/permission flag (opencode ships a read-only "plan" agent; the flag is expected to be `--agent plan` or similar). If present, add it to `cmd` in Step 3's implementation and extend `test_opencode_run_command_with_model` to assert it. If absent, keep the prompt-level instruction and note the residual risk in the README (Task 10 already words it).

- [ ] **Step 7: Live verification**

Run: `python -c "from rocket_review.backends import opencode; from rocket_review.backends.base import ReviewJob; print(opencode.review(ReviewJob(mode='code', content=open('rocket_review/models.py').read(), docs_content=None, extra=None, commit=None, pr=False, git_cmd=None, model='anthropic/claude-sonnet-5')))" | tail -20`
Expected: a real review prints (requires an opencode-configured provider credential; if none is configured, record that live validation is pending and continue — the backend is subprocess-mocked in CI).

- [ ] **Step 8: Commit**

```bash
git add rocket_review/backends/ tests/test_backends.py
git commit -m "feat: opencode backend for arbitrary-provider and local-model reviews"
```

---

### Task 9: Multi-backend fan-out (`--backend a,b` + `name:model`)

**Files:**
- Modify: `rocket_review/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `BACKENDS`, `missing_binary`, `parse_backend_output`, `should_fail`, `to_envelope`, `BackendError`.
- Produces: `parse_backend_arg(value: str, single_model: str | None) -> list[tuple[str, str | None]]` in `cli.py`; CLI accepts `--backend codex,claude`, `--backend claude:claude-opus-4-8`, `--api` alias.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
from rocket_review.cli import parse_backend_arg


def test_backend_arg_single_default():
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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_cli.py -q`
Expected: FAIL — `ImportError: cannot import name 'parse_backend_arg'`.

- [ ] **Step 3: Implement backend selection in `cli.py`**

```python
def parse_backend_arg(value: str, single_model: str | None) -> list[tuple[str, str | None]]:
    specs: list[tuple[str, str | None]] = []
    for item in value.split(","):
        name, _, model = item.strip().partition(":")
        if name not in BACKENDS:
            print(f"Error: unknown backend '{name}'. Available: {', '.join(BACKENDS)}.",
                  file=sys.stderr)
            sys.exit(1)
        if any(existing == name for existing, _ in specs):
            print(f"Error: backend '{name}' listed twice.", file=sys.stderr)
            sys.exit(1)
        specs.append((name, model or None))
    if single_model:
        if len(specs) > 1 or specs[0][1]:
            print("Error: with multiple backends use --backend name:model instead of --model.",
                  file=sys.stderr)
            sys.exit(1)
        specs[0] = (specs[0][0], single_model)
    return specs
```

argparse changes: add `parser.add_argument("--backend", default="codex", help="Comma-separated backends: codex, claude, opencode, api. Per-backend model via name:model")`; change `--model` default to `None`; `--api` becomes `action="store_true"` that rewrites `args.backend = "api"` right after parsing (keep the flag documented as an alias). Remove the old `HAS_CODEX`-based selection block.

Availability check before running:

```python
specs = parse_backend_arg(args.backend, args.model)
for name, _ in specs:
    hint = missing_binary(name)
    if hint:
        print(f"Error: backend '{name}' unavailable — {hint}", file=sys.stderr)
        sys.exit(1)
```

Content materialization rule replaces the old `use_codex` conditional: pass `content=None` (backend runs git itself) only when **every** selected backend is agentic (`name != "api"`); if `api` is selected anywhere, materialize the diff locally exactly as the old API path did.

- [ ] **Step 4: Implement the fan-out**

```python
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace


def run_one(name: str, model: str | None, job_template: ReviewJob) -> tuple[str, str | None, str | None, str | None]:
    job = replace(job_template, model=model)
    try:
        return name, model, BACKENDS[name].review(job), None
    except BackendError as e:
        return name, model, None, str(e)
```

In `main()`:

```python
with ThreadPoolExecutor(max_workers=len(specs)) as pool:
    futures = [pool.submit(run_one, name, model, job) for name, model in specs]
    outputs = [f.result() for f in futures]  # preserves --backend order

results = []
for name, model, raw, error in outputs:
    if error is not None:
        results.append(BackendResult(backend=name, model=model, error=error))
    elif args.json:
        results.append(parse_backend_output(raw, name, model))
    else:
        results.append(BackendResult(backend=name, model=model, raw=raw))

if args.json:
    print(json.dumps(to_envelope(results), indent=2))
else:
    for r in results:
        if len(results) > 1:
            print(f"\n## {r.backend}" + (f" ({r.model})" if r.model else ""), "\n")
        print(f"[backend error] {r.error}" if r.error else r.raw)

if all(r.error for r in results):
    sys.exit(1)
if any(r.error for r in results):
    print("Warning: some backends failed; findings above are partial.", file=sys.stderr)
if args.fail_on and should_fail(results, args.fail_on):
    sys.exit(2)
```

- [ ] **Step 5: Run the full suite**

Run: `pytest -q && ruff check .`
Expected: all pass.

- [ ] **Step 6: Live fan-out verification**

Run: `cd /Users/stepan/Projects/rocket-review && rr --diff --backend codex,claude --json 2>/dev/null | python -m json.tool | grep -E '"backend"|"severity"|"title"' | head -20` (with a small change staged/unstaged so `--diff` has content)
Expected: findings tagged with both `"backend": "codex"` and `"backend": "claude"`.

- [ ] **Step 7: Commit**

```bash
git add rocket_review/cli.py tests/test_cli.py
git commit -m "feat: multi-backend fan-out with --backend a,b and per-backend models"
```

---

### Task 10: README rewrite, LICENSE, help-text scrub

**Files:**
- Create: `LICENSE`
- Modify: `README.md`, `rocket_review/cli.py` (one help string)

**Interfaces:** none (docs + metadata).

- [ ] **Step 1: Scrub internal references**

In `cli.py`, change the `--repo` help string to: `"GitHub repo for --pr when not in the repo's checkout (e.g. acme/api-server)"`.

Then sweep the whole repo for LedgerRocket-internal names and scrub every hit in code, README, and packaging (the `ledger-rocket/rocket-review` install URL may stay until Task 11 decides the public home; this plan file under `docs/plans/` is exempt):

Run: `grep -rniE "ledger-?rocket|uniledger|event-service|event-loader|balance-|recon|accounting-rule|bigquery-adaptor|tigerbeetle|PRO-[0-9]+" --include="*.py" --include="*.md" --include="*.toml" --include="*.yml" . | grep -v "docs/plans/" | grep -v "rocket-review\|rocket_review"`
Expected after scrubbing: no hits (service names, org name, Linear ticket IDs all gone from published surfaces).

- [ ] **Step 2: Add MIT LICENSE**

`LICENSE` — standard MIT text, `Copyright (c) 2026 Stepan Sinkov`.

- [ ] **Step 3: Rewrite README.md**

Replace the full file with (positioning leads with what competitors lack: any-agent CLI, plan review, standards gate, multi-vendor; namechecks codex-plugin-cc honestly):

```markdown
# rocket-review

**`rr` — a second opinion on your code, from a model that didn't write it.**

One small CLI that sends your plan, diff, commit, or PR to an agentic reviewer
(Codex CLI, Claude Code, or opencode) that explores your project before judging —
then gives you prose or structured JSON you can gate CI on.

Unlike editor-bound plugins (e.g. OpenAI's codex-plugin-cc, which does GPT reviews
but only inside Claude Code), `rr` is a standalone CLI: call it from any shell, any
editor, any agent, or CI.

## Why

- **Cross-model review** — the model that wrote the code shouldn't be the only one
  grading it. Implement with Claude, review with GPT; implement with Codex, review
  with Claude; or fan out to both and compare.
- **Plans are reviewable too** — `rr plan.md` stress-tests a design doc *before*
  you build it. Most review tools only understand diffs.
- **Standards-aware** — `--docs` auto-discovers `llms.txt`, `AGENTS.md`, or
  `CLAUDE.md` (or takes explicit paths) and the reviewer flags deviations from
  *your* documented rules, not generic style opinions.
- **CI-gateable** — `--json --fail-on high` exits 2 when a high-severity finding
  lands. Pipe the envelope to `jq` or your bot of choice.

## Install

```bash
pipx install git+https://github.com/ledger-rocket/rocket-review.git
```

Requires Python 3.13+ and at least one backend:

- [Codex CLI](https://github.com/openai/codex) (default backend)
- [Claude Code](https://claude.com/claude-code) (`--backend claude`)
- [opencode](https://opencode.ai) (`--backend opencode` — any provider, including local models)
- or none of the above: `--backend api` calls the OpenAI API directly (`OPENAI_API_KEY`)

`--pr` also needs the [gh CLI](https://cli.github.com).

## Usage

```bash
rr plan.md                        # stress-test a plan before building
rr --diff                         # review uncommitted changes
rr --staged                       # review staged changes only
rr --commit abc1234               # review a commit
rr --pr 123                       # review a GitHub PR (number, URL, or branch)
rr --pr 123 --repo acme/api       # ...from outside that repo's checkout
git diff HEAD~3 | rr              # pipe anything
rr src/auth.py --docs             # review files against your documented standards
```

### Pick your reviewer

```bash
rr --diff --backend claude                     # Claude reviews (read-only sandbox)
rr --diff --backend opencode:ollama/qwen3      # fully local review
rr --diff --backend codex,claude               # both, side by side
rr --diff --backend codex:gpt-5.5,claude:claude-opus-4-8
```

Backends run agentically in read-only mode: they navigate your project — imports,
tests, related files — before judging. That context is what makes the review
worth reading.

### Structured output

```bash
rr --diff --json | jq '.findings[] | {severity, title, backend}'
rr --staged --json --fail-on high && git commit   # block the commit on high+ findings
```

Every finding carries `severity, title, file, line, why, fix, backend, model`.
Parse failures and backend errors fail the gate closed.

## Review modes

- `plan` — auto-detected for `.md`/`.txt`/`.plan` files: completeness, ordering, risks, over-engineering.
- `code` — source files: correctness, security, performance, maintainability.
- `diff` — for `--diff`/`--staged`/`--commit`/`--pr`/stdin: bugs introduced, missing changes, contract breaks.

Override with `--mode`, add focus with `--prompt "check the locking"`.

## Project standards (`--docs`)

Point the reviewer at your project's standards docs — it flags deviations from
*your* documented rules:

```bash
rr src/auth.py --docs                     # auto-discovers llms.txt / AGENTS.md / CLAUDE.md
rr src/auth.py --docs docs/standards.md docs/smells.md
```

Relative markdown links inside the docs are followed one level, so an index file
(like `llms.txt`) pulls in everything it references. `--llms [PATH]` is kept as a
compatibility alias for `--docs llms.txt`.

## Agent integration

Drop into your `CLAUDE.md` / `AGENTS.md`:

```markdown
Before pushing non-trivial changes, run `rr --diff --docs` and address the findings.
For plans, run `rr plan.md --docs` before implementing. Use a 900000ms timeout.
```

## Notes

- Codex runs with `-s read-only`; Claude Code runs with a read-only tool allowlist;
  opencode is instructed not to modify files (use a read-only agent profile there
  if you have one configured).
- `--fail-on` requires `--json`.
- Exit codes: 0 clean · 1 operational error · 2 findings at/above `--fail-on`.

MIT licensed.
```

- [ ] **Step 4: Verify docs match the code**

Run: `rr --help` and diff mentally against the README flag list; run `pytest -q`.
Expected: every flag in the README exists in `--help`; suite green.

- [ ] **Step 5: Commit**

```bash
git add README.md LICENSE rocket_review/cli.py
git commit -m "docs: publish-ready README, MIT license, scrub internal example"
```

---

### Task 11: Publish-prep checklist (decisions + external moves)

These are org/account actions, not code. Do them with Stepan, in order:

- [ ] **Step 1: Second-opinion review of the whole branch** — `rr --diff --docs` (dogfood), fix findings, then PR `publish-polish` → `main` and let the normal pipeline review it.
- [ ] **Step 2: Decide the public home** — options: (a) make `ledger-rocket/rocket-review` public, (b) transfer/fork to a personal account. Either way the repo must stop depending on org internals *at publish time*:
  - remove `.github/workflows/minimal-ci.yml` (it calls private `.github-private` reusables and will fail on a public repo) and delete the repo's entry from `.github-private/sync-config.yaml` so the sync engine doesn't re-add it;
  - `ci.yml` from Task 2 is already self-contained and stays.
- [ ] **Step 3: PyPI (optional but cheap credibility)** — check name availability: `pip index versions rocket-review`; if free, `pipx run build && pipx run twine upload dist/*`. If taken, README keeps the `pipx install git+...` path.
- [ ] **Step 4: Update the claude-plugins wrapper** — `claude-plugins/rocket-review/install.sh` and SKILL.md still work unchanged (they call `rr`), but the skill text should learn `--backend`, `--docs`, and `--json`; bump plugin.json when marketplace-listing it (PRO-1855).
- [ ] **Step 5: LinkedIn post angle** (from the 2026-07-06 landscape research): lead with plan-review + `--docs` standards gate + one-CLI-anywhere + local-model reviews via opencode; explicitly namecheck OpenAI's codex-plugin-cc (26k★) and position `rr` as the editor-agnostic superset, not the inventor of cross-model review. Verify any numbers quoted in the post on the day of posting.

---

## Self-Review Notes

- Spec coverage: publish blockers (LICENSE ✓ T10, tests ✓ T2–T9, CI ✓ T2/T11, README drift ✓ T10, scrub ✓ T10, pending changes ✓ T1), docs-flag unification ✓ T4, opencode backend ✓ T8, Claude backend ✓ T7, structured output ✓ T5–T6, fan-out ✓ T9, publish/positioning ✓ T11.
- Type consistency: `ReviewJob` fields fixed in T3 and consumed unchanged in T6–T9; `parse_backend_output(text, backend, model)` signature identical in T5 (definition), T6 and T9 (call sites); `collect_docs(docs_args, llms_arg)` defined in T4, wired into `main()` there, unchanged after; `BACKENDS` registry name used everywhere.
- Known deliberate deferrals (YAGNI): no consensus/dedup pass across backends (tagged merge only), no `--timeout` flag, no strict-schema mode for `api`/`claude` backends (tolerant parse + fail-closed gate covers it), no retry-on-parse-failure (a retry costs a full agentic run; the envelope reports `parse_error` instead).
- Deliberate behavior changes called out in T4: explicit `--docs` paths error when missing (was warn-and-skip); explicit `--docs` files gain one-level link-following.
- External unknowns flagged in-plan: opencode read-only agent flag (T8 S6 probes `--help`), opencode provider credentials for live validation (T8 S7).
