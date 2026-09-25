"""The crew MCP view exposes the derived obligations without reshaping them.

``_crew`` is a thin dispatcher, so the obligation view must hand back the
caller the same items :func:`reckon.crew.obligations.obligations` derives —
same kinds, ages and next commands — rather than a copy that can drift from the
function directly.

The derivation reads live run pointers and classifies each through
:func:`reckon.crew.recovery.classify_pointer`, so a fixture that stubs the
recovery sweep measures nothing: the stub never enters the read. The fixture
below therefore seeds real pointers, manifests and run directories into a
synthetic configuration home and lets the derivation run against them.

Run one file with the repo interpreter from the repo root::

    .venv/bin/python -m pytest tests/test_mcp_crew_obligations.py -q
"""

from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

import reckon.mcp as mcp_module
from reckon.crew import runs

# The census module owns the fixture vocabulary and the seeding helpers for one
# synthetic fleet; reusing them keeps the two fixtures describing the same
# shapes rather than a second, drifting copy.
census = importlib.import_module("tests.test_crew_obligations")

obligations_module = mcp_module.obligations_module

PROJECT = census.PROJECT
SESSION = census.SESSION

# The two duties the fixture seeds, oldest first: the ready one ages past the
# missing one so the derivation's oldest-first order is a fact of the seed.
REVIEW_MISSING = "run-view-review-missing"
REVIEW_READY = "run-view-review-ready"


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic config home with one mounted project, and an empty default.

    ``Path.home`` is pointed at a temporary directory holding a single sentinel
    under ``.config/reckon``. The home-resolved configuration path is what a
    code path would write to if it ignored the ``RECKON_HOME`` override, so the
    sentinel makes that leak visible without depending on the live fleet's own
    activity in the real home.
    """
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    default_home = tmp_path / "home"
    (default_home / ".config" / "reckon").mkdir(parents=True)
    (default_home / ".config" / "reckon" / "sentinel.json").write_text(
        "{}\n", encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: default_home))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed obligation view fixture"),
    ):
        census._git(repo, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _seed_completed_run(
    repository: Path,
    config_home: Path,
    run_id: str,
    *,
    age_seconds: int,
    node: str,
    reviewed: bool,
) -> None:
    """Seed one dead run whose manifest reports completion.

    The manifest's mtime is the age the classifier measures, so it is stamped
    ``age_seconds`` behind now rather than left as the write instant. A reviewed
    run also gets a stored review of its head, which is what separates a
    promotable run from a scoring one.
    """
    head = census._git(repository, "rev-parse", "HEAD")
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    (config_home / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    census._write_pointer(run_id, node=node)
    pointer = runs.read_pointer(run_id)
    pointer.update(
        {
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "manifest_path": str(manifest),
        }
    )
    runs._write_json(runs.pointer_path(run_id), pointer)
    stamp = time.time() - age_seconds
    os.utime(manifest, (stamp, stamp))
    if reviewed:
        census._store_complete_review(run_id, base=head, head=head)


@pytest.fixture()
def derived(config_home: Path) -> dict[str, Any]:
    """Seed a review-missing and a review-ready run and derive their duties."""
    home = config_home.parent / "config"
    _seed_completed_run(
        config_home,
        home,
        REVIEW_MISSING,
        age_seconds=120,
        node="view-review-missing",
        reviewed=False,
    )
    _seed_completed_run(
        config_home,
        home,
        REVIEW_READY,
        age_seconds=240,
        node="view-review-ready",
        reviewed=True,
    )
    return obligations_module.obligations(PROJECT, SESSION)


def test_obligations_view_returns_the_derived_items_unchanged(
    config_home: Path, derived: dict[str, Any]
) -> None:
    function_kinds = [item["kind"] for item in derived["obligations"]]
    assert function_kinds, "the derivation returned no duties seeded for it"
    assert function_kinds == ["review-ready", "review-missing"]
    assert {item["run_id"] for item in derived["obligations"]} == {
        REVIEW_READY,
        REVIEW_MISSING,
    }

    result = mcp_module._crew(PROJECT, view="obligations", session=SESSION)

    assert result["ok"] is True
    assert result["view"] == "obligations"
    assert result["project"] == PROJECT
    assert result["session"] == SESSION
    assert "obligations" in result, (
        "the obligations view did not carry the derived payload"
    )
    assert result["obligations"] == derived["obligations"]
    assert result["summary"] == derived["summary"]

    default_root = Path.home() / ".config" / "reckon"
    assert set(census._file_mtimes(default_root)) == {
        str(default_root / "sentinel.json")
    }, "the read wrote to the home-resolved configuration path"


def test_obligations_view_refuses_a_missing_session(config_home: Path) -> None:
    result = mcp_module._crew(PROJECT, view="obligations")

    assert result["ok"] is False
    assert result["error"] == "missing_session"
    assert result["project"] == PROJECT
