"""File-based defaults for rr: an optional user config and an optional project config.

Every key mirrors a flag, so a config file changes what rr does by default and never what
it can do. exec_commands and exec_commands_extra are the exceptions: they set what
--allow-exec allows (--exec-command replaces both for one run), and only the user file may
set them. Precedence — CLI flag > project file > user file > built-in default — lives in
resolve() and nowhere else.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rocket_review.backends import BACKENDS
from rocket_review.models import SEVERITIES
from rocket_review.repo import find_git_root, inside_dot_git as inside_dot_git

PROJECT_CONFIG_NAME = ".rocket-review.toml"
USER_CONFIG_RELPATH = Path("rocket-review") / "config.toml"

# Measured strengths, not preference: claude returns deeper code/diff findings with fewer
# false alarms, while codex keeps plan reviews focused and is the fastest of the two. A
# [backends] table overrides this per mode, and an explicit --backend outranks both.
DEFAULT_BACKEND_BY_MODE = {"plan": "codex", "code": "claude", "diff": "claude"}

MODES = tuple(DEFAULT_BACKEND_BY_MODE)

# "default" is not a mode: it names the backend for every mode the table leaves unset.
BACKENDS_TABLE_KEYS = (*MODES, "default")

# The built-in layer. None means "as if the flag were absent" — timeout falls through to
# the backend's own default, and effort/fail_on/docs are simply not applied.
FLAG_DEFAULTS: dict[str, Any] = {
    "timeout": None,
    "effort": None,
    "fail_on": None,
    "json": False,
    "full": False,
    "docs": None,
    "codex_sandbox": "read-only",
    "allow_exec": False,
    "claude_setting_sources": "user",
}

# Which Claude Code settings files the claude backend loads. Never the checkout's own:
# project and local settings could widen what the reviewer may run. "user" keeps the
# user's settings, which often carry how Claude Code reaches a model (env for a proxy or a
# cloud provider, apiKeyHelper) and also their permissions.allow rules, plugins and hooks.
# "none" loads no user, project or local settings file, so none of those reach the reviewer
# either. Managed (policy) settings apply either way.
CLAUDE_SETTING_SOURCES = ("user", "none")

# What `codex exec -s` accepts. read-only is the built-in default: a review needs to read
# the prompt file and the repository, nothing more. The wider modes exist for hosts where
# codex's own sandbox cannot start at all (a container that may not create user namespaces
# fails with "bwrap: No permissions to create a new namespace") and the surrounding
# environment is the boundary instead.
CODEX_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

# The command patterns `--allow-exec` lets the claude backend run, as Claude Code permission
# patterns: a trailing `*` matches any suffix, and a pattern without one also takes
# arguments. `exec_commands` replaces this list and `exec_commands_extra` adds to it.
DEFAULT_EXEC_COMMANDS = (
    "just test*",
    "go test",
    "pytest",
    "uv run pytest",
    "cargo test",
    "node --test",
    "npm test",
)

# Keys that widen the sandbox the reviewer runs in. A project file comes from the repository
# under review, and letting it set one of these would let the code being reviewed choose how
# much of the reviewer's machine it may touch. The user file and the flag decide them.
USER_ONLY_KEYS = {
    "codex_sandbox": "pass --codex-sandbox",
    "allow_exec": "pass --allow-exec",
    "claude_setting_sources": "pass --claude-setting-sources",
    "exec_commands": None,
    "exec_commands_extra": None,
}

FLAG_KEYS = tuple(FLAG_DEFAULTS)
ACCEPTED_KEYS = (*FLAG_KEYS, "backends", "models", "exec_commands", "exec_commands_extra")

# Precedence layer labels, as they appear in Settings.sources.
COMMAND_LINE = "command line"
BUILT_IN = "built-in default"


class ConfigError(Exception):
    """A config file is unreadable, malformed, or names something rr does not accept."""


@dataclass(frozen=True)
class Layer:
    """One config file's validated contents; `values` holds only the keys that file set."""

    path: Path
    values: dict[str, Any]
    #: True for the project file, which the repository supplies rather than the user. It is
    #: the layer's trust level, and it decides what the file's paths may reach.
    repo_supplied: bool


@dataclass(frozen=True)
class Settings:
    """Every setting rr runs with, after all four precedence layers have been applied."""

    timeout: int | None
    effort: str | None
    fail_on: str | None
    json: bool
    full: bool
    #: None for no docs, [] for auto-discovery (bare --docs), else the paths to read.
    docs: list[str] | None
    #: `codex exec -s <mode>`; one of CODEX_SANDBOX_MODES.
    codex_sandbox: str
    #: `--allow-exec`: the claude backend may run the commands in exec_commands.
    allow_exec: bool
    #: one of CLAUDE_SETTING_SOURCES.
    claude_setting_sources: str
    #: --exec-command when given; else DEFAULT_EXEC_COMMANDS or the user file's
    #: exec_commands, plus exec_commands_extra.
    exec_commands: tuple[str, ...]
    #: mode -> backend name, with the built-in table already folded in.
    backends: dict[str, str]
    #: backend name -> model, i.e. what `--backend name:model` pins.
    models: dict[str, str]
    #: one entry per flag-mirroring key above, plus `backends.<mode>` for the mode's chosen
    #: backend, naming the layer each came from — so a message about a value nobody typed
    #: can name the file responsible.
    sources: dict[str, str]

    def from_file(self, key: str) -> str | None:
        """The config file that set `key`, or None when a flag or the built-in default did."""
        source = self.sources[key]
        return None if source in (COMMAND_LINE, BUILT_IN) else source


def user_config_path() -> Path | None:
    """The user-level config path, honouring XDG_CONFIG_HOME — or None when there is none.

    Path.home() raises when HOME is unset and the uid has no passwd entry, which is an
    ordinary container setup (`docker run -u 12345`, k8s runAsUser, OpenShift). No home
    means no user config to read, not a run that cannot start.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / USER_CONFIG_RELPATH
    try:
        home = Path.home()
    except RuntimeError:
        return None
    return home / ".config" / USER_CONFIG_RELPATH


def find_project_config(start: Path) -> Path | None:
    """The nearest .rocket-review.toml from `start` up to and including the git root.

    Bounded by the repository so a stray file in $HOME or / can never be adopted as some
    project's config; outside a repository only `start` itself is considered, for the same
    reason.
    """
    start = start.resolve()
    chain = [start, *start.parents]
    root = find_git_root(start)
    searched = chain[: chain.index(root) + 1] if root is not None else chain[:1]
    for directory in searched:
        candidate = directory / PROJECT_CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_file(path: Path, *, repo_supplied: bool) -> Layer:
    """Parse and validate one config file.

    repo_supplied marks the project file, which comes from the repository rather than from
    the user. It is recorded on the layer rather than acted on here: what a path may reach
    is decided in one place for every route (see cli.resolve_doc_path).
    """
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise ConfigError(f"{path} is not valid UTF-8: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e
    values = _validate(path, data)
    for key, flag in USER_ONLY_KEYS.items():
        if repo_supplied and key in values:
            raise ConfigError(
                f"{path}: {key} is not accepted in a project file; "
                f"set it in the user config{f' or {flag}' if flag else ''}."
            )
    return Layer(
        path=path,
        values=values,
        repo_supplied=repo_supplied,
    )


def load(*, no_config: bool, cwd: Path) -> list[Layer]:
    """The config files in effect, highest precedence first: project, then user."""
    if no_config:
        return []
    layers = []
    project = find_project_config(cwd)
    if project is not None:
        layers.append(load_file(project, repo_supplied=True))
    user = user_config_path()
    if user is not None and user.is_file():
        layers.append(load_file(user, repo_supplied=False))
    return layers


def resolve(cli: dict[str, Any], layers: list[Layer]) -> Settings:
    """Settle every setting: CLI flag > project file > user file > built-in default.

    The one place precedence is decided. Scalars take the first layer that set them; the
    [backends] and [models] tables merge per entry instead, so a project file naming one
    mode or one backend's model leaves the rest of the user file in force.
    """
    stack = [(COMMAND_LINE, cli)] + [(str(layer.path), layer.values) for layer in layers]

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, builtin in FLAG_DEFAULTS.items():
        values[key], sources[key] = builtin, BUILT_IN
        for origin, layer_values in stack:
            if layer_values.get(key) is not None:
                values[key], sources[key] = layer_values[key], origin
                break

    # `docs = false` says "no docs here", which has to outrank a lower layer the way any
    # other set value does; only once the layers are settled does it become "no docs".
    if values["docs"] is False:
        values["docs"] = None

    backends = {}
    for mode in MODES:
        # Within one file the named mode outranks that file's `default`; between files the
        # higher layer wins outright, so a project file's `default` settles a mode a user
        # file names — the same order every other key follows.
        backends[mode], sources[f"backends.{mode}"] = DEFAULT_BACKEND_BY_MODE[mode], BUILT_IN
        for origin, layer_values in stack:
            table = layer_values.get("backends") or {}
            if chosen := table.get(mode) or table.get("default"):
                backends[mode], sources[f"backends.{mode}"] = chosen, origin
                break

    models: dict[str, str] = {}
    for _, layer_values in reversed(stack):  # lowest first, so a higher layer overwrites
        models.update(layer_values.get("models") or {})

    # --exec-command is the whole list for its run; a file's list still takes its extras.
    exec_commands: tuple[str, ...] = DEFAULT_EXEC_COMMANDS
    replaced_by = BUILT_IN
    for origin, layer_values in stack:
        if layer_values.get("exec_commands") is not None:
            exec_commands, replaced_by = layer_values["exec_commands"], origin
            break
    if replaced_by != COMMAND_LINE:
        for _, layer_values in reversed(stack):
            exec_commands += layer_values.get("exec_commands_extra", ())
    exec_commands = tuple(dict.fromkeys(exec_commands))

    return Settings(
        **values, exec_commands=exec_commands, backends=backends, models=models,
        sources=sources,
    )


def _names(items: list[str]) -> str:
    return ", ".join(repr(item) for item in items)


def _validate(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """Every accepted key, checked. An unknown or invalid one is an error, never an ignore."""
    unknown = sorted(set(data) - set(ACCEPTED_KEYS))
    if unknown:
        label = "key" if len(unknown) == 1 else "keys"
        raise ConfigError(
            f"{path}: unknown {label} {_names(unknown)}. "
            f"Accepted: {', '.join(sorted(ACCEPTED_KEYS))}."
        )

    values: dict[str, Any] = {}
    if "timeout" in data:
        values["timeout"] = _positive_int(path, "timeout", data["timeout"])
    if "effort" in data:
        values["effort"] = _effort(path, data["effort"])
    if "fail_on" in data:
        values["fail_on"] = _severity(path, data["fail_on"])
    for key in ("json", "full", "allow_exec"):
        if key in data:
            values[key] = _bool(path, key, data[key])
    if "docs" in data:
        values["docs"] = _docs(path, data["docs"])
    if "codex_sandbox" in data:
        values["codex_sandbox"] = _codex_sandbox(path, data["codex_sandbox"])
    for key in ("exec_commands", "exec_commands_extra"):
        if key in data:
            values[key] = exec_patterns(f"{path}: {key}", data[key])
    if "claude_setting_sources" in data:
        values["claude_setting_sources"] = _claude_setting_sources(
            path, data["claude_setting_sources"]
        )
    if "backends" in data:
        values["backends"] = _backends(path, data["backends"])
    if "models" in data:
        values["models"] = _models(path, data["models"])
    return values


def _positive_int(path: Path, key: str, value: Any) -> int:
    # bool is an int in Python, so `timeout = true` would otherwise pass as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{path}: {key} must be a positive integer, got {value!r}.")
    return value


def _string(path: Path, key: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: {key} must be a non-empty string, got {value!r}.")
    return value


def _bool(path: Path, key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{path}: {key} must be true or false, got {value!r}.")
    return value


def _codex_sandbox(path: Path, value: Any) -> str:
    if not isinstance(value, str) or value not in CODEX_SANDBOX_MODES:
        raise ConfigError(
            f"{path}: codex_sandbox must be one of {', '.join(CODEX_SANDBOX_MODES)}, "
            f"got {value!r}"
        )
    return value


def _claude_setting_sources(path: Path, value: Any) -> str:
    if not isinstance(value, str) or value not in CLAUDE_SETTING_SOURCES:
        raise ConfigError(
            f"{path}: claude_setting_sources must be one of "
            f"{', '.join(CLAUDE_SETTING_SOURCES)}, got {value!r}."
        )
    return value


def exec_patterns(origin: str, value: Any) -> tuple[str, ...]:
    """A non-empty list of command patterns, each safe to wrap as `Bash(<pattern>)`.

    `origin` names where the list came from, a config key or a flag, for the error.

    The patterns are joined into one --allowedTools argument, which Claude Code splits on
    commas and reads parentheses in, so either character could end one rule and start
    another. A leading `*` would match every command.
    """
    if (
        not isinstance(value, list) or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ConfigError(f"{origin} must be a non-empty list of strings, got {value!r}.")
    patterns = tuple(item.strip() for item in value)
    for pattern in patterns:
        if pattern.startswith("*") or any(c in pattern for c in "(),") or not pattern.isprintable():
            raise ConfigError(
                f"{origin} entry {pattern!r} must start with a command, and must not "
                "contain parentheses, commas or control characters."
            )
    return patterns


def _effort(path: Path, value: Any) -> str:
    """One token. Backends define their own levels, so the value itself is theirs to reject.

    codex takes effort as `-c model_reasoning_effort=<value>`, the one setting interpolated
    into a constructed argument rather than passed as its own argv element — and a project
    file's value comes from the repository. Every level any backend accepts is one word.
    """
    effort = _string(path, "effort", value).strip()
    if len(effort.split()) != 1:
        raise ConfigError(f"{path}: effort must be a single word, got {value!r}.")
    return effort


def _severity(path: Path, value: Any) -> str:
    if value not in SEVERITIES:
        raise ConfigError(
            f"{path}: fail_on must be one of {', '.join(SEVERITIES)}, got {value!r}."
        )
    return str(value)


def _docs(path: Path, value: Any) -> list[str] | bool:
    """true -> auto-discovery, false -> no docs, a list -> those paths.

    Paths are relative to the config file, the way every other tool reads its own config;
    `docs = true` is what asks for the current project's llms.txt / AGENTS.md / CLAUDE.md.
    """
    if isinstance(value, bool):
        return [] if value else False
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ConfigError(
            f"{path}: docs must be true, false, or a list of paths, got {value!r}."
        )
    if not value:
        # An empty list encodes what `docs = true` encodes, so accepting it would make a
        # config that names nothing turn auto-discovery on.
        raise ConfigError(
            f"{path}: docs = [] names no paths; use docs = true for auto-discovery, "
            "or docs = false for none."
        )
    # Anchored to the config file, and deliberately not resolved: the spelling matters to
    # the gate that judges it (a `.git` component survives here and is followed there), and
    # what a path may reach is one decision, taken for every route in cli.resolve_doc_path.
    root = path.parent.resolve()
    return [str(root / item) for item in value]


def _backends(path: Path, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [backends] must be a table of mode = backend.")
    unknown = sorted(set(value) - set(BACKENDS_TABLE_KEYS))
    if unknown:
        label = "mode" if len(unknown) == 1 else "modes"
        raise ConfigError(
            f"{path}: unknown {label} {_names(unknown)} in [backends]. "
            f"Accepted: {', '.join(BACKENDS_TABLE_KEYS)}."
        )
    resolved = {}
    for mode, name in value.items():
        # isinstance first: a TOML array is unhashable, so testing membership on it directly
        # would raise instead of telling the user a mode takes one backend name.
        if not isinstance(name, str) or name not in BACKENDS:
            raise ConfigError(
                f"{path}: unknown backend {name!r} in backends.{mode}. "
                f"Accepted (one per mode): {', '.join(BACKENDS)}."
            )
        resolved[mode] = name
    return resolved


def _models(path: Path, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [models] must be a table of backend = model.")
    unknown = sorted(set(value) - set(BACKENDS))
    if unknown:
        label = "backend" if len(unknown) == 1 else "backends"
        raise ConfigError(
            f"{path}: unknown {label} {_names(unknown)} in [models]. "
            f"Accepted: {', '.join(BACKENDS)}."
        )
    return {name: _string(path, f"models.{name}", model) for name, model in value.items()}
