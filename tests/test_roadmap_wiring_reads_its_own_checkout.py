"""A roadmap judges a tree's plans against that same tree's declarations.

Every production caller of ``build_roadmap`` inventories one checkout, while the
wiring scan can read a plan's standalone declaration from another: the project's
registered mount. These arms drive each caller with a second checkout whose plan
declares standalone while the mount's copy of the same plan does not, and
require the wiring finding to follow the tree the rows were inventoried from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import flight as flight_module
from reckon import mcp as mcp_module
from reckon import roadmap as roadmap_module
from reckon import serve

PROJECT = "wiring-reads-its-own-checkout"
SLUG = "alone"
HANDLE = "release"
ENFORCED_FROM = "2026-09-19"
STANDALONE = "One-file fix: it feeds nothing and waits on nothing."


def _write_plan(docs_dir: Path, *, standalone: str | None = None) -> Path:
    """Write the plan into ``docs_dir/plans`` in the store's own layout."""

    path = docs_dir / "plans" / f"{SLUG}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    metas = [
        ("docs-project", PROJECT),
        ("reckon-type", "plan"),
        ("plan-slug", SLUG),
        ("plan-status", "active"),
        ("plan-modified", ENFORCED_FROM),
        ("plan-graph-handle", HANDLE),
    ]
    if standalone is not None:
        metas.append(("plan-standalone", standalone))
    head = "".join(f'<meta name="{name}" content="{value}">' for name, value in metas)
    path.write_text(
        "<!doctype html><html><head>"
        f"{head}<title>{SLUG}</title></head>"
        '<body><main class="plan-doc"></main></body></html>',
        encoding="utf-8",
    )
    return path


def _row() -> dict:
    """One inventory row for the plan, as a caller would hold it."""

    return {
        "slug": SLUG,
        "type": "plan",
        "title": SLUG,
        "status": "active",
        "modified": ENFORCED_FROM,
        "impl": 0.0,
        "depends_on": [],
        "blocks": [],
        "informs": [],
        "gates": [],
        "graph_handle": HANDLE,
        "sprint": None,
        "decisions": [],
    }


def _wiring(report: dict) -> list[dict]:
    return [row for row in report["wiring_findings"] if row["code"] == "unwired-plan"]


@pytest.fixture(autouse=True)
def isolated_reckon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Resolve every config path inside the temporary tree, never the real one."""

    home = tmp_path / "reckon-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(home / "state"))
    serve._DISC_CACHE.clear()
    yield
    serve._DISC_CACHE.clear()


@pytest.fixture
def two_trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A registered mount and a second checkout that disagree about standalone.

    Returns ``(mount_docs, checkout_root, checkout_docs)``. Both trees hold the
    same plan; only the second checkout declares it standalone.
    """

    mount_docs = tmp_path / "registered"
    checkout_root = tmp_path / "worktree"
    checkout_docs = checkout_root / "docs"
    _write_plan(mount_docs)
    _write_plan(checkout_docs, standalone=STANDALONE)
    (checkout_docs / "state").mkdir(parents=True, exist_ok=True)
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(mount_docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    return mount_docs, checkout_root, checkout_docs


class _RoadmapCalls:
    """Record the roadmap a caller builds, delegating to the real builder.

    Two callers do not hand the roadmap back — the HTTP discovery projection and
    the graph resolver consume it — so the only way to read what the wiring scan
    decided for them is to observe the call itself.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.reports: list[dict] = []
        real = roadmap_module.build_roadmap

        def recording(*args, **kwargs):
            report = real(*args, **kwargs)
            self.reports.append(report)
            return report

        monkeypatch.setattr(roadmap_module, "build_roadmap", recording)
        monkeypatch.setattr(mcp_module, "build_roadmap", recording)

    def only(self) -> dict:
        assert len(self.reports) == 1, (
            f"expected exactly one roadmap from this caller, saw {len(self.reports)}"
        )
        return self.reports[0]

    def reset(self) -> None:
        self.reports.clear()


def test_a_worktree_roadmap_reads_that_worktrees_declaration(
    two_trees, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount_docs, checkout_root, checkout_docs = two_trees
    calls = _RoadmapCalls(monkeypatch)

    # Positive control: the mount's copy carries no declaration, so a roadmap
    # scoped to the mount does report the plan. Without this the arms below
    # would pass on an instrument that sees no plan at all.
    roadmap_module.build_roadmap(PROJECT, [_row()], [], docs_dir=mount_docs)
    (mounted_finding,) = _wiring(calls.only())
    assert mounted_finding["slug"] == SLUG
    calls.reset()

    # 1. serve — the HTTP discovery projection.
    serve._DISC_CACHE.clear()
    serve.discover_plans(checkout_docs, PROJECT, tmp_path / "state")
    assert _wiring(calls.only()) == []
    calls.reset()

    # 2. flight — the portfolio projection, which returns the roadmap.
    portfolio = flight_module._project_roadmap(PROJECT, checkout_docs)
    assert _wiring(portfolio) == []
    calls.reset()

    # 3. mcp roadmap — the lossless report.
    report = mcp_module._roadmap(PROJECT, checkout_path=str(checkout_root))
    assert _wiring(report) == []
    calls.reset()

    # 4. mcp audit — the findings the audit returns.
    audit = mcp_module._audit(PROJECT, checkout_path=str(checkout_root))
    assert [row for row in audit["findings"] if row["code"] == "unwired-plan"] == []
    calls.reset()

    # 5. the graph resolver — the roadmap it builds to schedule members.
    roadmap_module.resolve_graph_target(
        HANDLE,
        {PROJECT: {"inventory": [_row()], "docs_dir": checkout_docs}},
    )
    assert _wiring(calls.only()) == []
