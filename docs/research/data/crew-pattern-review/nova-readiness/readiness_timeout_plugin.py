"""Per-test timeout for the readiness census.

Pytest carries no per-test bound without a plugin, and the census may not
modify the environment or nova itself, so the census loads this module with
``-p``: it arms SIGALRM around each test and turns an overrun into a failure
whose message names the bound.
"""

import json
import os
import signal
from pathlib import Path

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


_REPORT = {"collection": [], "tests": []}


def pytest_collectreport(report):
    if report.failed or report.skipped:
        _REPORT["collection"].append(
            {
                "nodeid": report.nodeid,
                "outcome": report.outcome,
                "message": str(report.longrepr),
            }
        )


def pytest_runtest_logreport(report):
    _REPORT["tests"].append(
        {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "wasxfail": getattr(report, "wasxfail", None),
        }
    )


def pytest_collection_finish(session):
    _REPORT["selected"] = [item.nodeid for item in session.items]


def pytest_sessionfinish(session, exitstatus):
    target = os.environ.get("READINESS_REPORT")
    if target:
        _REPORT["exit_status"] = int(exitstatus)
        Path(target).write_text(json.dumps(_REPORT, sort_keys=True) + "\n")
