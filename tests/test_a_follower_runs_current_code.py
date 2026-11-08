"""A station subscriber re-executes onto its source checkout's code.

A subscriber to a station renders every row with its own code: the station
commits rows, and ``station_subscriber`` composes the sentences the pane prints.
So a long-lived subscriber keeps the wording of the image it was armed with,
including a remedy the source has since removed, unless it replaces its own
image when the checkout moves. These cases enter that delivery — the follower's
own subscriber path, with a stubbed subscriber and a recorded exec — so what
they observe is the reloader that path builds and polls through its per-wait
hook, not one a test assembles beside it.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from reckon import checkout_trust
from reckon.crew import follower_reload, runs
from reckon.crew_follow_commands import (
    _FOLLOWER_IMAGE_COMMIT_ENV,
    _FOLLOWER_SOURCE_ROOT_ENV,
    crew_follow,
)

# Two identities a checkout can report. ``IMPORTED`` is what the tree this
# process imports from answers, and stands in for the image a follower is
# already running; ``MOVED`` is a source checkout that has moved on.
IMPORTED = "a" * 40
MOVED = "b" * 40
SWEEPS = 100


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch) -> Path:
    """A crew home of this test's own, so no run is registered anywhere real."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


@pytest.fixture()
def identity(monkeypatch, tmp_path):
    """A stubbed checkout identity: one value per directory, no git needed.

    ``checkout`` is the source checkout a follower names through the carried
    environment variable. Every directory the identity read is asked about is
    recorded, so a case can show which tree the comparison actually read.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    reads: list[Path] = []
    values: dict[Path, Any] = {}

    def read(root):
        root = Path(root)
        reads.append(root)
        queued = values.get(root, IMPORTED)
        if isinstance(queued, list):
            # A list names the identity a checkout reports on each successive
            # read, so a case can move the checkout between the arming read and
            # the sweep that compares against it.
            return queued.pop(0) if len(queued) > 1 else queued[0]
        return queued

    monkeypatch.setattr(checkout_trust, "_committed_reload_tree", read)
    monkeypatch.setattr(checkout_trust, "_head_identity", lambda root: None)
    monkeypatch.setattr(checkout_trust, "published_reload_image", lambda root: False)
    monkeypatch.delenv(_FOLLOWER_SOURCE_ROOT_ENV, raising=False)
    monkeypatch.delenv(_FOLLOWER_IMAGE_COMMIT_ENV, raising=False)
    return SimpleNamespace(checkout=checkout, reads=reads, values=values)


@pytest.fixture()
def reexec(monkeypatch, tmp_path):
    """A trusted replacement image whose exec is recorded instead of performed."""
    image = tmp_path / "image"
    image.mkdir()
    materialized: list[tuple[Path, str]] = []
    execed: list[list[str]] = []

    def materialize(source_root, commit, *, active_root=None):
        materialized.append((Path(source_root), commit))
        return image, [], [], None

    def refuse(path, argv, environment):
        execed.append(list(argv))
        raise OSError("execution refused")

    monkeypatch.setattr(checkout_trust, "materialize_committed_tree", materialize)
    monkeypatch.setattr(checkout_trust, "reckon_imports", lambda root: (True, ""))
    monkeypatch.setattr(os, "execve", refuse)
    return SimpleNamespace(execed=execed, materialized=materialized, image=image)


class _Subscriber:
    """A station subscriber stubbed to the shape the delivery reads.

    The delivery reads the pane's place from its cursor path, takes the
    session's claim through its registration, and reads and appends its memory;
    a station itself is not needed to exercise any of that.
    """

    def __init__(self) -> None:
        self.path = SimpleNamespace(reported={}, lift_groups=set())
        self.history: list[dict] = []
        self.persist = False
        self.registered: list[dict] = []
        self.detached = 0
        self.store = SimpleNamespace(close=lambda: None)

    def register(self, **kwargs) -> None:
        self.registered.append(kwargs)

    def detach(self) -> None:
        self.detached += 1

    def append_history(self, **kwargs) -> None:
        self.history.append(kwargs)


@pytest.fixture()
def delivery(monkeypatch):
    """Enter the follower's subscriber delivery with a stubbed subscriber.

    ``_follow_watch_lines`` is replaced by a stub that hands the delivery's own
    per-wait hook the checkpoint it would hand a live loop, once per sweep and
    then returns. Everything above that hook is the production path: the
    subscriber is detected, the reloader is built at its delivery site, and the
    exec it decides on is recorded.
    """
    subscriber = _Subscriber()
    monkeypatch.setattr(
        "reckon.crew_follow_commands._station_subscriber_for",
        lambda *args, **kwargs: subscriber,
    )
    monkeypatch.setattr(
        "reckon.crew_follow_commands._hand_off_from_follower",
        lambda *args, **kwargs: False,
    )
    sweeps = {"count": 1}
    # Set by the caller to read the identity reads already made when the loop
    # is entered, so a case can separate the arming read from the sweeps'.
    probe: list = [list]

    def fake_follow(project, **kwargs):
        sweeps.setdefault("reads_at_arm", len(probe[0]()))
        on_poll = kwargs["on_poll"]
        for _ in range(sweeps["count"]):
            on_poll({"reported": {}, "offset": 0})
        return iter(())

    monkeypatch.setattr("reckon.crew_follow_commands._follow_watch_lines", fake_follow)

    def run() -> None:
        crew_follow.callback(
            project="proj",
            session="session-a",
            observe_sessions=(),
            run_ids=(),
            attention=False,
            json_output=False,
            pretty=False,
            lifetime=None,
            width=0,
            theme=None,
            no_color=True,
        )

    return SimpleNamespace(run=run, sweeps=sweeps, subscriber=subscriber, probe=probe)


def test_a_moved_checkout_re_executes_the_subscriber(
    identity, reexec, monkeypatch, delivery
) -> None:
    """The identity the subscriber reads is its checkout's, and it acts on it.

    The follower runs the image it was armed with (``IMPORTED``) while the
    checkout named in the carried environment has moved to ``MOVED``. A
    subscriber that never reloads keeps printing the old image's wording, so
    the reload is what makes its rows current.
    """
    monkeypatch.setenv(_FOLLOWER_SOURCE_ROOT_ENV, str(identity.checkout))
    # The checkout moves between the delivery arming its image and its first
    # sweep, which is the whole situation: the subscriber is long-lived and the
    # source changed under it.
    identity.values[identity.checkout] = [IMPORTED, MOVED]

    delivery.run()

    assert len(reexec.execed) == 1, "a moved checkout re-executes the subscriber"
    assert delivery.subscriber.registered, "the pane held its own registration"


def test_a_matching_stamp_leaves_the_subscriber_running(identity, reexec, delivery):
    """A checkout whose stamp matches the running image owes no replacement."""
    delivery.run()

    assert reexec.execed == [], "a matching stamp re-executes nothing"
    assert reexec.materialized == [], "and materialises no replacement image"


def test_an_untrusted_checkout_is_not_executed(
    identity, reexec, monkeypatch, delivery
) -> None:
    """The trust check is what keeps a moved-but-untrusted checkout out.

    A checkout that cannot prove it loads is deferred: the subscriber keeps the
    image it has rather than replacing itself with code nobody has proven.
    """
    monkeypatch.setenv(_FOLLOWER_SOURCE_ROOT_ENV, str(identity.checkout))
    monkeypatch.setattr(
        checkout_trust,
        "reckon_imports",
        lambda root: (False, "the checkout is not trusted"),
    )
    identity.values[identity.checkout] = [IMPORTED, MOVED]
    delivery.run()

    assert reexec.execed == [], "an untrusted checkout is never executed"
    assert reexec.materialized, "the moved checkout was still compared, and refused"


def test_a_reload_tree_compares_against_its_named_checkout(
    identity, reexec, monkeypatch, delivery
) -> None:
    """A follower running from a reload tree reads the checkout it came from.

    Its own image is the tree it imports from, and the carried commit names
    exactly that tree's identity. The comparison is made against the checkout
    named in the environment instead, which has moved: read from the import
    tree, the stamps would match and nothing would happen.
    """
    imported_root = _import_root()
    monkeypatch.setenv(_FOLLOWER_IMAGE_COMMIT_ENV, IMPORTED)
    monkeypatch.setenv(_FOLLOWER_SOURCE_ROOT_ENV, str(identity.checkout))
    identity.values[identity.checkout] = MOVED

    delivery.run()

    resolved_reads = [path.resolve() for path in identity.reads]
    assert identity.checkout.resolve() in resolved_reads, "the checkout was read"
    assert imported_root not in resolved_reads, "the import tree was not compared"
    assert [source for source, _commit in reexec.materialized] == [
        identity.checkout.resolve()
    ], "the replacement is materialised from the checkout, not the reload tree"
    assert len(reexec.execed) == 1


def test_identity_is_read_once_per_interval_over_a_hundred_sweeps(
    identity, reexec, monkeypatch, delivery
) -> None:
    """A hundred sweeps read the checkout's identity three times, not a hundred.

    The sweep that finds the code unchanged must cost no identity read: the
    reloader's cadence is what keeps a subscriber off the shared filesystem on
    every row. A reloader that is never polled reads nothing, and one polled
    into a fresh read every sweep reads a hundred times; neither can report
    three.
    """
    step = runs.FOLLOWER_FRESHNESS_SECONDS * 3 / SWEEPS
    moments = iter(index * step for index in range(SWEEPS))
    monkeypatch.setattr(
        follower_reload, "time", SimpleNamespace(monotonic=lambda: next(moments))
    )
    delivery.sweeps["count"] = SWEEPS
    delivery.probe[0] = lambda: identity.reads
    identity.reads.clear()

    delivery.run()

    assert reexec.execed == [], "a matching stamp never reloads"
    armed = delivery.sweeps["reads_at_arm"]
    swept = len(identity.reads) - armed
    assert armed == 1, "the delivery reads the identity once as it arms its image"
    assert swept == 3, (
        f"the sweeps read the checkout's identity {swept} times over {SWEEPS} "
        "sweeps spanning three intervals"
    )


def _import_root() -> Path:
    """The checkout this process imports ``reckon`` from."""
    return Path(runs.__file__).resolve().parents[2]
