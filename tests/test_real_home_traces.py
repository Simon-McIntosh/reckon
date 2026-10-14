"""The shared trace search reports only what a test's own needles name.

These cases exercise ``tests/real_home_traces.py`` directly, against a
temporary root, so they show the two readings the migrated guards rest on: a
foreign entry added between the readings is not reported, and an entry carrying
one of the test's needles -- by name or by content -- is. Without the first, a
guard built on this search could pass because it is blind rather than because
the test wrote nothing.
"""

from __future__ import annotations

from pathlib import Path

from tests.real_home_traces import traces_naming

# Identifiers this suite mints and no peer would. Distinctive on purpose, so a
# hit in a real run can only have come from the test that minted one.
NEEDLES = ("r-own-9f3a", "node-own-9f3a")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    for subtree in ("crew/live", "crew/watch"):
        (root / subtree).mkdir(parents=True)
    return root


def test_a_foreign_entry_added_between_the_readings_is_not_reported(
    tmp_path: Path,
) -> None:
    """A peer's own file moves the tree but names the peer, so it is no hit."""
    root = _root(tmp_path)
    subtrees = ("crew/live", "crew/watch")

    before = traces_naming(root, subtrees, NEEDLES)
    # A peer session files its pointer and its registration while this test runs.
    (root / "crew/live" / "r-peer-0001.json").write_text('{"session": "session-peer"}')
    (root / "crew/watch" / "session-peer.lock").write_text("peer")
    after = traces_naming(root, subtrees, NEEDLES)

    assert before == after == []


def test_an_entry_named_for_the_test_is_reported(tmp_path: Path) -> None:
    """A file whose own name carries a needle is returned, whatever its body."""
    root = _root(tmp_path)
    (root / "crew/live" / f"{NEEDLES[0]}.json").write_text("{}")

    hits = traces_naming(root, ("crew/live",), NEEDLES)

    assert hits, "a file named for the test's own run id went unreported"
    assert NEEDLES[0] in hits[0]


def test_an_entry_carrying_the_test_needle_is_reported(tmp_path: Path) -> None:
    """A file whose text carries a needle is returned, whatever its name."""
    root = _root(tmp_path)
    (root / "crew/watch" / "anon.lock").write_text(f'{{"session": "{NEEDLES[1]}"}}')

    hits = traces_naming(root, ("crew/watch",), NEEDLES)

    assert hits, "a file carrying the test's own session went unreported"
    assert NEEDLES[1] in Path(hits[0]).read_text()


def test_a_subtree_naming_one_file_is_searched_at_the_root(tmp_path: Path) -> None:
    """A subtree may name a single file, so a guard can point at that file.

    Some writers touch one file directly under the home rather than a directory
    tree -- the budget-lift store keeps its single ``budget-lifts.json`` at the
    home's root -- so a guard against such a leak searches that file instead of
    walking every directory the home holds. The file is a hit only when its own
    name or its text carries a needle.
    """
    root = tmp_path / "home"
    root.mkdir()
    target = root / "budget-lifts.json"
    target.write_text(f'{{"group": "{NEEDLES[0]}"}}')

    assert traces_naming(root, (target.name,), NEEDLES) == [str(target)]

    target.write_text('{"group": "some-peer"}')
    assert traces_naming(root, (target.name,), NEEDLES) == []
