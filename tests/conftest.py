import pytest

from rocket_review import cli
from rocket_review.backends import base


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    """Keep whatever config the developer running the suite has out of every test.

    Both discovery roots are redirected: XDG_CONFIG_HOME for the user file, and the working
    directory for the project file, which is found by walking up from it to the git root —
    from rocket-review's own checkout that would be the repo's own .rocket-review.toml. A
    test that wants either file writes it under tmp_path itself.
    """
    home = tmp_path / "config-home"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    # Production reads each checkout's tracked set once per process; a test that commits
    # between two reads must not be answered from the previous test's — or its own — cache.
    # Cleared on the way in only: on the way out monkeypatch may not have restored a patched
    # probe yet, and the next test clears anyway.
    cli.tracked_files.cache_clear()
    cli.case_folds.cache_clear()


@pytest.fixture(autouse=True)
def reset_interrupt_gate():
    """Keep the process-global interrupt gate from leaking between tests.

    A test that drives an interrupt latches the gate, and every later run_command() in the
    same process would then refuse to launch. Production clears it per fan-out; the suite
    shares one interpreter across all of them, so clear it per test as well.
    """
    base.begin_fanout()
    yield
    base.begin_fanout()
