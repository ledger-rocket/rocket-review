import shutil

from rocket_review.backends import api, claude, codex, opencode

BACKENDS = {m.NAME: m for m in (codex, claude, opencode, api)}


def missing_binary(name: str) -> str | None:
    """Return an install hint if the backend's CLI is absent, else None."""
    mod = BACKENDS[name]
    if mod.BINARY and shutil.which(mod.BINARY) is None:
        return mod.INSTALL_HINT
    return None
