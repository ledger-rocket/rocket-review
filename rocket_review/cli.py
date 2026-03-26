import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from rocket_review.prompts import get_prompt
from rocket_review.review import DEFAULT_MODEL, HAS_CODEX, review_with_api, review_with_codex


def read_files(paths: list[str]) -> str:
    parts = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            print(f"Error: file not found: {p}", file=sys.stderr)
            sys.exit(1)
        text = path.read_text()
        parts.append(f"=== {path} ===\n{text}")
    return "\n\n".join(parts)


def read_docs(paths: list[str]) -> str:
    parts = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            print(f"Warning: docs file not found, skipping: {p}", file=sys.stderr)
            continue
        text = path.read_text()
        parts.append(f"--- {path.name} ---\n{text}")
    return "\n\n".join(parts) if parts else ""


def read_llms(llms_path: Path) -> str:
    """Read llms.txt and follow all relative markdown links to build full project context."""
    if not llms_path.exists():
        print(f"Error: {llms_path} not found.", file=sys.stderr)
        sys.exit(1)

    base_dir = llms_path.parent
    llms_text = llms_path.read_text()
    parts = [f"--- llms.txt ---\n{llms_text}"]

    links = re.findall(r"\[[^\]]*\]\(([^)]+)\)", llms_text)
    for link in links:
        if link.startswith(("http://", "https://", "#")):
            continue
        doc_path = base_dir / link
        if doc_path.is_file():
            try:
                text = doc_path.read_text()
                parts.append(f"--- {link} ---\n{text}")
            except Exception as e:
                print(f"Warning: could not read {link}: {e}", file=sys.stderr)

    return "\n\n".join(parts)


def get_diff(staged: bool) -> str:
    cmd = ["git", "diff"]
    if staged:
        cmd.append("--staged")
    else:
        cmd.append("HEAD")
    result = subprocess.run(cmd, capture_output=True, text=True)
    diff = result.stdout.strip()
    if not diff:
        print("Error: no diff output. Working tree is clean.", file=sys.stderr)
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


def get_pr_content(pr_ref: str) -> tuple[str, str]:
    """Fetch PR metadata and diff using gh CLI. Returns (description, diff)."""
    if not shutil.which("gh"):
        print("Error: gh CLI not found. Install it from https://cli.github.com", file=sys.stderr)
        sys.exit(1)

    view_result = subprocess.run(
        ["gh", "pr", "view", pr_ref, "--json", "title,body,number,url"],
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
        ["gh", "pr", "diff", pr_ref],
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
        "--model", default=DEFAULT_MODEL, help=f"Model to use (default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--mode",
        choices=["plan", "code", "diff"],
        help="Review mode (auto-detected if omitted)",
    )
    parser.add_argument("--prompt", help="Additional review instructions")
    parser.add_argument("--docs", nargs="+", help="Project standards docs to include as context")
    parser.add_argument(
        "--llms",
        nargs="?",
        const="llms.txt",
        metavar="PATH",
        help="Read llms.txt and follow its doc links for project context (default: ./llms.txt)",
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="Use OpenAI API directly instead of Codex CLI (auto-extracts referenced files)",
    )

    args = parser.parse_args()

    if args.api:
        use_codex = False
    elif HAS_CODEX:
        use_codex = True
    else:
        print(
            "Error: codex CLI not found. Install it from https://github.com/openai/codex\n"
            "Or use --api for direct OpenAI API mode (no project navigation, higher token usage).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Gather content to review
    content: str | None = None
    pr_description: str | None = None
    if args.pr:
        pr_description, diff = get_pr_content(args.pr)
        content = f"=== PULL REQUEST ===\n{pr_description}\n=== END PULL REQUEST ===\n\n{diff}"
        mode = "diff"
    elif args.commit:
        if use_codex:
            content = None  # let codex run git show itself
        else:
            content = get_commit_diff(args.commit)
        mode = "diff"
    elif args.diff or args.staged:
        if use_codex:
            content = None  # let codex run git diff itself
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
    docs_content = None
    if args.llms:
        docs_content = read_llms(Path(args.llms))
    if args.docs:
        explicit = read_docs(args.docs)
        docs_content = f"{docs_content}\n\n{explicit}" if docs_content else explicit

    # Run review
    if use_codex:
        result = review_with_codex(
            mode, content, docs_content, args.model, args.prompt,
            commit=args.commit, pr=bool(args.pr),
        )
    else:
        # API mode: assemble full content with docs
        if docs_content:
            content = f"=== PROJECT STANDARDS ===\n{docs_content}\n=== END PROJECT STANDARDS ===\n\n{content}"

        system_prompt = get_prompt(mode, docs_content)
        result = review_with_api(content, system_prompt, args.model, args.prompt)

    print(result)
