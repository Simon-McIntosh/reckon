"""Suite-wide isolation: no test reaches the real fleet, and none arms it.

Two facts made this file necessary. A test that dispatches arms a project
watch producer detached, which is right for a coordinator and wrong under a
test: the test ends and the producer survives with nothing left that knows it
exists. And two watch locks carrying test input names were found in the real
configuration home, so at least one path resolved the real home rather than a
fixture's temporary one. Both are closed here by default, in one place bound to
a moment that always happens — every test gets this fixture.

A test whose own subject is the producer lifecycle needs a real producer and
reaps what it starts. It says so with the ``arms_watch_producer`` marker; the
default is suppression, so arming is opted into rather than out of.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from reckon.crew.dispatch import WATCH_ARMING_ENV

ARMING_MARKER = "arms_watch_producer"

# Modules whose subject IS the producer lifecycle: they start producers on
# purpose and terminate them in their own teardown. Everything else is
# suppressed. Prefer the marker on new tests; these entries carry the modules
# that predate it.
_PRODUCER_LIFECYCLE_MODULES = frozenset(
    {
        "test_crew",
        "test_crew_dispatch_arming",
        "test_crew_dispatch_guard",
        "test_crew_orphan",
        "test_crew_watch_lifetime",
        "test_crew_watch_stream",
        "test_crew_watch_visibility",
        "test_crew_watchlife",
    }
)


def watch_producers_under(root: Path) -> list[tuple[int, Path]]:
    """Live watch producers whose configuration home lies under ``root``.

    A producer is bound to this run by the home it reports into, read from its
    own process environment — not by a name, and not by a parent that has
    already exited, because the supervisor is detached on purpose.
    """
    found: list[tuple[int, Path]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if b"watch" not in argv or b"--project" not in argv:
                continue
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for variable in environ:
            name, _, value = variable.partition(b"=")
            if name != b"RECKON_HOME" or not value:
                continue
            home = Path(os.fsdecode(value))
            if root == home or root in home.parents:
                found.append((int(entry.name), home))
    return found


@pytest.fixture(scope="session", autouse=True)
def reaped_watch_producers(tmp_path_factory):
    """Nothing armed against this run's temporary homes outlives the run.

    Arming is detached by design, so a producer a test starts is not the
    test's child and no teardown of the test's own can be relied on to end it:
    a suite interrupted at a fence leaves its `finally` blocks unrun. This is
    the backstop for the tests that legitimately arm, and it is bound to the
    one moment that always happens.
    """
    root = tmp_path_factory.getbasetemp()
    yield
    for pid, _home in watch_producers_under(root):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{ARMING_MARKER}: the test owns and reaps a real watch producer",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        module = getattr(item, "module", None)
        name = getattr(module, "__name__", "").rsplit(".", 1)[-1]
        if name in _PRODUCER_LIFECYCLE_MODULES:
            item.add_marker(getattr(pytest.mark, ARMING_MARKER))


@pytest.fixture(autouse=True)
def isolated_reckon_home(request, tmp_path_factory, monkeypatch):
    """Point the configuration home at a temporary tree and suppress arming."""
    home = tmp_path_factory.mktemp("reckon-home")
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv(
        WATCH_ARMING_ENV,
        "on" if request.node.get_closest_marker(ARMING_MARKER) else "off",
    )
    return home
