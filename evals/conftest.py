"""Fixtures for the eval-harness tests: a throwaway git repo and a stub backend binary.

The stub is a real executable named `codex` placed first on PATH, so `rr` launches it
through its own backend module and its own `subprocess` plumbing. That is what makes the
injection tests meaningful: nothing about the prompt path is faked, only the model at the
far end of it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

GOOD_REVIEW = {"verdict": "approve", "summary": "stub review", "findings": []}

# Reads the prompt rr wrote for it, records the exact bytes, and answers with whatever
# review the test asked for. STUB_FAIL_UNTIL makes the first N invocations fail, which is
# how the retry path is exercised without a real flaky backend.
STUB_CODEX = '''\
import os
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("stub-codex 0.0.1")
    raise SystemExit(0)

capture_dir = Path(os.environ["STUB_CAPTURE_DIR"])
instruction = args[-1]
prompt_file = instruction.split("Read the file ", 1)[1].split(" for your full", 1)[0]
(capture_dir / f"prompt-{uuid.uuid4().hex}.txt").write_text(
    Path(prompt_file).read_text(encoding="utf-8"), encoding="utf-8"
)

fail_until = int(os.environ.get("STUB_FAIL_UNTIL", "0"))
if fail_until:
    counter = capture_dir / "invocations"
    seen = len(counter.read_text().splitlines()) if counter.exists() else 0
    with counter.open("a") as fh:
        fh.write("x\\n")
    if seen < fail_until:
        sys.stderr.write("stub codex: simulated failure\\n")
        sys.exit(1)

Path(args[args.index("-o") + 1]).write_text(
    os.environ.get("STUB_REVIEW", DEFAULT_REVIEW), encoding="utf-8"
)
'''


@dataclass
class StubBackend:
    bin_dir: Path
    capture_dir: Path

    def captured_prompts(self) -> list[str]:
        return [p.read_text(encoding="utf-8") for p in sorted(self.capture_dir.glob("*.txt"))]

    def env(self, **extra: str) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["STUB_CAPTURE_DIR"] = str(self.capture_dir)
        env.update(extra)
        return env


@pytest.fixture
def stub_backend(tmp_path: Path) -> StubBackend:
    bin_dir = tmp_path / "stub-bin"
    capture_dir = tmp_path / "captures"
    bin_dir.mkdir()
    capture_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        f"DEFAULT_REVIEW = {json.dumps(GOOD_REVIEW)!r}\n"
        + STUB_CODEX,
        encoding="utf-8",
    )
    script.chmod(0o755)
    return StubBackend(bin_dir=bin_dir, capture_dir=capture_dir)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=evals@example.invalid",
         "-c", "user.name=evals", *args],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A two-commit repository standing in for the project under review."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "sample.py").write_text(
        "def rank(label):\n"
        "    known = ['critical', 'high']\n"
        "    return known.index(label) if label in known else 0\n",
        encoding="utf-8",
    )
    git(repo, "add", "sample.py")
    git(repo, "commit", "-qm", "add sample")
    (repo / "sample.py").write_text(
        "def rank(label):\n"
        "    known = ['critical', 'high']\n"
        "    if not label:\n"
        "        return 0\n"
        "    return known.index(label) if label in known else 0\n",
        encoding="utf-8",
    )
    git(repo, "add", "sample.py")
    git(repo, "commit", "-qm", "guard against an empty label")
    return repo


@pytest.fixture
def head_oid(git_repo: Path) -> str:
    return git(git_repo, "rev-parse", "HEAD").stdout.strip()
