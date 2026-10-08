"""A warm pick never re-walks the docs tree to resolve the plan a node names.

A pick stamps its plan-path resolve by the directories a docs scan reads, so a
plan whose file has not moved is served from the cache instead of walking the
tree again.

Two properties hold that stamp together. The scan helper must return the
docs-relative directory paths the stamp iterates; a shape that hands the stamp
something other than a path string makes it raise while stamping, so the resolve
is abandoned and every pick -- warm or cold -- pays a fresh docs scan. And a
second ask for an unchanged tree must resolve from the cache without walking at
all. The first test pins the shape the stamp consumes and the second the warm
resolve it protects; both fail when the scan helper moves under a stamp that was
not updated with it.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from reckon import resources
from reckon.crew import routing

picker = importlib.import_module("reckon.crew.picker")

#: A minimal plan whose slug resolves through a docs-tree scan.
PLAN_HTML = (
    "<!doctype html>\n<html><head>\n"
    '<meta name="docs-project" content="proj">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="probe">\n'
    '<meta name="plan-effort-hours" content="48">\n'
    "</head><body>\n"
    '<h2 id="s2">&sect;2 &mdash; A deep section</h2>\n'
    "</body></html>\n"
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the pick-input cache at the test's temp tree."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "pick-cache"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    plans = tmp_path / "repo" / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "probe.html").write_text(PLAN_HTML)
    return tmp_path / "repo"


def test_the_scan_helper_returns_relative_directory_paths(repo: Path) -> None:
    """The scan helper hands the pick's stamp docs-relative directory paths.

    The pick's stamp iterates the helper's result and joins each entry onto the
    docs root, so an entry that is not a path string makes the stamp raise and
    the resolve fall back to a fresh scan on every pick.
    """

    docs = repo / "docs"
    directories = routing._docs_scan_directories(docs)

    assert directories, "the scan reports the directories it walked"
    assert all(isinstance(entry, str) for entry in directories), directories
    assert all((docs / entry).is_dir() for entry in directories), directories


def test_a_warm_resolve_walks_no_docs_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second resolve of an unchanged tree reads no plan through a scan."""

    calls = {"n": 0}
    real = resources.resolve_resource

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(resources, "resolve_resource", counting)

    docs = repo / "docs"
    first = picker._resolved_plan_path(docs, "proj", "probe")
    assert first is not None, "the first resolve found the plan it names"
    assert calls["n"] >= 1, "the first resolve walked the docs tree"

    calls["n"] = 0
    second = picker._resolved_plan_path(docs, "proj", "probe")
    assert second == first
    assert calls["n"] == 0, "a warm resolve re-resolved the plan through a scan"
