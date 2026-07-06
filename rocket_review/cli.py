import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urldefrag

from rocket_review.backends import BACKENDS, missing_binary
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.models import (
    BackendResult,
    parse_backend_output,
    should_fail,
    to_envelope,
)


def read_files(paths: list[str]) -> str:
    parts = []
    for p in paths:
        path = Path(p)
        if not path.is_file():
            print(f"Error: not a file: {p}", file=sys.stderr)
            sys.exit(1)
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError) as e:
            print(f"Error: could not read {p}: {e}", file=sys.stderr)
            sys.exit(1)
        parts.append(f"=== {path} ===\n{text}")
    return "\n\n".join(parts)


def read_doc_with_links(doc_path: Path) -> str:
    """Read a doc and follow all relative markdown links to build full project context."""
    if not doc_path.is_file():
        print(f"Error: {doc_path} not found.", file=sys.stderr)
        sys.exit(1)

    base_dir = doc_path.parent.resolve()
    doc_text = doc_path.read_text()
    parts = [f"--- {doc_path.name} ---\n{doc_text}"]

    links = re.findall(r"\[[^\]]*\]\(([^)]+)\)", doc_text)
    for raw_link in links:
        if raw_link.startswith(("http://", "https://", "#")):
            continue
        link, _ = urldefrag(unquote(raw_link))
        if not link:
            continue
        linked_path = (base_dir / link).resolve()
        # Prevent path traversal outside the doc's directory
        if not str(linked_path).startswith(str(base_dir)):
            print(f"Warning: skipping link outside project: {raw_link}", file=sys.stderr)
            continue
        if linked_path.is_file():
            try:
                text = linked_path.read_text()
                parts.append(f"--- {link} ---\n{text}")
            except (OSError, UnicodeDecodeError) as e:
                print(f"Warning: could not read {link}: {e}", file=sys.stderr)

    return "\n\n".join(parts)


DISCOVERY_CANDIDATES = ["llms.txt", "AGENTS.md", "CLAUDE.md"]


def collect_docs(docs_args: list[str] | None, llms_arg: str | None) -> str | None:
    """Assemble standards context from --docs (explicit or auto-discovered) and --llms."""
    paths: list[Path] = []
    if llms_arg:
        paths.append(Path(llms_arg))
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


def get_diff(staged: bool) -> str:
    cmd = ["git", "diff"]
    if staged:
        cmd.append("--staged")
    else:
        cmd.append("HEAD")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: {' '.join(cmd)} failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    diff = result.stdout.strip()
    if not diff:
        label = "staged changes" if staged else "uncommitted changes"
        print(f"Error: no {label} found.", file=sys.stderr)
        sys.exit(1)
    return diff


def get_commit_diff(sha: str) -> str:
    result = subprocess.run(["git", "show", sha], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: git show {sha} failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    diff = result.stdout.strip()
    if not diff:
        print(f"Error: no output for commit {sha}.", file=sys.stderr)
        sys.exit(1)
    return diff


def get_pr_content(pr_ref: str, repo: str | None = None) -> tuple[str, str]:
    """Fetch PR metadata and diff using gh CLI. Returns (description, diff)."""
    if not shutil.which("gh"):
        print("Error: gh CLI not found. Install it from https://cli.github.com", file=sys.stderr)
        sys.exit(1)

    repo_args = ["--repo", repo] if repo else []

    view_result = subprocess.run(
        ["gh", "pr", "view", pr_ref, "--json", "title,body,number,url"] + repo_args,
        capture_output=True, text=True,
    )
    if view_result.returncode != 0:
        print(f"Error: gh pr view failed: {view_result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    pr_info = json.loads(view_result.stdout)
    description = f"PR #{pr_info['number']}: {pr_info['title']}\n{pr_info.get('url', '')}"
    if pr_info.get("body"):
        description += f"\n\n{pr_info['body']}"

    diff_result = subprocess.run(
        ["gh", "pr", "diff", pr_ref] + repo_args,
        capture_output=True, text=True,
    )
    if diff_result.returncode != 0:
        print(f"Error: gh pr diff failed: {diff_result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    diff = diff_result.stdout.strip()
    if not diff:
        print("Error: PR has no diff (empty changes).", file=sys.stderr)
        sys.exit(1)

    return description, diff


def detect_mode(paths: list[str]) -> str:
    plan_indicators = {".md", ".txt", ".plan"}
    exts = {Path(p).suffix.lower() for p in paths}
    if exts <= plan_indicators:
        return "plan"
    return "code"


def stdin_has_input() -> bool:
    """True only for a real pipe or redirected file, not just any non-tty fd.

    A bare non-interactive fd (e.g. stdin redirected from /dev/null, common under
    CI/test harnesses) is non-tty too but carries no content, so it must not count
    as a review source or it collides with an explicit --diff/--pr/etc.
    """
    if sys.stdin.isatty():
        return False
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError):
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISREG(mode)


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


def run_one(
    name: str, model: str | None, job_template: ReviewJob
) -> tuple[str, str | None, str | None, str | None]:
    job = replace(job_template, model=model)
    try:
        return name, model, BACKENDS[name].review(job), None
    except BackendError as e:
        return name, model, None, str(e)


def main():
    parser = argparse.ArgumentParser(
        prog="rr",
        description="rocket-review: get GPT review of plans, code, or diffs",
    )
    parser.add_argument("files", nargs="*", help="Files to review")
    parser.add_argument("--diff", action="store_true", help="Review git diff (HEAD)")
    parser.add_argument("--staged", action="store_true", help="Review staged changes only")
    parser.add_argument("--commit", metavar="SHA", help="Review a specific commit")
    parser.add_argument(
        "--pr", metavar="REF", help="Review a GitHub PR (number, URL, or branch)"
    )
    parser.add_argument(
        "--repo", metavar="OWNER/REPO",
        help="GitHub repo for --pr when not in the repo's checkout (e.g. ledger-rocket/event-service)",
    )
    parser.add_argument(
        "--backend", default="codex",
        help="Comma-separated backends: codex, claude, opencode, api. "
             "Per-backend model via name:model (e.g. codex:gpt-5.5,claude).",
    )
    parser.add_argument(
        "--model", default=None,
        help="Model for the single selected backend (codex/api default to gpt-5.5; "
             "claude/opencode use the tool's own default). "
             "With multiple backends use --backend name:model instead.",
    )
    parser.add_argument(
        "--mode",
        choices=["plan", "code", "diff"],
        help="Review mode (auto-detected if omitted)",
    )
    parser.add_argument("--prompt", help="Additional review instructions")
    parser.add_argument(
        "--docs", nargs="*", metavar="PATH",
        help="Project standards docs to review against; relative markdown links inside them are "
             "followed one level. With no PATH, auto-discovers llms.txt / AGENTS.md / CLAUDE.md.",
    )
    parser.add_argument(
        "--llms", nargs="?", const="llms.txt", metavar="PATH",
        help="Alias for --docs llms.txt (kept for compatibility)",
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="Alias for --backend api: OpenAI API directly, no project navigation "
             "(auto-extracts referenced files)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit findings as a JSON envelope instead of prose",
    )
    parser.add_argument(
        "--fail-on", choices=["critical", "high", "medium", "low"],
        help="Exit 2 if any finding is at or above this severity (requires --json)",
    )

    args = parser.parse_args()

    if args.fail_on and not args.json:
        print(
            "Error: --fail-on requires --json (findings must be parsed to be gated).",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.api:
        if args.backend != "codex":
            print(
                "Error: --api conflicts with --backend; --api is shorthand for --backend api.",
                file=sys.stderr,
            )
            sys.exit(1)
        args.backend = "api"

    specs = parse_backend_arg(args.backend, args.model)
    for name, _ in specs:
        hint = missing_binary(name)
        if hint:
            print(f"Error: backend '{name}' unavailable — {hint}", file=sys.stderr)
            sys.exit(1)
    all_agentic = all(name != "api" for name, _ in specs)

    # Validate mutually exclusive sources
    explicit_sources = sum([
        bool(args.pr),
        bool(args.commit),
        args.diff or args.staged,
        bool(args.files),
    ])
    sources = explicit_sources + int(stdin_has_input())
    if sources > 1:
        print("Error: specify only one review source (files, --diff, --staged, --commit, --pr, or stdin).", file=sys.stderr)
        sys.exit(1)

    # Gather content to review
    content: str | None = None
    git_cmd: str | None = None
    if args.pr:
        pr_description, diff = get_pr_content(args.pr, repo=args.repo)
        content = f"=== PULL REQUEST ===\n{pr_description}\n=== END PULL REQUEST ===\n\n{diff}"
        mode = "diff"
    elif args.commit:
        if all_agentic:
            content = None  # let the agentic backend run git show itself
        else:
            content = get_commit_diff(args.commit)
        mode = "diff"
    elif args.diff or args.staged:
        if all_agentic:
            content = None
            git_cmd = "git diff --staged" if args.staged else "git diff HEAD"
        else:
            content = get_diff(args.staged)
        mode = "diff"
    elif args.files:
        content = read_files(args.files)
        mode = detect_mode(args.files)
    elif not sys.stdin.isatty():
        content = sys.stdin.read().strip()
        if not content:
            print("Error: empty input from stdin.", file=sys.stderr)
            sys.exit(1)
        mode = "diff"
    else:
        parser.print_help()
        sys.exit(1)

    if args.mode:
        mode = args.mode

    # Read project standards docs
    docs_content = collect_docs(args.docs, args.llms)

    # Per-backend model is injected by run_one; the template leaves it unset.
    job = ReviewJob(
        mode=mode,
        content=content,
        docs_content=docs_content,
        extra=args.prompt,
        commit=args.commit,
        pr=bool(args.pr),
        git_cmd=git_cmd,
        model=None,
        json_output=args.json,
    )

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
            # A failed backend's block (header + error) goes to stderr so piping stdout
            # to a file captures only real reviews; successful prose stays on stdout.
            stream = sys.stderr if r.error else sys.stdout
            if len(results) > 1:
                print(f"\n## {r.backend}" + (f" ({r.model})" if r.model else ""), "\n",
                      file=stream)
            if r.error:
                print(f"[backend error] {r.error}", file=sys.stderr)
            else:
                print(r.raw)

    if all(r.error for r in results):
        sys.exit(1)
    if any(r.error for r in results):
        print("Warning: some backends failed; findings above are partial.", file=sys.stderr)
    if args.fail_on and should_fail(results, args.fail_on):
        sys.exit(2)
