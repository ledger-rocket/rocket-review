"""File-based defaults for rr: an optional user config and an optional project config.

Every key mirrors a flag, so a config file changes what rr does by default and never what
it can do. Precedence — CLI flag > project file > user file > built-in default — lives in
resolve() and nowhere else.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rocket_review.backends import BACKENDS
from rocket_review.models import SEVERITIES

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
}

FLAG_KEYS = tuple(FLAG_DEFAULTS)
ACCEPTED_KEYS = (*FLAG_KEYS, "backends", "models")

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


def inside_dot_git(path: Path) -> bool:
    """Whether a path enters a .git directory, compared case-insensitively.

    Path.resolve() does not canonicalise case, so an exact ".git" comparison is bypassed by
    ".GIT/config" on the case-insensitive filesystems macOS and Windows default to — where
    the file that then opens is the real one. Repository metadata is never a standards doc:
    it is local machine state a clone does not control, credentialed remote URLs above all,
    and a doc is copied into the prompt verbatim.
    """
    return any(part.lower() == ".git" for part in path.parts)


def user_config_path() -> Path:
    """The user-level config path, honouring XDG_CONFIG_HOME when it is set."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / USER_CONFIG_RELPATH


def find_project_config(start: Path) -> Path | None:
    """The nearest .rocket-review.toml from `start` up to and including the git root.

    Bounded by the repository so a stray file in $HOME or / can never be adopted as some
    project's config; outside a repository only `start` itself is considered, for the same
    reason. A worktree's .git is a file rather than a directory, hence exists() over is_dir().
    """
    start = start.resolve()
    chain = [start, *start.parents]
    root = next((i for i, directory in enumerate(chain) if (directory / ".git").exists()), None)
    searched = chain[: root + 1] if root is not None else chain[:1]
    for directory in searched:
        candidate = directory / PROJECT_CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_file(path: Path, *, confine_docs: bool) -> Layer:
    """Parse and validate one config file.

    confine_docs bounds `docs` paths to the file's own directory. It is set for the project
    file, which comes from the repository rather than from the user: without it, a cloned
    repo could name any file on the machine and have it read and sent to a backend.
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
    return Layer(path=path, values=_validate(path, data, confine_docs=confine_docs))


def load(*, no_config: bool, cwd: Path) -> list[Layer]:
    """The config files in effect, highest precedence first: project, then user."""
    if no_config:
        return []
    layers = []
    project = find_project_config(cwd)
    if project is not None:
        layers.append(load_file(project, confine_docs=True))
    user = user_config_path()
    if user.is_file():
        layers.append(load_file(user, confine_docs=False))
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

    return Settings(**values, backends=backends, models=models, sources=sources)


def _names(items: list[str]) -> str:
    return ", ".join(repr(item) for item in items)


def _validate(path: Path, data: dict[str, Any], *, confine_docs: bool) -> dict[str, Any]:
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
    for key in ("json", "full"):
        if key in data:
            values[key] = _bool(path, key, data[key])
    if "docs" in data:
        values["docs"] = _docs(path, data["docs"], confine=confine_docs)
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


def _docs(path: Path, value: Any, *, confine: bool) -> list[str] | bool:
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
    root = path.parent.resolve()
    resolved = []
    for item in value:
        try:
            doc = (root / item).resolve()
        except (OSError, ValueError) as e:
            # An embedded null byte and the like: the path never reaches a stat, so this has
            # to come back as rr's error rather than as a traceback.
            raise ConfigError(f"{path}: docs path {item!r} is not a usable path: {e}") from e
        if confine:
            if not doc.is_relative_to(root):
                raise ConfigError(
                    f"{path}: docs path {item!r} resolves outside {root} — a project config "
                    "may only name docs inside its own directory."
                )
            if inside_dot_git(doc):
                raise ConfigError(
                    f"{path}: docs path {item!r} is inside .git — a project config may not "
                    "name repository metadata."
                )
        resolved.append(str(doc))
    return resolved


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
