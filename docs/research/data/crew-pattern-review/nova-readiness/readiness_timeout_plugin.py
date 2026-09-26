"""Per-test timeout for the readiness census.

Pytest carries no per-test bound without a plugin, and the census may not
modify the environment or nova itself, so the census loads this module with
``-p``: it arms SIGALRM around each test and turns an overrun into a failure
whose message names the bound.
"""

import signal

import pytest

TIMEOUT_SECONDS = 120


class TestTimeoutError(Exception):
    pass


def _raise_timeout(signum, frame):
    raise TestTimeoutError(f"per-test timeout of {TIMEOUT_SECONDS}s exceeded")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    previous = signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(TIMEOUT_SECONDS)
    try:
        return (yield)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
