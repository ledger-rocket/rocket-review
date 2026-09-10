"""`rr doctor` and `rr init`: what this host would do, and the file that decides it.

Both are non-interactive and bounded. doctor never prompts, never reviews anything, and
asks each backend CLI only its own cheap status command — a pre-push hook runs
`rr doctor --quiet` and soft-passes, so the command must be cheap enough to run on every
push and must never block on input.
"""

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from shutil import which
from typing import Any

from rocket_review import config
from rocket_review.backends import BACKENDS, api, missing_binary
from rocket_review.cli import rr_version, where_set

#: Status tokens. A report line opens with one, so `rr doctor | grep ^failed` works.
OK = "ok"
MISSING = "missing"
UNKNOWN = "unknown"
FAILED = "failed"

#: Only MISSING and FAILED are gaps. UNKNOWN is a probe that could not answer — a CLI with
#: no status command, one too old to have it, a timeout — and treating that as a gap would
#: fail a pre-push hook on hosts where everything is fine.
GAPS = (MISSING, FAILED)

#: Seconds for one status probe. A doctor run must stay under a few seconds in total, and a
#: backend CLI that needs longer than this to say whether it is logged in cannot say.
PROBE_TIMEOUT = 5.0


@dataclass(frozen=True)
class AuthProbe:
    """One backend's cheap "am I logged in" command, and how to read its answer.

    `refusals` are the substrings that mean the CLI answered *no*. Nothing else counts:
    a non-zero exit on its own is unknown, because an older CLI without the subcommand
    exits non-zero too, and reporting that as a broken login would send the user to
    re-authenticate an account that is fine.
    """

    cmd: list[str]
    refusals: tuple[str, ...]
    #: What to run to fix a refusal.
    login: str


AUTH_PROBES = {
    "codex": AuthProbe(
        cmd=["codex", "login", "status"],
        refusals=("not logged in", "no credentials", "run `codex login`"),
        login="codex login",
    ),
    "claude": AuthProbe(
        # Prints a small JSON status document and exits 0; ~0.1s, no network round-trip
        # required for the answer.
        cmd=["claude", "auth", "status"],
        refusals=('"loggedin": false', "not logged in", "not authenticated"),
        login="claude auth login",
    ),
    "opencode": AuthProbe(
        cmd=["opencode", "auth", "list"],
        refusals=("no credentials", "not logged in"),
        login="opencode auth login",
    ),
}


def _probe(cmd: list[str]) -> subprocess.CompletedProcess | None:
    """Run one status command, or return None when the host cannot answer.

    stdin is closed so a CLI that would prompt gets EOF instead of hanging a hook, and the
    timeout bounds the rest. Every failure to *run* is None (unknown); only a command that
    ran and spoke gets an opinion read out of it.
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _install_channel() -> str:
    """Where this rr came from — pipx, brew, pip, or unknown. Best effort, by path."""
    candidates = [str(Path(__file__).resolve()), sys.executable]
    script = which("rr")
    if script:
        candidates.append(script)
    trail = " ".join(candidates).lower()
    if "pipx" in trail:
        return "pipx"
    if "/cellar/" in trail or "homebrew" in trail or "linuxbrew" in trail:
        return "brew"
    try:
        version("rocket-review")
    except PackageNotFoundError:
        return "unknown"
    return "pip"


#: What to do about each half of the api backend, since the two fail for different reasons
#: and the repair for one does nothing for the other.
API_KEY_FIX = "make the api backend runnable: set OPENAI_API_KEY (or put it in .env)"
API_SDK_FIX = (
    "install the api backend's SDK: pipx inject rocket-review openai "
    "(or pipx install 'rocket-review[api]')"
)


def _api_ready() -> tuple[bool, str, str]:
    """(runnable, why not, what to do): the SDK installed and a key resolvable.

    The same two things `backends.available("api")` asks, kept apart from it so the reason
    and its repair can be reported rather than just the verdict. Reading .env is how the
    backend itself finds the key, so a doctor that skipped it would report a working setup
    as broken.
    """
    from rocket_review.backends import _openai_sdk_installed

    api._load_env_file()
    if not os.environ.get("OPENAI_API_KEY"):
        return False, "no OPENAI_API_KEY in the environment or .env", API_KEY_FIX
    if not _openai_sdk_installed():
        return False, "the openai SDK is not installed", API_SDK_FIX
    return True, "", ""


@dataclass
class Report:
    """The report, buffered. --quiet exists to print none of it, so nothing writes early."""

    quiet: bool
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)

    def line(self, status: str, text: str) -> None:
        self.lines.append(f"{status:<8} {text}")

    def error(self, text: str) -> None:
        self.errors.append(text)

    def fix(self, text: str) -> None:
        self.fixes.append(text)

    def flush(self) -> None:
        if self.quiet:
            return
        for line in self.lines:
            print(line)
        if self.fixes:
            print("\nfixes:")
            for fix in self.fixes:
                print(f"  - {fix}")
        for error in self.errors:
            print(error, file=sys.stderr)


def _show(value: Any) -> str:
    """A setting as the config file would spell it, so a report line is copy-pasteable."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "auto-discover"
    return str(value)


def _report_install(rep: Report) -> None:
    installed = rr_version()
    channel = _install_channel()
    rep.line(UNKNOWN if channel == "unknown" else OK, f"rr {installed} ({channel})")


def _report_config_files(rep: Report, cwd: Path) -> None:
    user = config.user_config_path()
    if user is None:
        rep.line(UNKNOWN, "user config: no home directory and no XDG_CONFIG_HOME")
    elif user.is_file():
        rep.line(OK, f"user config: {user}")
    else:
        rep.line(MISSING, f"user config: {user} (not written yet)")
        rep.fix("write the default user config: rr init")

    project = config.find_project_config(cwd)
    if project is None:
        # Optional by design, and most projects have none; not a gap, so no fix line.
        rep.line(
            MISSING,
            f"project config: no {config.PROJECT_CONFIG_NAME} found from {cwd} (optional)",
        )
    else:
        rep.line(OK, f"project config: {project}")


def _report_settings(rep: Report, cwd: Path) -> config.Settings | None:
    """The effective settings, from the loader itself — or None when the config is broken.

    A config rr refuses is a config no review would start with either, so it is a gap in its
    own right: the caller counts it and exits non-zero.
    """
    try:
        layers = config.load(no_config=False, cwd=cwd)
        settings = config.resolve(dict.fromkeys(config.FLAG_KEYS), layers)
    except config.ConfigError as e:
        rep.line(FAILED, f"settings: {e}")
        rep.fix("fix the config file named above, or run rr with --no-config")
        return None

    for key in config.FLAG_KEYS:
        value = getattr(settings, key)
        rep.line(OK, f"setting {key} = {_show(value)} ({settings.sources[key]})")
    for mode in config.MODES:
        source = settings.sources[f"backends.{mode}"]
        rep.line(OK, f"setting backends.{mode} = {settings.backends[mode]} ({source})")
    return settings


def _backend_names(chosen: list[str] | None, settings: config.Settings | None) -> list[str]:
    """The backends this host actually depends on: what --backend names, else the modes'.

    Deduplicated in mode order, because one backend usually serves several modes and a
    report that probed it twice would say the same thing twice.
    """
    if chosen is not None:
        return chosen
    table = settings.backends if settings else config.DEFAULT_BACKEND_BY_MODE
    return list(dict.fromkeys(table[mode] for mode in config.MODES))


def _settings_conflicts(
    settings: config.Settings, names: list[str]
) -> list[tuple[str, str]]:
    """(what is wrong, what to do) for each setting combination rr refuses to run with.

    The same three checks cli._run makes before it starts a review, asked here of the
    effective settings. Without them doctor would pass a host where every single review
    exits 1 on its config — which is exactly the failure a preflight exists to catch.
    Kept in step with cli._run by hand; there are three, and each is one line there.
    """
    conflicts = []
    for key in ("fail_on", "full"):
        if getattr(settings, key) and not settings.json:
            conflicts.append((
                f"{key} is set but json is not; every review would exit 1"
                + where_set(settings, key),
                f"set json = true beside {key}, or remove {key}",
            ))
    if settings.effort and "opencode" in names:
        conflicts.append((
            "effort is set and opencode is a chosen backend, which has no effort flag; "
            "every review through it would exit 1" + where_set(settings, "effort"),
            "remove effort, or point the modes opencode serves at another backend",
        ))
    return conflicts


def _auth_state(name: str) -> tuple[str, str]:
    """(status, detail) for one backend's login, never harsher than the evidence."""
    probe = AUTH_PROBES.get(name)
    if probe is None:
        return UNKNOWN, "no status command to ask"
    result = _probe(probe.cmd)
    if result is None:
        return UNKNOWN, "status command did not answer"
    spoken = f"{result.stdout}\n{result.stderr}".lower()
    # Matched twice: once as written, once with every space removed on both sides, so a
    # status document printed compactly (`{"loggedIn":false}`) is read the same as a
    # pretty-printed one. A refusal missed here reports a logged-out CLI as `ok`, which is
    # the one direction this command must never get wrong.
    squeezed = "".join(spoken.split())
    if any(
        refusal in spoken or "".join(refusal.split()) in squeezed
        for refusal in probe.refusals
    ):
        return FAILED, "not logged in"
    if result.returncode == 0:
        return OK, ""
    # Ran, said nothing this version of rr recognises — an older CLI without the
    # subcommand lands here, and it is not evidence of a broken login.
    return UNKNOWN, f"status command exited {result.returncode}"


def _report_backend(rep: Report, name: str, models: dict[str, str]) -> str:
    """One backend line: is its CLI here, is it authenticated, what model is pinned."""
    mod = BACKENDS[name]
    facts = []
    status = OK

    hint = missing_binary(name)
    if mod.BINARY is None:
        facts.append("no CLI (SDK backend)")
    elif hint:
        status = MISSING
        facts.append(f"cli not on PATH — {hint}")
        rep.fix(f"install {name}: {hint}")
    else:
        facts.append(f"cli {which(mod.BINARY)}")

    if name == "api":
        ready, why, repair = _api_ready()
        auth, detail = (OK, "") if ready else (FAILED, why)
        if not ready:
            rep.fix(repair)
    elif status == MISSING:
        auth, detail = UNKNOWN, "not probed — the CLI is not here"
    else:
        auth, detail = _auth_state(name)
        if auth == FAILED:
            probe = AUTH_PROBES[name]
            rep.fix(f"authenticate {name}: run `{probe.login}`")
    facts.append(f"auth {auth}{f' ({detail})' if detail else ''}")
    if status == OK and auth in (FAILED, UNKNOWN):
        status = auth

    pinned = models.get(name)
    if pinned:
        facts.append(f"model {pinned} (pinned)")
    elif mod.DEFAULT_MODEL:
        facts.append(f"model {mod.DEFAULT_MODEL} (backend default)")
    else:
        facts.append("model: the backend's own default")

    rep.line(status, f"backend {name} · " + " · ".join(facts))
    return status


def _parse_backends(rep: Report, value: str | None) -> list[str] | None:
    """--backend, as doctor reads it: names only, a `name:model` suffix accepted and dropped.

    The suffix is accepted so a --backend argument copied from a review command works here
    unchanged; which model is pinned is a config question, reported from the config.
    """
    if value is None:
        return None
    names = []
    for item in value.split(","):
        name = item.strip().partition(":")[0]
        if name not in BACKENDS:
            rep.error(f"Error: unknown backend '{name}'. Available: {', '.join(BACKENDS)}.")
            return []
        if name not in names:
            names.append(name)
    return names


def run_doctor(argv: list[str]) -> int:
    """0 when every backend this host depends on is here and not refusing, 1 otherwise."""
    parser = argparse.ArgumentParser(
        prog="rr doctor",
        description="Report what rr would do on this host, and what is missing.",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Print nothing; only set the exit code (for a pre-push hook)",
    )
    parser.add_argument(
        "--backend", default=None, metavar="NAMES",
        help="Comma-separated backends to check instead of the ones this host's modes use",
    )
    args = parser.parse_args(argv)

    rep = Report(quiet=args.quiet)
    try:
        names = _parse_backends(rep, args.backend)
        if names == []:
            return 2
        cwd = Path.cwd()
        _report_install(rep)
        _report_config_files(rep, cwd)
        settings = _report_settings(rep, cwd)
        chosen = _backend_names(names, settings)
        statuses = [
            _report_backend(rep, name, settings.models if settings else {}) for name in chosen
        ]
        conflicts = _settings_conflicts(settings, chosen) if settings else []
        for wrong, repair in conflicts:
            rep.line(FAILED, f"settings: {wrong}")
            rep.fix(repair)
        healthy = (
            settings is not None and not conflicts and not any(s in GAPS for s in statuses)
        )
        return 0 if healthy else 1
    except Exception as e:  # noqa: BLE001 — a diagnostic must not traceback over its report
        rep.error(f"Error: rr doctor failed internally: {type(e).__name__}: {e}")
        return 2
    finally:
        rep.flush()


DEFAULT_CONFIG_HEADER = """\
# rocket-review config. Every key mirrors a flag, so this file changes what rr does by
# default and never what it can do.
# Precedence: CLI flag > .rocket-review.toml (project) > this file > built-in default.

# Reasoning effort, passed through to the backend. medium is the everyday setting;
# --effort high is the recommended final pass before opening a PR.
effort = "medium"

# timeout = 1800       # --timeout, seconds (default 900)
# json = false         # --json, findings as a JSON envelope
# fail_on = "high"     # --fail-on, exit 2 at or above this severity (needs json = true)
# docs = true          # --docs with no path: this project's llms.txt / AGENTS.md / CLAUDE.md

[backends]             # which backend reviews each mode
"""

DEFAULT_CONFIG_MODELS = """
# The model each backend runs, i.e. what `--backend name:model` pins. These override the
# default your codex / Claude Code install would otherwise pick; delete a line to hand that
# choice back to the CLI.
[models]
codex = "gpt-6-astra"
claude = "claude-fable-5"
"""


def default_config_text() -> str:
    """The file `rr init` writes.

    The [backends] table is rendered from the built-in one, so that table cannot drift from
    what rr does with no config at all. The rest of the file is a starting opinion rather
    than a copy of the defaults: `effort` and the `[models]` pins change what runs, which
    is the point of writing them down — and why each is commented in the file itself.
    """
    modes = "".join(
        f'{mode} = "{backend}"\n' for mode, backend in config.DEFAULT_BACKEND_BY_MODE.items()
    )
    return DEFAULT_CONFIG_HEADER + modes + DEFAULT_CONFIG_MODELS


def run_init(argv: list[str]) -> int:
    """Write the default user config, once. An existing file is never overwritten silently."""
    parser = argparse.ArgumentParser(
        prog="rr init",
        description="Write the default user config file, if there is not one already.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing config file",
    )
    args = parser.parse_args(argv)

    path = config.user_config_path()
    if path is None:
        print(
            "Error: no user config path — set HOME or XDG_CONFIG_HOME and run rr init again.",
            file=sys.stderr,
        )
        return 1

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # "x" rather than a prior exists() check: the refusal to overwrite is the
        # filesystem's, so nothing can slip in between the question and the write.
        mode = "w" if args.force else "x"
        with open(path, mode, encoding="utf-8") as handle:
            handle.write(default_config_text())
    except FileExistsError:
        print(f"{path} already exists; left unchanged (rr init --force overwrites it).")
        return 0
    except OSError as e:
        print(f"Error: could not write {path}: {e}", file=sys.stderr)
        return 1
    print(f"Wrote {path}")
    return 0
