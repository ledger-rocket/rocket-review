import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rocket_review.prompts import build_codex_prompt


DEFAULT_MODEL = "gpt-5.4"

HAS_CODEX = shutil.which("codex") is not None


# ---------------------------------------------------------------------------
# Codex backend
# ---------------------------------------------------------------------------

def review_with_codex(
    mode: str,
    content: str | None,
    docs_content: str | None = None,
    model: str = DEFAULT_MODEL,
    extra: str | None = None,
    commit: str | None = None,
    pr: bool = False,
    git_cmd: str | None = None,
) -> str:
    prompt = build_codex_prompt(
        mode, content, docs_content, extra,
        commit=commit, pr=pr, git_cmd=git_cmd,
    )

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        outfile = f.name

    # Write prompt to temp file to avoid ARG_MAX limits on large reviews
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, prefix="rr-prompt-",
    ) as pf:
        pf.write(prompt)
        prompt_file = pf.name

    try:
        cmd = ["codex", "exec", "-s", "read-only", "-o", outfile]
        if model:
            cmd += ["-m", model]
        cmd.append(f"Read the file {prompt_file} for your full instructions, then follow them.")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            print("Error: codex timed out after 15 minutes.", file=sys.stderr)
            sys.exit(1)

        if result.returncode != 0:
            stderr = result.stderr.strip()
            print(f"Error: codex failed (exit {result.returncode}): {stderr}", file=sys.stderr)
            sys.exit(1)

        output = Path(outfile).read_text().strip()
        if not output:
            print("Error: codex produced no output.", file=sys.stderr)
            sys.exit(1)
        return output
    finally:
        Path(outfile).unlink(missing_ok=True)
        Path(prompt_file).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# API backend
# ---------------------------------------------------------------------------

def _load_env_file() -> None:
    """Load OPENAI_API_KEY from .env files if not already in environment."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    for candidate in [Path.cwd() / ".env", Path.home() / ".env"]:
        if candidate.is_file():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("OPENAI_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        os.environ["OPENAI_API_KEY"] = val
                        return


def _get_repo_root() -> Path | None:
    """Get the git repo root, or None if not in a repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    return None


def extract_referenced_files(text: str, max_size: int = 100_000) -> str:
    """Extract file paths from text and read their contents (repo-scoped only)."""
    repo_root = _get_repo_root()
    if not repo_root:
        repo_root = Path.cwd().resolve()

    backtick = re.findall(r"`([^`\s]+\.\w{1,10})`", text)
    bare = re.findall(r"(?<![`\w])([\w][\w./-]*\.\w{1,10})(?![\w`])", text)
    candidates = set(backtick + bare)

    parts = []
    seen = set()
    for c in sorted(candidates):
        p = Path(c).resolve()
        # Only include files within the repo/cwd
        if not str(p).startswith(str(repo_root)):
            continue
        if p.is_file() and p not in seen:
            try:
                if p.stat().st_size <= max_size:
                    parts.append(f"=== {c} ===\n{p.read_text()}")
                    seen.add(p)
            except (OSError, UnicodeDecodeError):
                continue
    return "\n\n".join(parts)


def _resolve_model(client, model: str) -> str:
    """Resolve short model alias to available dated ID."""
    if "-202" in model:
        return model
    try:
        available = {m.id for m in client.models.list()}
    except Exception:
        return model
    if model in available:
        return model
    candidates = sorted(m for m in available if m.startswith(model + "-202"))
    return candidates[-1] if candidates else model


def review_with_api(
    content: str,
    system_prompt: str,
    model: str = DEFAULT_MODEL,
    extra: str | None = None,
) -> str:
    _load_env_file()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Error: OPENAI_API_KEY not set. Export it or put it in .env", file=sys.stderr)
        sys.exit(1)

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    model = _resolve_model(client, model)

    # Extract referenced files and append as context
    referenced = extract_referenced_files(content)
    if referenced:
        content = f"{content}\n\n=== REFERENCED PROJECT FILES ===\n{referenced}\n=== END REFERENCED FILES ==="

    user_message = content
    if extra:
        user_message = f"Additional review instructions: {extra}\n\n---\n\n{user_message}"

    try:
        response = client.responses.create(
            model=model,
            instructions=system_prompt,
            input=user_message,
        )
    except Exception as exc:
        print(f"Error: OpenAI API call failed: {exc}", file=sys.stderr)
        sys.exit(1)

    return response.output_text
