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

from rocket_review.backends import BACKENDS, base, missing_binary
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.models import (
    BackendResult,
    parse_backend_output,
    should_fail,
    to_envelope,
)

RAW_TRUNCATE_LIMIT = 4000

# Bounds git/gh preflight calls so a hung credential helper or network fetch
# can't stall a CI gate indefinitely; backend runs have their own longer timeout.
SUBPROCESS_TIMEOUT = 300


def run_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a git/gh command with a timeout and total decoding.

    Diffs legitimately contain non-UTF8 bytes (files in other encodings), so
    decode with errors="replace" rather than letting a stray byte raise.
    """
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"Error: {' '.join(cmd)} timed out after {SUBPROCESS_TIMEOUT}s", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: {cmd[0]} not found on PATH", file=sys.stderr)
        sys.exit(1)


def truncate_raw(results: list[BackendResult]) -> None:
    """Truncate oversized raw output inline; never spill it to disk.

    A truncated review can quote proprietary code or secrets, so it must not be written
    to a world-readable temp file that nothing ever cleans up. The envelope stays bounded
    by dropping the tail (agent-ergonomic axi P3: the marker names the full length and
    points at --full, which inlines the complete text on demand).
    """
    for r in results:
        if len(r.raw) <= RAW_TRUNCATE_LIMIT:
            continue
        total = len(r.raw)
        r.raw = (
            r.raw[:RAW_TRUNCATE_LIMIT]
            + f"\n(truncated, {total} chars total; use --full to inline)"
        )
        r.raw_file = None


def read_files(paths: list[str]) -> str:
    parts = []
    for p in paths:
        path = Path(p)
        if not path.is_file():
            print(f"Error: not a file: {p}", file=sys.stderr)
            sys.exit(1)
        try:
            text = path.read_text(encoding="utf-8")
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
    try:
        doc_text = doc_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"Error: could not read {doc_path}: {e}", file=sys.stderr)
        sys.exit(1)
    parts = [f"--- {doc_path.name} ---\n{doc_text}"]

    links = re.findall(r"\[[^\]]*\]\(([^)]+)\)", doc_text)
    for raw_link in links:
        if raw_link.startswith(("http://", "https://", "#")):
            continue
        link, _ = urldefrag(unquote(raw_link))
        if not link:
            continue
        linked_path = (base_dir / link).resolve()
        # Prevent path traversal outside the doc's directory. is_relative_to, not a
        # string-prefix check: /a/b-sibling must not pass as inside /a/b.
        if not linked_path.is_relative_to(base_dir):
            print(f"Warning: skipping link outside project: {raw_link}", file=sys.stderr)
            continue
        if linked_path.is_file():
            try:
                text = linked_path.read_text(encoding="utf-8")
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
    result = run_capture(cmd)
    if result.returncode != 0:
        print(f"Error: {' '.join(cmd)} failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    diff = result.stdout.strip()
    if not diff:
        label = "staged changes" if staged else "uncommitted changes"
        print(f"Error: no {label} found.", file=sys.stderr)
        sys.exit(1)
    return diff


def ensure_diff_exists(staged: bool) -> None:
    """Preflight for agentic backends: fail fast on an empty diff instead of
    launching a multi-minute backend run that reviews nothing."""
    cmd = ["git", "diff", "--quiet"]
    cmd.append("--staged" if staged else "HEAD")
    result = run_capture(cmd)
    if result.returncode == 0:
        label = "staged changes" if staged else "uncommitted changes"
        print(f"Error: no {label} found.", file=sys.stderr)
        sys.exit(1)
    if result.returncode != 1:
        print(f"Error: {' '.join(cmd)} failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


def resolve_commit(rev: str) -> str:
    """Resolve a commit revision to its full object ID, failing closed on unknown input.

    Rejecting a leading dash and pinning with --end-of-options stops a value like
    --no-patch from being read as a git option. The canonical OID — not the raw
    argument — is what flows into `git show` and the agent's `git show <oid>`
    instruction, so a crafted --commit can neither inject options nor a shell command.
    """
    if rev.startswith("-"):
        print(f"Error: invalid commit revision {rev!r}.", file=sys.stderr)
        sys.exit(1)
    result = run_capture(
        ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"]
    )
    oid = result.stdout.strip()
    if result.returncode != 0 or not oid:
        print(f"Error: unknown commit {rev}.", file=sys.stderr)
        sys.exit(1)
    return oid


def get_commit_diff(oid: str) -> str:
    result = run_capture(["git", "show", oid])
    if result.returncode != 0:
        print(f"Error: git show {oid} failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    diff = result.stdout.strip()
    if not diff:
        print(f"Error: no output for commit {oid}.", file=sys.stderr)
        sys.exit(1)
    return diff


def get_pr_content(pr_ref: str, repo: str | None = None) -> tuple[str, str]:
    """Fetch PR metadata and diff using gh CLI. Returns (description, diff)."""
    if pr_ref.startswith("-"):
        print(f"Error: invalid PR reference {pr_ref!r}.", file=sys.stderr)
        sys.exit(1)
    if not shutil.which("gh"):
        print("Error: gh CLI not found. Install it from https://cli.github.com", file=sys.stderr)
        sys.exit(1)

    repo_args = ["--repo", repo] if repo else []

    view_result = run_capture(
        ["gh", "pr", "view", pr_ref, "--json", "title,body,number,url"] + repo_args,
    )
    if view_result.returncode != 0:
        print(f"Error: gh pr view failed: {view_result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    try:
        pr_info = json.loads(view_result.stdout)
    except json.JSONDecodeError:
        print("Error: could not parse gh pr view output as JSON.", file=sys.stderr)
        sys.exit(1)
    description = f"PR #{pr_info['number']}: {pr_info['title']}\n{pr_info.get('url', '')}"
    if pr_info.get("body"):
        description += f"\n\n{pr_info['body']}"

    diff_result = run_capture(["gh", "pr", "diff", pr_ref] + repo_args)
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


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return parsed


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
        raw = BACKENDS[name].review(job)
        # Central fail-closed check: the CLI backends already reject empty output, but
        # the API backend can return a blank string. An empty error would also read as
        # success under the downstream truthiness checks, so never emit one.
        if not isinstance(raw, str) or not raw.strip():
            raise BackendError(f"{name} produced no review output")
        return name, model, raw, None
    except BackendError as e:
        return name, model, None, str(e) or f"{name} backend failed"
    except Exception as e:
        return name, model, None, f"{type(e).__name__}: {e}"


def main():
    try:
        _run()
    except BrokenPipeError:
        # A downstream reader (e.g. `rr ... | head`) closed the pipe. Redirect stdout
        # to devnull so the interpreter's final flush can't raise a second BrokenPipeError,
        # and exit quietly instead of dumping a traceback into a normal Unix pipeline.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)


def _run():
    parser = argparse.ArgumentParser(
        prog="rr",
        description="rocket-review: get GPT review of plans, code, or diffs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  rr plan.md                             # review a plan or design doc\n"
            "  rr --diff                              # review uncommitted changes (git diff HEAD)\n"
            "  rr --staged --json --fail-on high      # gate a commit on high+ findings (exit 2)\n"
            "  rr --diff --backend codex,claude       # cross-model review, one pass per backend\n"
            "  rr src/auth.py --docs                  # review a file against project standards\n"
            "  git diff | rr                          # review a diff piped on stdin\n"
        ),
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
        help="GitHub repo for --pr when not in the repo's checkout (e.g. acme/api-server)",
    )
    parser.add_argument(
        "--backend", default="codex",
        help="Comma-separated backends: codex, claude, opencode, api. "
             "Per-backend model via name:model (e.g. codex:gpt-5.6-sol,claude).",
    )
    parser.add_argument(
        "--model", default=None,
        help="Model for the single selected backend (codex uses your codex default "
             "from ~/.codex/config.toml, e.g. gpt-5.6-sol on ChatGPT plans; api defaults "
             "to gpt-5.6; claude/opencode use the tool's own default). "
             "With multiple backends use --backend name:model instead.",
    )
    parser.add_argument(
        "--mode",
        choices=["plan", "code", "diff"],
        help="Review mode (auto-detected if omitted)",
    )
    parser.add_argument("--prompt", help="Additional review instructions")
    parser.add_argument(
        "--effort", default=None, metavar="LEVEL",
        help="Reasoning effort, passed through to the backend (values differ per backend: "
             "codex/api e.g. minimal|low|medium|high; claude low|medium|high|xhigh|max). "
             "Not supported by opencode; invalid values fail loudly downstream.",
    )
    parser.add_argument(
        "--timeout", type=positive_int, default=None, metavar="SECONDS",
        help="Per-backend subprocess timeout in seconds (default: 900 = 15 min). "
             "Raise for slow high-effort reviews, e.g. --timeout 1800.",
    )
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
    parser.add_argument(
        "--full", action="store_true",
        help="Inline full backend output in the --json envelope (skip truncation)",
    )

    args = parser.parse_args()

    if args.fail_on and not args.json:
        print(
            "Error: --fail-on requires --json (findings must be parsed to be gated).",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.full and not args.json:
        print("Error: --full requires --json (text mode never truncates).", file=sys.stderr)
        sys.exit(1)

    if args.repo and not args.pr:
        print("Error: --repo only applies with --pr.", file=sys.stderr)
        sys.exit(1)

    if args.diff and args.staged:
        # Not silently staged-only: the user likely expects both sets reviewed.
        print("Error: specify only one of --diff or --staged.", file=sys.stderr)
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
    if args.effort and any(name == "opencode" for name, _ in specs):
        # opencode has no reasoning-effort flag; drop nothing silently.
        print("Error: --effort is not supported by the opencode backend.", file=sys.stderr)
        sys.exit(1)
    for name, _ in specs:
        hint = missing_binary(name)
        if hint:
            print(f"Error: backend '{name}' unavailable — {hint}", file=sys.stderr)
            sys.exit(1)
    # api has no repo to navigate, and opencode's read-only `plan` agent may be denied the
    # tools it would need to run git itself in non-interactive mode — so if either is
    # selected we materialize the diff/commit once and hand the SAME snapshot to every
    # backend. That both fixes opencode (it can't review nothing) and keeps a cross-model
    # fan-out honest: all backends judge identical bytes rather than each re-running git at
    # a slightly different instant. Pure codex/claude runs stay agentic (content=None).
    needs_content = any(name in {"api", "opencode"} for name, _ in specs)

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

    # Gather content to review. When needs_content, the diff/commit is materialized into a
    # single `content` shared by every backend; otherwise it stays None and codex/claude
    # run git themselves. Files, stdin, and PR sources are always materialized.
    content: str | None = None
    git_cmd: str | None = None
    commit_oid: str | None = None
    if args.pr:
        pr_description, diff = get_pr_content(args.pr, repo=args.repo)
        content = f"=== PULL REQUEST ===\n{pr_description}\n=== END PULL REQUEST ===\n\n{diff}"
        mode = "diff"
    elif args.commit:
        oid = resolve_commit(args.commit)
        if needs_content:
            content = get_commit_diff(oid)  # one snapshot for every backend
        else:
            commit_oid = oid  # let the agentic backend run git show itself
        mode = "diff"
    elif args.diff or args.staged:
        if needs_content:
            content = get_diff(args.staged)  # one snapshot for every backend
        else:
            ensure_diff_exists(args.staged)
            git_cmd = "git diff --staged" if args.staged else "git diff HEAD"
        mode = "diff"
    elif args.files:
        content = read_files(args.files)
        mode = detect_mode(args.files)
    elif not sys.stdin.isatty():
        # Piped diffs can carry non-UTF8 bytes (files in other encodings); decode
        # with replacement instead of crashing mid-pipe.
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # non-reconfigurable stdin (e.g. a test harness shim)
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
        commit=commit_oid,
        pr=bool(args.pr),
        git_cmd=git_cmd,
        model=None,
        json_output=args.json,
        effort=args.effort,
        timeout=args.timeout,
    )

    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [pool.submit(run_one, name, model, job) for name, model in specs]
        try:
            outputs = [f.result() for f in futures]  # preserves --backend order
        except KeyboardInterrupt:
            # SIGINT lands here on the main thread, not in the workers blocked on their
            # subprocesses. Tear the backend process groups down before the executor's
            # __exit__ waits on the workers, or Ctrl-C hangs until each backend times out.
            # Best-effort: this kills registered subprocess groups (codex/claude/opencode);
            # an in-flight `api` HTTP request has no process to signal and finishes or hits
            # its own client timeout.
            base.terminate_active_commands()
            raise

    results = []
    for name, model, raw, error in outputs:
        if error is not None:
            results.append(BackendResult(backend=name, model=model, error=error))
        elif args.json:
            results.append(parse_backend_output(raw, name, model))
        else:
            results.append(BackendResult(backend=name, model=model, raw=raw))

    if args.json:
        if not args.full:
            truncate_raw(results)
        print(json.dumps(to_envelope(results, fail_on=args.fail_on), indent=2))
    else:
        for r in results:
            # A failed backend's block (header + error) goes to stderr so piping stdout
            # to a file captures only real reviews; successful prose stays on stdout.
            stream = sys.stderr if r.error else sys.stdout
            if len(results) > 1:
                print(f"\n## {r.backend}{f' ({r.model})' if r.model else ''}\n", file=stream)
            if r.error:
                print(f"[backend error] {r.error}", file=sys.stderr)
            else:
                print(r.raw)
        # Next-step hint on stderr, not stdout (axi P9 places it on stdout): keeps a
        # piped `rr --diff > review.txt` clean. Text-mode success only — never in --json
        # (envelope purity), never when every backend errored (handled by the exit below).
        if not all(r.error for r in results):
            print(
                "help: rr --diff --json --fail-on high (CI gate) | "
                "rr --diff --backend codex,claude (cross-model)",
                file=sys.stderr,
            )

    if all(r.error for r in results):
        sys.exit(1)
    if any(r.error for r in results):
        print("Warning: some backends failed; findings above are partial.", file=sys.stderr)
    if args.fail_on and should_fail(results, args.fail_on):
        sys.exit(2)
