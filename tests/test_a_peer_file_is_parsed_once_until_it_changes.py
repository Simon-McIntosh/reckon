"""A launch reads each peer's Python files once until a file changes.

Adjacency compares a dispatching node with every live peer by parsing the
Python files in both scopes. The parse was repeated for every peer pair of every
launch, and on 14 November the admit service spent a sampled minute entirely in
it and seventeen minutes on one admission pass, while queued work waited.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from reckon.crew import dispatch_peer


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "left.py").write_text("import pkg.right\n", encoding="utf-8")
    (root / "pkg" / "right.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pkg" / "other.py").write_text("VALUE = 2\n", encoding="utf-8")
    getattr(dispatch_peer, "_REFERENCES", {}).clear()
    return root.resolve()


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = ast.parse

    def counting(source, filename="<unknown>", *args, **kwargs):
        seen.append(str(filename))
        return real(source, filename, *args, **kwargs)

    monkeypatch.setattr(dispatch_peer.ast, "parse", counting)
    return seen


def test_repeated_adjacency_parses_each_file_once(repo, parses):
    for _ in range(5):
        assert dispatch_peer._nodes_are_adjacent(
            ["pkg/left.py"], ["pkg/right.py"], repo
        )
        assert not dispatch_peer._nodes_are_adjacent(
            ["pkg/other.py"], ["pkg/right.py"], repo
        )
    assert sorted(set(parses)) == sorted(parses)


def test_a_changed_file_is_parsed_again(repo, parses):
    left = repo / "pkg" / "left.py"
    assert dispatch_peer._nodes_are_adjacent(["pkg/left.py"], ["pkg/right.py"], repo)
    left.write_text("import pkg.other\n", encoding="utf-8")
    stat = left.stat()
    os.utime(left, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert not dispatch_peer._nodes_are_adjacent(
        ["pkg/left.py"], ["pkg/right.py"], repo
    )
    assert dispatch_peer._nodes_are_adjacent(["pkg/left.py"], ["pkg/other.py"], repo)
    assert parses.count(str(left)) == 2
