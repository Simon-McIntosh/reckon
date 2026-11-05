"""A read or write of one plan never composes the whole project.

The north-star diagnostic on a plan read needs two things from the project: the
north-star collection it is checked against, and nothing else. It used to take
them by composing the project state, which reads every sprint, milestone and
blocker resource, so a read about one plan paid the whole project's cost.

The diagnostic now reads the project's declaring record — the project resource
in a distributed store, the legacy index in a legacy one — through
``project_state.read_project_manifest``. These cases count
``compose_project_state`` through a patch while one plan is read and written,
in both storage layouts, and check that the diagnostic is unchanged.

The count is taken through the MCP read of one plan (``view="summary"``) and
through an ``edit_plan`` ``set``, because those are the entry points the plan
names. The count patch replaces the composed function, so a memo inside it
would not hide a call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import file_memo, project_state
from reckon._plan_html import write_state
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "sample"
DECLARED = "declared-direction"
MISSING = "missing-direction"

_BARE_PLAN = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    f'<meta name="docs-project" content="{PROJECT}">'
    "<title>plan</title></head>"
    '<body><main class="plan-doc"></main></body></html>'
)

_NORTH_STARS = [
    {
        "id": DECLARED,
        "name": "Declared direction",
        "statement": "Every accepted change is reproducible and observable.",
    }
]


def _plan_document(slug: str, north_star: str | None) -> str:
    state: dict[str, object] = {
        "slug": slug,
        "title": slug,
        "status": "active",
        "type": "plan",
        "version": 0,
    }
    if north_star is not None:
        state["north_star"] = north_star
    return write_state(
        _BARE_PLAN.replace("<title>plan</title>", f"<title>{slug}</title>"), state
    )


def _write_legacy_project(root: Path) -> Path:
    docs = root / "docs"
    plans = docs / "plans"
    state = docs / "state" / PROJECT
    plans.mkdir(parents=True)
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "doc": "index",
                "data": {
                    "_version": 0,
                    "north_stars": _NORTH_STARS,
                    "projects": [{"project": PROJECT}],
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _write_distributed_project(root: Path) -> Path:
    docs = root / "docs"
    plans = docs / "plans"
    state = docs / "state" / PROJECT
    plans.mkdir(parents=True)
    state.mkdir(parents=True)
    marker = docs / ".reckon" / "project-state-migration.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "format": "distributed",
                "status": "complete",
                "project": PROJECT,
                "resources": [{"type": "project", "id": "project", "version": 0}],
            }
        ),
        encoding="utf-8",
    )
    (state / "project.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "doc": "project",
                "data": {
                    "project": PROJECT,
                    "id": "project",
                    "type": "project",
                    "version": 0,
                    "north_stars": _NORTH_STARS,
                },
            }
        ),
        encoding="utf-8",
    )
    return root


_BUILDERS = {
    "legacy": _write_legacy_project,
    "distributed": _write_distributed_project,
}


def _build_project(root: Path, storage: str) -> Path:
    """Lay out one project in the requested storage layout with two plans."""

    built = _BUILDERS[storage](root)
    plans = built / "docs" / "plans"
    (plans / "declared.html").write_text(
        _plan_document("declared", DECLARED), encoding="utf-8"
    )
    (plans / "undeclared.html").write_text(
        _plan_document("undeclared", MISSING), encoding="utf-8"
    )
    (plans / "unoriented.html").write_text(
        _plan_document("unoriented", None), encoding="utf-8"
    )
    return built


def _read_inventory(root: Path, storage: str) -> None:
    """Run the project inventory read the summary view performs on its own."""

    from reckon import mcp_views

    mcp_views.index_discovery(root / "docs", PROJECT, None)


class _ComposeCounter:
    """Count every call to ``project_state.compose_project_state``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        self._real = project_state.compose_project_state

        def counting(docs_dir: Path, project: str) -> dict:
            self.count += 1
            return self._real(docs_dir, project)

        monkeypatch.setattr(project_state, "compose_project_state", counting)

    def reset(self) -> None:
        self.count = 0


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch):
    """Point the store at the fixture tree and keep the write path unguarded."""

    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    monkeypatch.delenv("RECKON_MANIFEST", raising=False)
    file_memo.clear()
    reload_mcp_family()
    yield
    file_memo.clear()
    reload_mcp_family()


@pytest.mark.parametrize("storage", sorted(_BUILDERS))
def test_one_plan_read_and_write_compose_no_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated, storage: str
) -> None:
    root = _build_project(tmp_path / storage, storage)
    from reckon import mcp
    from reckon import mcp_edit_plan as edit_plan

    counter = _ComposeCounter(monkeypatch)

    file_memo.clear()
    counter.reset()
    payload = mcp._read_plan(
        PROJECT, "declared", view="summary", checkout_path=str(root)
    )
    assert payload.get("ok", True) is not False, payload
    summary_count = counter.count

    # The summary view also assembles the project's inventory — its sprint,
    # milestone and blocker lists — and a distributed store composes the
    # project to answer that list. That composition belongs to the inventory
    # reader, not to the plan: what this case requires is that the plan's own
    # read adds none on top of it.
    file_memo.clear()
    counter.reset()
    _read_inventory(root, storage)
    inventory_count = counter.count

    assert summary_count == inventory_count, (
        f"read_plan(view='summary') composed the project {summary_count} time(s) "
        f"against the inventory reader's {inventory_count} on a {storage} store"
    )
    if storage == "legacy":
        assert summary_count == 0, (
            f"read_plan(view='summary') composed the project {summary_count} "
            "time(s) on a legacy store"
        )

    file_memo.clear()
    counter.reset()
    result = edit_plan._edit_plan(
        PROJECT,
        "declared",
        [{"op": "set", "path": "title", "value": "Renamed"}],
        expected_version=0,
        checkout_path=str(root),
    )
    assert result.get("ok") is True, result
    assert counter.count == 0, (
        f"an edit_plan set composed the project {counter.count} time(s) "
        f"on a {storage} store"
    )


@pytest.mark.parametrize("storage", sorted(_BUILDERS))
def test_an_undeclared_north_star_still_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated, storage: str
) -> None:
    root = _build_project(tmp_path / storage, storage)
    from reckon import mcp

    payload = mcp._read_plan(PROJECT, "undeclared", checkout_path=str(root))
    diagnostics = _diagnostics(payload)

    assert diagnostics == [
        {
            "code": "undeclared-north-star",
            "severity": "warning",
            "message": (
                f"plan north-star {MISSING!r} is not declared by project {PROJECT!r}"
            ),
        }
    ]


@pytest.mark.parametrize("storage", sorted(_BUILDERS))
def test_a_declared_north_star_carries_no_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated, storage: str
) -> None:
    root = _build_project(tmp_path / storage, storage)
    from reckon import mcp

    payload = mcp._read_plan(PROJECT, "declared", checkout_path=str(root))

    assert _diagnostics(payload) == []


def _diagnostics(payload: dict) -> list[dict]:
    """Return the north-star diagnostics a summary read carries."""

    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    return [
        diagnostic
        for diagnostic in data.get("validation_diagnostics") or []
        if diagnostic.get("code") == "undeclared-north-star"
    ]
