import importlib.util
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


def _openai_sdk_installed() -> bool:
    """Whether the OpenAI SDK could be imported — probed without importing it.

    Fails closed: an unresolvable spec only means api is not offered automatically, which
    is the safe answer for a probe whose whole job is to avoid a doomed choice.
    """
    try:
        return importlib.util.find_spec("openai") is not None
    except (ImportError, ValueError):
        return False


def available(name: str) -> bool:
    """Whether this backend could run right now — the test for picking a default.

    Stricter than missing_binary for api, which ships no binary at all: it needs both the
    SDK (an optional extra, absent from a base install) and a resolvable API key
    (environment or .env, found exactly the way the backend itself finds it). Those are
    the two things the backend refuses on, so an automatic choice never lands on an api
    call that is certain to fail.
    """
    if missing_binary(name):
        return False
    if name == "api":
        api._load_env_file()
        return bool(os.environ.get("OPENAI_API_KEY")) and _openai_sdk_installed()
    return True
