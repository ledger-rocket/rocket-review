import argparse
import json
import os
import re
import shutil
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import unquote, urldefrag

from rocket_review import config
from rocket_review.backends import BACKENDS, available, base, missing_binary
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.models import (
    BackendResult,
    parse_backend_output,
    should_fail,
    to_envelope,
)
from rocket_review.repo import (
    case_folds as case_folds,
    resolve_doc_path,
    resolve_doc_paths,
    run_capture,
    tracked_files as tracked_files,
)

RAW_TRUNCATE_LIMIT = 4000

# Stand-ins when a mode's default is not available, closest substitute first: the other
# agentic CLI reviews the same way, opencode is agentic but provider-dependent, and api
# cannot navigate the project at all (and needs a key).
FALLBACK_ORDER = ["codex", "claude", "opencode", "api"]


def rr_version() -> str:
    """The installed distribution's version, or a marker when running from a checkout."""
    try:
        return version("rocket-review")
    except PackageNotFoundError:
        return "unknown (source checkout)"


def truncate_raw(results: list[BackendResult]) -> None:
    """Truncate oversized raw output inline; never spill it to disk.

    A truncated review can quote proprietary code or secrets, so it must not be written
    to a world-readable temp file that nothing ever cleans up. The envelope stays bounded
    by dropping the tail; the truncation marker names the full length and points at
    --full, which inlines the complete text on demand.
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


@dataclass(frozen=True)
class DocsSource:
    """A config file's `docs` value: where discovery looks, and whose word its paths are."""

    #: The config file itself, so a refusal can name what asked for the path.
    config_file: Path
    #: The project the file speaks for: the directory holding a project config, or the
    #: checkout a user config is being used in — never ~/.config, which is no project.
    discovery_root: Path
    #: Whether this file is repository content rather than the user's own.
    repo_supplied: bool


def docs_source(settings: config.Settings, layers: list[config.Layer]) -> DocsSource | None:
    """The config file that supplied `docs`, or None when the flag did — or nothing did."""
    origin = settings.from_file("docs")
    if origin is None:
        return None
    layer = next(item for item in layers if str(item.path) == origin)
    if layer.repo_supplied:
        return DocsSource(layer.path, layer.path.parent, repo_supplied=True)
    # A user config speaks for whatever project it is used in, so discovery follows the
    # checkout rather than ~/.config, where no project's standards live.
    cwd = Path.cwd()
    return DocsSource(layer.path, config.find_git_root(cwd) or cwd, repo_supplied=False)


# What a refused doc path is told, and what the README states: one rule, one wording.
DOC_RULE = (
    "rr reads a doc the repository chose only when the repository tracks it, it resolves "
    "inside the directory it came from, and it is not repository metadata (.git). Name a "
    "path yourself with --docs/--llms to read anything else"
)

#: Why a path the *user* named was refused. Neither the tracked rule nor the confinement
#: applies to their own word, so the only ways left to fail are the .git footgun check and
#: a path that will not resolve at all — and a message reciting the rest would misdirect.
NAMED_DOC_RULE = (
    "rr never reads repository metadata (.git) into a review, and a path it cannot resolve "
    "it cannot read"
)

#: `--llms` with no path. A distinct object, not the string "llms.txt", so the bare flag
#: (the repository's llms.txt if it has one) stays distinguishable from `--llms llms.txt`
#: (the user naming a file) — argparse cannot tell a const from a value otherwise, and the
#: two sit on opposite sides of the trust boundary.
LLMS_DISCOVER = object()


def read_doc_with_links(doc_path: Path) -> str:
    """Read a doc and follow its relative markdown links to build full project context.

    A link is written by whoever wrote the doc, so every one of them goes through
    resolve_doc_path as repository-chosen — including out of a doc the user named, since
    what a doc points at is not what the user vouched for.
    """
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

    links = []
    for raw_link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", doc_text):
        if raw_link.startswith(("http://", "https://", "#")):
            continue
        link, _ = urldefrag(unquote(raw_link))
        if link:
            links.append(link)

    # Every link judged in one pass, so an index with hundreds of them asks git once.
    allowed = resolve_doc_paths(
        (base_dir / link for link in links), user_named=False, base=base_dir,
    )
    for link, linked_path in zip(links, allowed, strict=True):
        if linked_path is None:
            print(f"Warning: skipping link {link}: {DOC_RULE}.", file=sys.stderr)
            continue
        if not linked_path.is_file():
            continue
        try:
            parts.append(f"--- {link} ---\n{linked_path.read_text(encoding='utf-8')}")
        except (OSError, UnicodeDecodeError) as e:
            print(f"Warning: could not read {link}: {e}", file=sys.stderr)

    return "\n\n".join(parts)


DISCOVERY_CANDIDATES = ["llms.txt", "AGENTS.md", "CLAUDE.md"]


def collect_docs(
    docs_args: list[str] | None,
    llms_arg: str | object | None,
    *,
    source: DocsSource | None = None,
) -> str | None:
    """Assemble standards context from --docs (explicit or auto-discovered) and --llms.

    `source` is set when a config file supplied `docs` rather than the flag. Being a standing
    preference rather than this run's instruction changes two things: discovery looks at the
    project the file speaks for instead of wherever the user is standing, and a project with
    no standards doc is simply a project without one, where the typed flag asked for
    something specific and errors.

    `--llms` is the compatibility alias for `--docs` and sits on the same boundary: a bare
    flag is the repository's llms.txt if it carries one, a path is the user naming a file.

    Which paths are the user's own word and which the repository chose is decided here and
    settled by resolve_doc_path; nothing downstream re-decides it.
    """
    cwd = Path.cwd()

    def require(path: Path, *, user_named: bool, base: Path, named_by: Path | None) -> Path:
        """A path someone named outright: refusing it stops the run.

        named_by is the config file that named this path, or None when the command line
        did. Carried explicitly rather than inferred: a config may be supplying `docs`
        while the refused path came from a flag, and blaming the file for it would send
        the user to edit something they did not write.
        """
        allowed = resolve_doc_path(path, user_named=user_named, base=base)
        if allowed is None:
            origin = f"{named_by}: " if named_by else ""
            rule = NAMED_DOC_RULE if user_named else DOC_RULE
            print(f"Error: {origin}refusing docs path {str(path)!r} — {rule}.",
                  file=sys.stderr)
            sys.exit(1)
        return allowed

    def offer(path: Path, *, base: Path) -> Path | None:
        """A path discovery turned up: refusing it skips it, since discovery meant "if any".

        Never the user's own word — whoever asked for the pattern, the repository decides
        which file answers it.
        """
        allowed = resolve_doc_path(path, user_named=False, base=base)
        if allowed is None:
            print(f"Warning: skipping {path.name}: {DOC_RULE}.", file=sys.stderr)
        return allowed

    paths: list[Path] = []
    if llms_arg is LLMS_DISCOVER:
        # Bare --llms is bare --docs narrowed to llms.txt: the repository decides whether it
        # has one, so the file it offers is not the user's own word.
        candidate = cwd / "llms.txt"
        kept = offer(candidate, base=cwd) if candidate.is_file() else None
        if kept is None:
            print("Error: --llms given without a path and llms.txt cannot be read here.",
                  file=sys.stderr)
            sys.exit(1)
        paths.append(kept)
    elif isinstance(llms_arg, str) and llms_arg:
        paths.append(require(Path(llms_arg), user_named=True, base=cwd, named_by=None))

    if docs_args is not None and len(docs_args) == 0:
        search_root = source.discovery_root if source else cwd
        found = [search_root / c for c in DISCOVERY_CANDIDATES if (search_root / c).is_file()]
        kept_docs = [
            allowed for allowed in (offer(p, base=search_root) for p in found)
            if allowed is not None
        ]
        if not kept_docs and source is None:
            refused = " (" + ", ".join(c.name for c in found) + " refused)" if found else ""
            print(
                "Error: --docs given without paths and none of "
                f"{', '.join(DISCOVERY_CANDIDATES)} can be read here{refused}.",
                file=sys.stderr,
            )
            sys.exit(1)
        paths.extend(kept_docs)
    elif docs_args:
        # A project config is repository content; the user's own config is not.
        user_named = source is None or not source.repo_supplied
        base = source.config_file.parent if source else cwd
        named_by = source.config_file if source else None
        paths.extend(
            require(Path(raw), user_named=user_named, base=base, named_by=named_by)
            for raw in docs_args
        )

    seen: set[Path] = set()
    parts: list[str] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        parts.append(read_doc_with_links(path))
    return "\n\n".join(parts) if parts else None


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
    # Return the diff verbatim (strip only decides emptiness): trailing whitespace on the
    # last changed line is part of the patch, and this snapshot must be byte-identical to
    # what every backend reviews.
    if not result.stdout.strip():
        label = "staged changes" if staged else "uncommitted changes"
        print(f"Error: no {label} found.", file=sys.stderr)
        sys.exit(1)
    return result.stdout


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
    # Verbatim (strip only decides emptiness); see get_diff — the snapshot must be
    # byte-identical to what every backend reviews.
    if not result.stdout.strip():
        print(f"Error: no output for commit {oid}.", file=sys.stderr)
        sys.exit(1)
    return result.stdout


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


def resolve_default_backend(
    default: str, mode: str, model: str | None, effort: str | None, *, origin: str = "",
) -> str:
    """The mode's default backend, or an available stand-in — announced, never silent.

    Returns the unavailable default when nothing else is available either, so the caller's
    missing-backend error names the backend the mode actually asked for.

    A --model rides along to whichever backend is substituted, where a model name from the
    absent vendor fails at the backend, so the notice names it as part of what will run.
    """
    if available(default):
        return default
    for candidate in FALLBACK_ORDER:
        if candidate == default or not available(candidate):
            continue
        if effort and candidate == "opencode":
            # opencode has no reasoning-effort flag, so substituting it here would
            # manufacture a usage error out of a request the user made of another backend.
            continue
        with_model = f" with --model {model}" if model else ""
        print(
            f"Note: default backend '{default}' for {mode} review is unavailable; "
            f"using '{candidate}'{with_model}. "
            f"Pass --backend to choose explicitly and silence this.{origin}",
            file=sys.stderr,
        )
        return candidate
    return default


def where_set(settings: config.Settings, key: str, label: str | None = None) -> str:
    """Name the config file a value came from, for a message the user typed no flag for.

    A label carries keys whose spelling in the file may differ from the setting they
    settled — `backends.default` names a mode's backend just as `backends.diff` does.
    """
    origin = settings.from_file(key)
    return f" ({label or key} is set in {origin})" if origin else ""


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
    name: str, model: str | None, job_template: ReviewJob, commit_content: str | None
) -> tuple[str, str | None, str | None, str | None]:
    job = replace(job_template, model=model)
    # A commit OID is immutable, so codex/claude keep it and `git show` the exact commit
    # (no snapshot-drift risk, and they don't front-load a large diff into the prompt).
    # api/opencode can't run git, so they get the commit materialized instead. A mutable
    # working-tree diff is already captured once into job.content and shared by everyone.
    if commit_content is not None and name in {"api", "opencode"}:
        job = replace(job, content=commit_content, commit=None)
    try:
        raw = BACKENDS[name].review(job)
        # Central fail-closed check, defence in depth: all four shipped backends already
        # reject blank output before returning, so this only catches a future or
        # misbehaving one. Blank raw would read as success under the downstream truthiness
        # checks, so it must never get past here.
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
            "  rr --version                           # print the installed version\n"
        ),
    )
    parser.add_argument("files", nargs="*", help="Files to review")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {rr_version()}",
        help="Show the installed rocket-review version and exit",
    )
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
        "--backend", default=None,
        help="Comma-separated backends: codex, claude, opencode, api. "
             "Per-backend model via name:model (e.g. codex:gpt-5.6-sol,claude). "
             "Defaults per mode: plan -> codex, code -> claude, diff -> claude.",
    )
    parser.add_argument(
        "--model", default=None,
        help="Model for the single selected backend (codex uses your codex default "
             "from ~/.codex/config.toml, e.g. gpt-5.6-sol on ChatGPT plans; api defaults "
             "to gpt-5.6-terra; claude/opencode use the tool's own default). "
             "With multiple backends use --backend name:model instead.",
    )
    parser.add_argument(
        "--mode",
        # The same set the per-mode default backend table is keyed by; a mode outside it
        # would have no default to resolve.
        choices=list(config.MODES),
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
        "--llms", nargs="?", const=LLMS_DISCOVER, metavar="PATH",
        help="Alias for --docs llms.txt (kept for compatibility). Bare, it takes the "
             "project's llms.txt if the repository carries one; with a PATH it reads what "
             "you name, exactly as --docs does.",
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="Alias for --backend api: OpenAI API directly, no project navigation "
             "(auto-extracts referenced files)",
    )
    # store_true with default=None so an absent flag is distinguishable from an explicit
    # false, which is what lets a config file supply the value without overriding the flag.
    parser.add_argument(
        "--json", action="store_true", default=None,
        help="Emit findings as a JSON envelope instead of prose",
    )
    parser.add_argument(
        "--fail-on", choices=["critical", "high", "medium", "low"],
        help="Exit 2 if any finding is at or above this severity (requires --json)",
    )
    parser.add_argument(
        "--full", action="store_true", default=None,
        help="Inline full backend output in the --json envelope (skip truncation)",
    )
    parser.add_argument(
        "--no-config", action="store_true",
        help="Ignore .rocket-review.toml and ~/.config/rocket-review/config.toml",
    )

    args = parser.parse_args()

    # Settled before anything else runs: a bad config must fail before any git, gh, or
    # backend work, and every check below judges the effective value, not just the flag.
    try:
        layers = config.load(no_config=args.no_config, cwd=Path.cwd())
        settings = config.resolve({key: getattr(args, key) for key in config.FLAG_KEYS}, layers)
    except config.ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if settings.fail_on and not settings.json:
        print(
            "Error: --fail-on requires --json (findings must be parsed to be gated)."
            + where_set(settings, "fail_on") + where_set(settings, "json"),
            file=sys.stderr,
        )
        sys.exit(1)

    if settings.full and not settings.json:
        print(
            "Error: --full requires --json (text mode never truncates)."
            + where_set(settings, "full") + where_set(settings, "json"),
            file=sys.stderr,
        )
        sys.exit(1)

    if args.repo and not args.pr:
        print("Error: --repo only applies with --pr.", file=sys.stderr)
        sys.exit(1)

    if args.diff and args.staged:
        # Not silently staged-only: the user likely expects both sets reviewed.
        print("Error: specify only one of --diff or --staged.", file=sys.stderr)
        sys.exit(1)

    if args.api:
        if args.backend is not None:
            print(
                "Error: --api conflicts with --backend; --api is shorthand for --backend api.",
                file=sys.stderr,
            )
            sys.exit(1)
        args.backend = "api"

    # An explicit --backend is parsed here so a typo or a duplicate fails before any git/gh
    # work; the per-mode default can only be resolved once the mode is known, below.
    specs = parse_backend_arg(args.backend, args.model) if args.backend is not None else None

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

    # The mode is settled before the backend: the default backend follows the mode, and the
    # chosen backends in turn decide whether the review content has to be materialized.
    # Pure flag inspection — no git, gh, or stdin read happens here.
    if args.pr or args.commit or args.diff or args.staged:
        mode = "diff"
    elif args.files:
        mode = detect_mode(args.files)
    elif not sys.stdin.isatty():
        mode = "diff"
    else:
        parser.print_help()
        sys.exit(1)
    if args.mode:
        mode = args.mode

    # Empty when --backend was passed: the config's table did not decide this run's backend,
    # so naming the file that holds it would misdirect.
    backend_origin = ""
    if specs is None:
        backend_origin = where_set(settings, f"backends.{mode}", f"the {mode} backend")
        default_backend = resolve_default_backend(
            settings.backends[mode], mode, args.model, settings.effort, origin=backend_origin,
        )
        specs = parse_backend_arg(default_backend, args.model)

    # A model pinned on the command line (--backend name:model, or --model) stays as pinned;
    # [models] fills in the rest, exactly as writing the suffix out would have.
    specs = [(name, model or settings.models.get(name)) for name, model in specs]

    if settings.effort and any(name == "opencode" for name, _ in specs):
        # opencode has no reasoning-effort flag; drop nothing silently.
        print(
            "Error: --effort is not supported by the opencode backend."
            + where_set(settings, "effort"),
            file=sys.stderr,
        )
        sys.exit(1)
    for name, _ in specs:
        hint = missing_binary(name)
        if hint:
            print(f"Error: backend '{name}' unavailable — {hint}{backend_origin}",
                  file=sys.stderr)
            sys.exit(1)
    # api has no repo to navigate, and opencode's read-only `plan` agent may be denied the
    # tools it would need to run git itself in non-interactive mode — so if either is
    # selected we materialize content for them. A mutable working-tree diff is captured
    # ONCE and shared by every backend (so a cross-model fan-out judges identical bytes,
    # not each re-running git at a slightly different instant). A commit is immutable, so
    # only api/opencode get it materialized (run_one) while codex/claude git-show the OID.
    needs_content = any(name in {"api", "opencode"} for name, _ in specs)

    # Gather content to review. For a mutable working-tree diff, needs_content captures one
    # `content` shared by every backend; otherwise it stays None and codex/claude run git.
    # For a commit, codex/claude always git-show the OID (commit_oid) and api/opencode get
    # the diff via commit_content (run_one). Files, stdin, and PR sources are materialized.
    # The branch conditions mirror the mode block above, which has already exited when none
    # of them holds.
    content: str | None = None
    commit_content: str | None = None
    git_cmd: str | None = None
    commit_oid: str | None = None
    if args.pr:
        pr_description, diff = get_pr_content(args.pr, repo=args.repo)
        content = f"=== PULL REQUEST ===\n{pr_description}\n=== END PULL REQUEST ===\n\n{diff}"
    elif args.commit:
        commit_oid = resolve_commit(args.commit)
        if needs_content:
            commit_content = get_commit_diff(commit_oid)  # for api/opencode
    elif args.diff or args.staged:
        if needs_content:
            content = get_diff(args.staged)  # one snapshot for every backend
        else:
            ensure_diff_exists(args.staged)
            git_cmd = "git diff --staged" if args.staged else "git diff HEAD"
    elif args.files:
        content = read_files(args.files)
    else:
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

    # Read project standards docs
    docs_content = collect_docs(settings.docs, args.llms, source=docs_source(settings, layers))

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
        json_output=settings.json,
        effort=settings.effort,
        timeout=settings.timeout,
        foreign_repo=bool(args.repo),
    )

    base.begin_fanout()
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        try:
            futures = [
                pool.submit(run_one, name, model, job, commit_content) for name, model in specs
            ]
            outputs = [f.result() for f in futures]  # preserves --backend order
        except KeyboardInterrupt:
            # SIGINT lands on the main thread, not in the workers blocked on their
            # subprocesses, so it can arrive anywhere in this block — including mid-submit,
            # once an earlier worker has already launched a billed backend. Both phases are
            # therefore inside the same handler.
            #
            # Order matters. Closing the gate first means every backend is accounted for:
            # one already running is in terminate_active_commands()' snapshot, and one whose
            # worker has not reached its launch yet gives up instead of starting a backend
            # nothing is left to reap. Reversed, that second worker would launch after the
            # snapshot and the executor's non-daemon threads would hold the CLI open for its
            # full timeout. cancel_futures then drops any work item no thread has picked up
            # yet, so those never reach the gate at all. The teardown itself unblocks the
            # workers already in communicate(), before the executor's __exit__ waits on them.
            #
            # Best-effort by design: this kills registered subprocess groups
            # (codex/claude/opencode); an in-flight `api` HTTP request has no process to
            # signal and finishes or hits its own client timeout.
            base.request_interrupt()
            pool.shutdown(wait=False, cancel_futures=True)
            base.terminate_active_commands()
            raise

    results = []
    for name, model, raw, error in outputs:
        if error is not None:
            results.append(BackendResult(backend=name, model=model, error=error))
        elif settings.json:
            results.append(parse_backend_output(raw, name, model))
        else:
            results.append(BackendResult(backend=name, model=model, raw=raw))

    if settings.json:
        if not settings.full:
            truncate_raw(results)
        print(json.dumps(to_envelope(results, fail_on=settings.fail_on), indent=2))
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
        # Next-step hint on stderr, not stdout: keeps a
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
    if settings.fail_on and should_fail(results, settings.fail_on):
        sys.exit(2)
