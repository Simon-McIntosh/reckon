"""One reader for the plan slug a sprint item names.

A sprint item reaches the readers in four places — the store, the roadmap, the
document checker and the MCP discovery surface — and each once read the slug
its own way. They now share ``reckon._store.sprint_item_slug``; these tests fix
the behaviour that shape must keep and assert the consumers use the one helper
rather than a private copy.
"""

from reckon import doccheck, mcp_discovery, roadmap
from reckon._store import sprint_item_slug


def test_reads_a_bare_slug_string():
    assert sprint_item_slug("a-b") == "a-b"


def test_reads_a_slug_mapping():
    assert sprint_item_slug({"slug": "a-b"}) == "a-b"


def test_reads_a_path_mapping():
    assert sprint_item_slug({"path": "a-b"}) == "a-b"


def test_slug_wins_over_path():
    assert sprint_item_slug({"slug": "first", "path": "second"}) == "first"


def test_strips_surrounding_whitespace():
    assert sprint_item_slug("  a-b  ") == "a-b"
    assert sprint_item_slug({"slug": "  a-b  "}) == "a-b"


def test_blank_slug_falls_through_to_path():
    assert sprint_item_slug({"slug": "   ", "path": "a-b"}) == "a-b"


def test_non_mapping_and_missing_slug_are_empty():
    assert sprint_item_slug(None) == ""
    assert sprint_item_slug(42) == ""
    assert sprint_item_slug({}) == ""
    assert sprint_item_slug({"slug": ""}) == ""


def test_consumers_share_the_one_reader():
    assert doccheck.sprint_item_slug is sprint_item_slug
    assert roadmap.sprint_item_slug is sprint_item_slug
    assert mcp_discovery._sprint_item_slug is sprint_item_slug
