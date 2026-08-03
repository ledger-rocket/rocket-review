import os
import shutil

from rocket_review.backends import api, claude, codex, opencode

BACKENDS = {m.NAME: m for m in (codex, claude, opencode, api)}


def missing_binary(name: str) -> str | None:
    """Return an install hint if the backend's CLI is absent, else None."""
    mod = BACKENDS[name]
    if mod.BINARY and shutil.which(mod.BINARY) is None:
        return mod.INSTALL_HINT
    return None


def available(name: str) -> bool:
    """Whether this backend could run right now — the test for picking a default.

    Stricter than missing_binary for api, which ships no binary at all: its readiness is
    a resolvable API key (environment or .env, found exactly the way the backend itself
    finds it), so an automatic choice never lands on an api call that is certain to fail.
    """
    if missing_binary(name):
        return False
    if name == "api":
        api._load_env_file()
        return bool(os.environ.get("OPENAI_API_KEY"))
    return True
