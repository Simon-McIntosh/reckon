"""A malformed MCP request is refused from its arguments, not from storage.

A request whose only defect is an argument — a crew view outside the served
set, a crew field no row carries, a read_plan or roadmap view outside the
declared shapes, an op verb outside the edit vocabulary — depends on nothing
but the argument. Deciding it after the first file read makes the answer wait
on whatever storage is doing, and the wait is unbounded: any timeout the
storage imposes becomes the caller's timeout, and any deadline the tool sets
expires while the request is still being judged.

The stub below blocks on any open, so the request reaching storage is the
failure. On the revision before the argument checks moved ahead of the first
read, the crew live field call reached the stub and the test failed here.
"""

from __future__ import annotations

import builtins
import io

from reckon import mcp, mcp_edit_plan
from reckon.crew import query
from reckon.crew.recovery_classification import classify_pointer

#: A pointer shaped as the fleet writes one, used where a classification is
#: needed: its keys are what a served live row carries.
_FIXTURE_POINTER = {
    "run_id": "r-fixture",
    "node": {"time_budget": "30m"},
    "phase": "running",
    "plan": "a-plan",
    "section": "01-section",
    "session": "sess-fixture",
    "manifest_path": "/nonexistent/manifest.md",
}


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


def test_the_declared_vocabulary_matches_a_classified_pointer() -> None:
    """The declared set is the one a classification produces, field for field.

    The live view and the pre-flight read one declaration
    (:data:`reckon.crew.query.LIVE_FIELD_VOCABULARY`) and the classification is
    what a row actually carries, so the declaration is only right while the two
    agree. Comparing them catches a key the classification gained — which the
    view would serve and the pre-flight then refuse.
    """
    classified = classify_pointer(_FIXTURE_POINTER)
    derived = query._live_field_vocabulary([classified], with_mine=False)
    assert derived == set(query.LIVE_FIELD_VOCABULARY)


def test_a_live_read_of_a_served_field_is_not_refused() -> None:
    """The other direction: a well-formed request is served, not refused.

    A check that refused every request would pass every refusal test and be
    useless, so the accepted path is asserted beside the refused one —
    including ``section``, a field the row carries rather than one the
    classification invents.
    """
    served = ["run_id", "node", "plan", "section", "manifest_status"]
    assert (
        mcp._crew_argument_refusal(
            "live", fields=served, session=None, source="all", scope="project"
        )
        is None
    )
    rows = query.project_live_rows([_FIXTURE_POINTER], fields=served)
    assert rows and rows[0]["run_id"] == "r-fixture"


def test_a_view_named_in_another_case_is_not_refused() -> None:
    """A spelling the read serves is not refused from the argument ahead of it.

    The plan and roadmap reads match a view after stripping and lowercasing it,
    so an uppercased name reaches their own selector and is served; the
    pre-flight has to judge it the same way or it refuses a request the read
    would have answered.
    """
    assert (
        mcp._read_plan_tool(project="fixture", view="RAW").get("error")
        != "invalid_view"
    )
    assert (
        mcp._roadmap_tool(project="fixture", view="SuMMaRy").get("error")
        != "invalid_view"
    )


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


def test_a_crew_read_naming_an_unknown_scope_is_refused(monkeypatch) -> None:
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub,
        lambda: mcp._crew(project="fixture", view="runs", scope="no_such_scope"),
    )
    assert "no_such_scope" in result["detail"]


def test_a_crew_read_naming_an_unknown_runs_field_is_refused(monkeypatch) -> None:
    """The runs view's fields are judged by the runs view's own validator."""
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub,
        lambda: mcp._crew(project="fixture", view="runs", fields=["no_such_field"]),
    )
    assert "no_such_field" in result["detail"]


def test_a_crew_read_naming_an_unknown_view_is_refused(monkeypatch) -> None:
    stub = _stubbed(monkeypatch)
    result = _refuse(stub, lambda: mcp._crew(project="fixture", view="no_such_view"))
    assert result["error"] == "invalid_view"
    # The accepted views are named.
    assert "live" in result["detail"]
    assert "ledger" in result["detail"]


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
