import pytest

from rocket_review.backends import base


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
