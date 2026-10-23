"""A malformed MCP request is refused from its arguments, not from storage.

A request whose only defect is an argument — a crew view outside the served
set, a crew field no row carries, a read_plan or roadmap view outside the
declared shapes, an op verb outside the edit vocabulary — depends on nothing
but the argument. Refusing it after the first file read makes the answer wait
on whatever storage is doing: on 2026-10-23 a ``crew view live`` call naming an
unknown field waited over 120 s before it was refused.

The stub below blocks on any open, so the request reaching storage is the
failure. On the revision before the argument checks moved ahead of the first
read, the crew live field call reached the stub and the test failed here.
"""

from __future__ import annotations

import builtins
import io

from reckon import mcp, mcp_edit_plan
from reckon.crew import query


class _StorageStub:
    """Any open raises, recording that a request reached storage."""

    def __init__(self) -> None:
        self.reached = False
        self.paths: list[str] = []

    def __call__(self, file: object, *args: object, **kwargs: object) -> object:
        self.reached = True
        self.paths.append(str(file))
        raise AssertionError(
            f"a malformed request reached storage before it was refused: {file!r}"
        )


def _refuse(stub: _StorageStub, call) -> dict:
    """Run ``call`` under the stub's block, which must never be triggered."""
    result = call()
    assert not stub.reached, f"storage was opened: {stub.paths}"
    assert result.get("ok") is False, result
    return result


def _stubbed(monkeypatch) -> _StorageStub:
    stub = _StorageStub()
    monkeypatch.setattr(io, "open", stub)
    monkeypatch.setattr(builtins, "open", stub)
    return stub


def test_the_refusal_vocabulary_covers_every_live_field() -> None:
    """The pre-flight set names no fewer fields than the view serves.

    The view builds its own vocabulary from the classification, so a field the
    classification carries but the pre-flight set omits is refused before
    storage while the view would have served it. Comparing the two sets is what
    catches an omission the per-call tests cannot see.
    """
    derived = query._live_field_vocabulary([], with_mine=True)
    declared = set(mcp._LIVE_ROW_FIELDS) | {"mine"}
    assert derived <= declared, sorted(derived - declared)


def test_a_crew_live_read_naming_an_unknown_field_is_refused(monkeypatch) -> None:
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub,
        lambda: mcp._crew(project="fixture", view="live", fields=["dispatched_at"]),
    )
    detail = result["detail"]
    # The refusal names what was refused and the accepted values beside it.
    assert "dispatched_at" in detail
    assert "run_id" in detail


def test_a_crew_read_naming_an_unknown_view_is_refused(monkeypatch) -> None:
    stub = _stubbed(monkeypatch)
    result = _refuse(stub, lambda: mcp._crew(project="fixture", view="no_such_view"))
    assert result["error"] == "invalid_view"
    # The accepted views are named.
    assert "live" in result["detail"]
    assert "ledger" in result["detail"]


def test_a_crew_read_naming_an_unknown_scope_is_refused(monkeypatch) -> None:
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub,
        lambda: mcp._crew(project="fixture", view="runs", scope="no_such_scope"),
    )
    assert "no_such_scope" in result["detail"]


def test_an_edit_naming_an_unknown_op_is_refused(monkeypatch) -> None:
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub,
        lambda: mcp_edit_plan._edit_plan_tool(
            project="fixture",
            slug="a-plan",
            expected_version=1,
            ops=[{"op": "no_such_verb", "path": "title"}],
        ),
    )
    assert result["error"] == "op_error"
    assert "no_such_verb" in result["detail"]
    # The accepted verbs are named.
    assert "set" in result["detail"]


def test_a_plan_read_naming_an_unknown_view_is_refused(monkeypatch) -> None:
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub, lambda: mcp._read_plan_tool(project="fixture", view="no_such_view")
    )
    assert "no_such_view" in result["detail"]
    assert "summary" in result["detail"]


def test_a_roadmap_naming_an_unknown_view_is_refused(monkeypatch) -> None:
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub,
        lambda: mcp._roadmap_tool(project="fixture", view="no_such_view"),
    )
    assert "no_such_view" in result["detail"]
    assert "summary" in result["detail"]
