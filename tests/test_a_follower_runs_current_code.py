"""A station subscriber re-executes onto its source checkout's code.

A subscriber to a station renders every row with its own code: the station
commits rows, and ``station_subscriber`` composes the sentences the pane prints.
So a long-lived subscriber keeps the wording of the image it was armed with,
including a remedy the source has since removed, unless it replaces its own
image when the checkout moves. These cases pin that path: the reloader the
subscriber polls, the stamp it compares against, the trust check it refuses on,
and the cadence that keeps the identity read off every sweep.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import checkout_trust
from reckon.crew import follower_reload, runs
from reckon.crew_follow_commands import (
    _FOLLOWER_IMAGE_COMMIT_ENV,
    _FOLLOWER_SOURCE_ROOT_ENV,
    _follower_reloader_for,
)

# Two identities a checkout can report. ``IMPORTED`` is what the tree this
# process imports from answers, and stands in for the image a follower is
# already running; ``MOVED`` is a source checkout that has moved on.
IMPORTED = "a" * 40
MOVED = "b" * 40


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
    values: dict[Path, str] = {}

    def read(root):
        root = Path(root)
        reads.append(root)
        return values.get(root, IMPORTED)

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


class _Registration:
    """Stands in for the follower's delivery registration across a reload."""

    def __init__(self) -> None:
        self.prepared = 0
        self.cancelled = 0

    def prepare_reexec(self) -> None:
        self.prepared += 1

    def cancel_reexec(self) -> None:
        self.cancelled += 1


def _import_root() -> Path:
    """The checkout this process imports ``reckon`` from."""
    return Path(runs.__file__).resolve().parents[2]


def _subscriber_reloader(registration=None):
    """The reloader the station-subscriber arm of the follower polls.

    Built through the same call the subscriber's delivery makes, so a delivery
    that stopped building one fails here rather than only in production.
    """
    return _follower_reloader_for("proj", registration or _Registration())


def test_a_moved_checkout_re_executes_the_subscriber(
    identity, reexec, monkeypatch
) -> None:
    """The identity the subscriber reads is its checkout's, and it acts on it.

    The follower runs the image it was armed with (``IMPORTED``) while the
    checkout named in the carried environment has moved to ``MOVED``. A
    subscriber that never reloads keeps printing the old image's wording, so
    the reload is what makes its rows current.
    """
    monkeypatch.setenv(_FOLLOWER_SOURCE_ROOT_ENV, str(identity.checkout))
    registration = _Registration()
    reloader = _subscriber_reloader(registration)
    assert reloader is not None, "the subscriber's delivery builds a reloader"

    # The checkout moves after the pane is armed, which is the whole situation:
    # the subscriber is long-lived and the source changed under it.
    identity.values[identity.checkout] = MOVED
    reloader.poll({"reported": {}, "offset": 12})

    assert len(reexec.execed) == 1, "a moved checkout re-executes the subscriber"
    assert registration.prepared == 1
    assert registration.cancelled == 1, "a refused exec hands the registration back"


def test_a_matching_stamp_leaves_the_subscriber_running(identity, reexec) -> None:
    """A checkout whose stamp matches the running image owes no replacement."""
    reloader = _subscriber_reloader()
    reloader.poll({"reported": {}, "offset": 12})

    assert reexec.execed == [], "a matching stamp re-executes nothing"
    assert reexec.materialized == [], "and materialises no replacement image"


def test_an_untrusted_checkout_is_not_executed(identity, reexec, monkeypatch) -> None:
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
    reloader = _subscriber_reloader()

    identity.values[identity.checkout] = MOVED
    reloader.poll({"reported": {}, "offset": 12})

    assert reexec.execed == [], "an untrusted checkout is never executed"
    assert reexec.materialized, "the moved checkout was still compared, and refused"


def test_a_reload_tree_compares_against_its_named_checkout(
    identity, reexec, monkeypatch
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

    reloader = _subscriber_reloader()
    assert reloader.source_root == identity.checkout.resolve()

    reloader.poll({"reported": {}, "offset": 12})

    resolved_reads = [path.resolve() for path in identity.reads]
    assert identity.checkout.resolve() in resolved_reads, "the checkout was read"
    assert imported_root not in resolved_reads, "the import tree was not compared"
    assert [source for source, _commit in reexec.materialized] == [
        identity.checkout.resolve()
    ], "the replacement is materialised from the checkout, not the reload tree"
    assert len(reexec.execed) == 1


def test_identity_is_read_once_per_interval_over_a_hundred_sweeps(
    identity, reexec, monkeypatch
) -> None:
    """A hundred sweeps read the checkout's identity three times, not a hundred.

    The sweep that finds the code unchanged must cost no identity read: the
    reloader's cadence is what keeps a subscriber off the shared filesystem on
    every row. A reloader that is never polled reads nothing, and one polled
    into a fresh read every sweep reads a hundred times; neither can report
    three.
    """
    sweeps = 100
    step = runs.FOLLOWER_FRESHNESS_SECONDS * 3 / sweeps
    clock = SimpleNamespace(monotonic=lambda: next(moments))
    moments = iter(index * step for index in range(sweeps))

    reloader = _subscriber_reloader()
    monkeypatch.setattr(follower_reload, "time", clock)
    identity.reads.clear()

    for _ in range(sweeps):
        reloader.poll({"reported": {}, "offset": 12})

    assert reexec.execed == [], "a matching stamp never reloads"
    assert len(identity.reads) == 3, (
        f"the checkout's identity was read {len(identity.reads)} times over "
        f"{sweeps} sweeps spanning three intervals"
    )
