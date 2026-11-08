"""A ``commits:`` line written with spaces names each commit, not one.

Two nova runs on 7 November wrote ``commits: 461641d1f d50a237c7 9d3d6ea4a``.
Read whole, that named one commit resolving to nothing, so the merge citing it
was refused while the promotion went through. A commit identifier never holds
whitespace, so such a line is split into its identifiers.
"""

from __future__ import annotations

import pytest

from reckon.crew.reports import parse_manifest


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "commits: 461641d1f d50a237c7 9d3d6ea4a",
            ["461641d1f", "d50a237c7", "9d3d6ea4a"],
        ),
        ("commits: a1b2c3d, e4f5a6b", ["a1b2c3d", "e4f5a6b"]),
        ("commits: [a1b2c3d e4f5a6b]", ["a1b2c3d", "e4f5a6b"]),
        ("commits: 1e5", ["1e5"]),
    ],
)
def test_each_commit_on_the_line_is_its_own_identifier(line: str, expected) -> None:
    assert parse_manifest(f"status: complete\n{line}\n")["commits"] == expected
