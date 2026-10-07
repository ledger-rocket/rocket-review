import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rocket_review.backends import base
from rocket_review.backends.base import BackendError, ReviewJob
from rocket_review.prompts import build_agent_prompt

NAME = "claude"
BINARY = "claude"
INSTALL_HINT = "npm install -g @anthropic-ai/claude-code (https://claude.com/claude-code)"
DEFAULT_MODEL = None  # honor the user's Claude Code default model

# Read-only review sandbox, layer 1: deny by default. --allowedTools ALONE is not
# restrictive — in headless (`-p`) mode Claude Code auto-approves tools that are on
# neither the allow- nor the deny-list, so an unlisted Write/Edit or arbitrary Bash
# runs unprompted. `--permission-mode manual` flips the default to deny: with no TTY
# to grant approval, only allow-listed tools execute. Now the allowlist bounds the
# sandbox.
PERMISSION_MODE = "manual"

# Layer 2: an allowlist of pure-read tools. Git is deliberately kept OFF this wildcard
# set. `git diff`, `git show`, and `git log` all accept `--output=<file>`, which git
# opens itself — bypassing Claude Code's shell-redirect guard — so a wildcard such as
# `Bash(git diff:*)` would let prompt-injected content run `git diff --output=<path>`
# and overwrite files despite the read-only claim. The one git command needed to view
# the change under review is instead allow-listed by EXACT string (see _git_view_rule),
# so no write flag can be appended. Read/Glob/Grep cover project navigation.
READ_ONLY_TOOLS = "Read Glob Grep"

# Layer 3: no settings file the reviewer did not need. Claude Code layers settings files:
# in a workspace it trusts, the checkout's `.claude/settings.json` and
# `.claude/settings.local.json` add their permissions.allow entries to --allowedTools, so the
# branch under review could choose what its reviewer may run. The user's own settings file
# does the same for the user's rules, and also brings their hooks, plugins, skills and env.
# Every mode passes `--setting-sources` without project or local:
#   - "auth" (the default): no settings file loads; rr passes, through --settings, a file
#     holding only the keys below from the user's settings. A --settings file is a full
#     layer (its permissions merge into --allowedTools, its hooks run, its env reaches the
#     shell), so it carries no permissions, hooks or plugins, and no env outside the list.
#   - "user": the user's settings file loads whole.
#   - "none": no settings file loads.
# Managed (policy) settings apply in every mode. --strict-mcp-config with no --mcp-config
# starts no MCP server, so a repository's `.mcp.json` launches nothing either.
SETTING_SOURCES_ARG = {"auth": "", "user": "user", "none": ""}

# What "auth" keeps: how Claude Code reaches and authenticates to a model, and which model.
# Sources: https://code.claude.com/docs/en/settings (settings keys) and
# https://code.claude.com/docs/en/env-vars (environment variables).
AUTH_SETTINGS_KEYS = (
    "apiKeyHelper",
    "awsAuthRefresh",
    "awsCredentialExport",
    "gcpAuthRefresh",
    "forceLoginMethod",
    "forceLoginOrgUUID",
    "forceLoginGatewayUrl",
    "gatewayInternalNetworks",
    "allowedProviders",
    "modelOverrides",
    "model",
)
AUTH_ENV_PREFIXES = (
    "ANTHROPIC_", "CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_", "AWS_", "AZURE_", "VERTEX_REGION_",
)
AUTH_ENV_NAMES = frozenset({
    "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GCLOUD_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_CLIENT_CERT",
    "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
})


def user_settings_path() -> Path | None:
    """Where Claude Code keeps the user's settings, or None when there is no home."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "settings.json"
    try:
        return Path.home() / ".claude" / "settings.json"
    except RuntimeError:
        return None


def _auth_env_name(name: str) -> bool:
    return name in AUTH_ENV_NAMES or name.startswith(AUTH_ENV_PREFIXES)


def auth_settings(path: Path | None) -> dict[str, Any]:
    """The part of the user's settings file that "auth" passes on; {} when there is none.

    A file that exists but cannot be read as a JSON object is an error rather than an
    empty result: falling back would run the review without the auth it was meant to keep.
    """
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BackendError(f"could not read Claude Code settings {path}: {e}") from None
    if not isinstance(data, dict):
        raise BackendError(f"Claude Code settings {path} is not a JSON object")
    kept = {key: data[key] for key in AUTH_SETTINGS_KEYS if key in data}
    env = data.get("env")
    if isinstance(env, dict):
        kept_env = {name: value for name, value in env.items() if _auth_env_name(name)}
        if kept_env:
            kept["env"] = kept_env
    return kept


@contextmanager
def _settings_args(job: ReviewJob) -> Iterator[list[str]]:
    """The settings arguments for one claude run, with any --settings file it needs.

    The file goes on disk rather than inline: a command line is readable by every local
    user through /proc, and an apiKeyHelper or proxy URL can carry a secret. It sits in a
    private directory, mode 0600, and is removed when the run ends, however it ends.
    """
    try:
        sources = SETTING_SOURCES_ARG[job.claude_setting_sources]
    except KeyError:
        raise BackendError(
            f"unknown claude setting sources {job.claude_setting_sources!r}"
        ) from None
    args = ["--setting-sources", sources, "--strict-mcp-config"]
    kept = auth_settings(user_settings_path()) if job.claude_setting_sources == "auth" else {}
    if not kept:
        yield args
        return
    directory = tempfile.mkdtemp(prefix="rr-claude-")
    try:
        fd, name = tempfile.mkstemp(suffix=".json", dir=directory)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(kept, f)
        yield [*args, "--settings", name]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _exec_rules(commands: tuple[str, ...]) -> list[str]:
    """Bash allow rules for `--allow-exec`'s command patterns.

    A pattern without a trailing `*` also takes arguments: Claude Code matches
    `Bash(go test *)` against bare `go test` and against `go test ./...`. It splits a
    command on shell operators and checks each part, so `go test && rm -rf x` needs both
    parts allowed and is denied.
    """
    return [
        f"Bash({pattern})" if pattern.endswith("*") else f"Bash({pattern} *)"
        for pattern in commands
    ]


def _git_view_command(job: ReviewJob) -> str | None:
    """The single git command that surfaces the change, or None when the prompt inlines it.

    Same precedence as build_agent_prompt, which decides what the prompt asks the reviewer to
    run: an inlined pull request needs no git, then --commit, then --diff/--staged. Resolving
    it in another order would allow-list one command and ask for another, and the reviewer
    would meet a denial on the command the prompt named.
    """
    if job.pr and job.content:
        return None
    if job.commit:
        return f"git show {job.commit}"
    return job.git_cmd or None


def _git_view_rule(job: ReviewJob) -> str | None:
    """Exact-match Bash allow rule for the single git command that surfaces the change.

    build_agent_prompt directs the agent to run this exact command for `--diff` /
    `--staged` / `--commit`; allow only it — no `:*` wildcard — so an `--output` (or
    any other) write flag can't be appended. Returns None for sources whose content is
    already inlined in the prompt (PR, files, stdin, plan), which need no git at all.
    """
    command = _git_view_command(job)
    return f"Bash({command})" if command else None


def _environment(job: ReviewJob) -> str:
    """What this sandbox lets the reviewer do, stated in its prompt.

    Built from the same sources as the allowlist, so the prompt cannot promise a tool that
    `--allowedTools` denies or hide one it allows.
    """
    names = READ_ONLY_TOOLS.split()
    tools = f"the {', '.join(names[:-1])} and {names[-1]} tools"
    command = _git_view_command(job)
    if command:
        tools += f", and the single shell command `{command}`"
    if not job.exec_commands:
        return (
            f"This review runs in a read-only sandbox. You can use only {tools}. Every other "
            "command is denied, including tests, type checks, linters, builds, package "
            "managers, gh, and any other git command, so do not attempt them."
        )
    commands = ", ".join(f"`{pattern}`" for pattern in job.exec_commands)
    return (
        f"This review runs in a sandbox that allows reading and a fixed set of test commands. "
        f"You can use only {tools}, and these test commands in the working directory, each "
        f"with any arguments: {commands}. When running a test would confirm or refute a "
        "defect you suspect, run it. The commands run on the working tree as it is now, which "
        "matches the change under review only when the review is of the uncommitted changes; "
        "for a commit, a staged change or a supplied diff, check that the tree holds the "
        "change before you cite a result. Run tests only to observe them: pass no argument "
        "that writes or updates files, such as a snapshot or golden-file update flag or an "
        "output path. Name in your review each command you ran and its "
        "result. Every other command is denied, including type checks, linters, builds, "
        "package managers, gh, and any other git command, so do not attempt them."
    )


def prompt(job: ReviewJob) -> str:
    """The instructions this backend sends, and the text `rr --fingerprint` hashes."""
    return build_agent_prompt(job, _environment(job))


def review(job: ReviewJob) -> str:
    allowed = READ_ONLY_TOOLS.split()
    git_rule = _git_view_rule(job)
    if git_rule:
        allowed.append(git_rule)
    allowed += _exec_rules(job.exec_commands)
    timeout = base.TIMEOUT if job.timeout is None else job.timeout
    with _settings_args(job) as settings_args:
        cmd = [
            "claude", "-p",
            "--permission-mode", PERMISSION_MODE,
            *settings_args,
            "--allowedTools", " ".join(allowed),
        ]
        # --model outranks a `model` the settings file carries.
        if job.model:
            cmd += ["--model", job.model]
        if job.effort:
            cmd += ["--effort", job.effort]
        # Prompt goes via stdin: no ARG_MAX concern and no temp file needed.
        output = base.run_command(
            cmd, stdin=prompt(job), timeout=timeout
        ).strip()
    if not output:
        raise BackendError("claude produced no output")
    return output
