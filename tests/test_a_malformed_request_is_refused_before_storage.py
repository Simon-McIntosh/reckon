"""A malformed MCP request is refused from its arguments, not from storage.

A request whose only defect is an argument — a crew view outside the served
set, a crew field no row carries, a read_plan or roadmap view outside the
declared shapes, an op verb outside the edit vocabulary — depends on nothing
but the argument. Deciding it after the first file read makes the answer wait
on whatever storage is doing, and the wait is unbounded: any timeout the
storage imposes becomes the caller's timeout, and any deadline the tool sets
expires while the request is still being judged.

The stub below blocks on any open, so the request reaching storage is the
failure. Every refusal path is exercised under it, so a pre-flight that
resolved a project's storage before judging its arguments fails here rather
than passing quietly.
"""

from __future__ import annotations

import builtins
import io

from reckon import mcp, mcp_edit_plan, mcp_views
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
    """Every storage access raises, recording that a request reached storage.

    Covers both forms the reads reach storage through: opening a file, and
    resolving a project's docs dir, which stats the mounts file rather than
    opening one. A probe that covered only ``open`` would let a read that
    stats its way to its first answer look untouched by storage.
    """

    def __init__(self) -> None:
        self.reached = False
        self.paths: list[str] = []

    def _record(self, what: str) -> None:
        self.reached = True
        self.paths.append(what)
        raise AssertionError(f"storage was reached: {what}")

    def __call__(self, file: object, *args: object, **kwargs: object) -> object:
        self._record(repr(file))

    def docs_dir_for(self, project: str, root: object = None) -> object:
        self._record(f"the docs dir for {project!r}")


def _refuse(stub: _StorageStub, call) -> dict:
    """Run ``call`` under the stub's block, which must never be triggered."""
    result = call()
    assert not stub.reached, f"a malformed request reached storage: {stub.paths}"
    assert result.get("ok") is False, result
    return result


def _stubbed(monkeypatch) -> _StorageStub:
    stub = _StorageStub()
    monkeypatch.setattr(io, "open", stub)
    monkeypatch.setattr(builtins, "open", stub)
    monkeypatch.setattr(mcp, "_docs_dir_for_project", stub.docs_dir_for)
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
    # The parts the view composes a row from that a classification of a pointer
    # does not itself carry: a name dropped from one of them is a field the
    # view fills, and the pre-flight would then refuse a request for it.
    for part in (query.COMMON_RUN_FIELDS, query.LIVE_PLACEMENT_FIELDS):
        assert set(part) <= query.LIVE_FIELD_VOCABULARY


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


def test_a_view_named_in_another_case_reaches_the_read(monkeypatch) -> None:
    """A spelling the read serves is not refused from the argument ahead of it.

    The plan and roadmap reads match a view after stripping and lowercasing it,
    so an uppercased name is served; the pre-flight has to judge it the same
    way. The stub is what makes this a served/refused assertion rather than a
    reading of the answer: the read must reach storage, which is the proof that
    no argument check turned it back. Without it the call resolves the host's
    own project registry, so the assertion would depend on the machine the
    suite runs on.
    """
    for call in (
        lambda: mcp._read_plan_tool(project="fixture", view="RAW"),
        lambda: mcp._roadmap_tool(project="fixture", view="SuMMaRy"),
    ):
        stub = _stubbed(monkeypatch)
        result: dict | None = None
        raised: str | None = None
        try:
            result = call()
        except AssertionError as error:
            raised = str(error)
        if raised is not None:
            assert "storage was reached" in raised, raised
        assert stub.reached, "an argument check refused a view the read serves"
        if result is not None:
            assert result.get("error") != "invalid_view", result


def test_the_runs_checks_serve_a_spelling_the_view_normalises() -> None:
    """The runs view's own source and scope normalisation is what judges them.

    ``runs_view`` strips and lowercases both before it compares, so ``'ALL'``
    and ``' Project '`` are served. A pre-flight testing the raw argument
    against the accepted set refuses them, and the refusal is one the caller
    cannot act on: the view would have answered the request it just turned back.

    The view echoes the source and scope it selected, so an answer carrying the
    normalised pair is what shows the request was judged by the view's own rule
    rather than turned back at the argument.
    """
    result = mcp._crew(
        project="fixture",
        view="runs",
        source="ALL",
        scope=" Project ",
    )
    assert result.get("ok") is True, result
    assert result["source"] == "all"
    assert result["scope"] == "project"


def test_every_pre_flight_check_is_judged_by_its_owner() -> None:
    """Every check the pre-flights make is judged by the owner that decides it.

    The table names each check, the value the pre-flight passes it, and what the
    owner makes of a differently cased or spaced spelling. A check written as
    its own membership test against the accepted set, or as its owner's
    normalisation restated, passes every refusal case and fails on a row here.

    The two checks whose owner compares the value as written carry no
    normalisation to miss, and are asserted as agreement instead: the crew view
    name, whose owner compares it raw against its registry, and the edit mode,
    whose owner is the write path this tool branches on. For each, the
    pre-flight refusing exactly what the owner refuses is the whole
    requirement, so the row asserts the two agree — on a name outside the
    registry and on a mode outside the two.
    """
    served = (
        # check -> the pre-flight's verdict, and what the owner makes of it
        (
            "runs source",
            mcp._crew_argument_refusal(
                "runs", fields=None, session=None, source="ALL", scope="project"
            ),
            query.normalize_runs_source("ALL"),
            "all",
        ),
        (
            "runs scope",
            mcp._crew_argument_refusal(
                "runs",
                fields=None,
                session=None,
                source="all",
                scope=" Project ",
            ),
            query.normalize_runs_scope(" Project "),
            "project",
        ),
        (
            "live fields",
            mcp._crew_argument_refusal(
                "live", fields=[" run_id "], session=None, source="all", scope="project"
            ),
            query._requested_field_names([" run_id "])[0],
            "run_id",
        ),
        (
            "plan view",
            None,
            mcp_views.normalize_view(" RAW "),
            "raw",
        ),
        (
            "roadmap view",
            None,
            mcp_views.normalize_roadmap_view(" SuMMaRy "),
            "summary",
        ),
        (
            "edit op verb",
            mcp_edit_plan._refuse_unknown_op_verbs(
                [{"op": "push"}], mcp_edit_plan._document_type(None, "SPRINT")
            ),
            mcp_edit_plan._document_type(None, "SPRINT"),
            "sprint",
        ),
    )
    for name, refusal, owner_value, expected in served:
        assert refusal is None, f"{name}: a served spelling was refused"
        assert owner_value == expected, f"{name}: {owner_value!r} != {expected!r}"

    # The crew view name is the check with no normalisation to miss: its owner
    # compares it as written, so the pre-flight's requirement is to refuse
    # exactly what the owner refuses. Asserted as agreement, so a pre-flight
    # that accepted a spelling the owner turns away fails here too.
    assert (
        mcp._crew_argument_refusal(
            "  LIVE  ", fields=None, session=None, source="all", scope="project"
        )
        is not None
    )
    try:
        mcp_views.crew_view_name("  LIVE  ")
    except mcp_views.ViewRequestError:
        pass
    else:
        raise AssertionError("the owner served a view the pre-flight refused")

    # The edit mode is the other: the tool's own write path compares it raw
    # against the two modes it implements, so an uppercased mode reaches that
    # comparison unchanged and is refused there.
    assert (
        mcp_edit_plan._edit_plan_tool(
            project="fixture", slug="a-plan", expected_version=1, mode="STATE"
        )["error"]
        == "invalid_edit_mode"
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
    """The runs view's own scope validator refuses it, and names what it serves.

    The refusal is the owner's message, so it names the accepted scopes rather
    than the value that was turned away — the wording a caller reads is the
    validator's, not a sentence the pre-flight composed.
    """
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub,
        lambda: mcp._crew(project="fixture", view="runs", scope="no_such_scope"),
    )
    assert "project" in result["detail"]
    assert "workstation" in result["detail"]


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
    assert "live" in result["message"]
    assert "ledger" in result["message"]


def test_the_cli_and_the_tool_refuse_one_view_the_same_way(monkeypatch) -> None:
    """Both crew surfaces reach one refusal, so both read the same keys.

    The crew MCP tool and the ``crew view`` CLI both answer through
    :func:`reckon.mcp_views.serve_crew_view`, so a view outside the served set
    is one request refused in one place. The tool's pre-flight refuses it before
    the server is called and the server refuses it when the CLI calls it, and
    both go through ``error_response``: the same four keys, naming the accepted
    set in the message and the refused view's own code, rather than one surface answering a ``detail`` shape the
    other does not.
    """
    stub = _stubbed(monkeypatch)
    from_the_tool = mcp._crew(project="fixture", view="no_such_view")
    from_the_cli = mcp_views.serve_crew_view("no_such_view", project="fixture")
    assert not stub.reached, f"a refusal reached storage: {stub.paths}"
    assert from_the_tool.keys() == from_the_cli.keys()
    for answer in (from_the_tool, from_the_cli):
        assert answer["error"] == "invalid_view"
        assert answer["operation"] == "read"
        assert set(answer) == {"ok", "error", "message", "operation"}
        # The accepted views are named, composed from the registry.
        assert "live" in answer["message"]
        assert "ledger" in answer["message"]


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
    """An unknown view answers the read's own refusal contract.

    The plan and roadmap reads answer a ``ViewRequestError`` raised inside them
    through ``error_response``, so the pre-flight refuses through
    ``error_response`` too rather than a shape of its own: one contract whether
    the argument check or the read itself turns the request back, which is what
    lets a caller read one answer shape.
    """
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub, lambda: mcp._read_plan_tool(project="fixture", view="no_such_view")
    )
    assert result["error"] == "invalid_view"
    assert result["operation"] == "read"
    assert "no_such_view" in result["message"]
    # The validator's own verdict, kept in its own fields: the message names
    # what it refused and the hint the values it accepts.
    assert "summary" in result["hint"]
    # The shape is the read's own: the same keys an in-read refusal carries.
    assert set(result) <= {
        "ok",
        "error",
        "message",
        "operation",
        "hint",
        "resource",
    }


def test_a_roadmap_naming_an_unknown_view_is_refused(monkeypatch) -> None:
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub,
        lambda: mcp._roadmap_tool(project="fixture", view="no_such_view"),
    )
    assert result["error"] == "invalid_view"
    assert result["operation"] == "read"
    assert "no_such_view" in result["message"]
    # The validator's own verdict, kept in its own fields: the message names
    # what it refused and the hint the values it accepts.
    assert "summary" in result["hint"]


def test_a_plan_batch_pushing_a_plan_is_refused(monkeypatch) -> None:
    """A verb the plan's own dispatch does not carry is refused from the verb.

    ``push`` marks a sprint active and the store dispatches it for a sprint
    alone; the grammar ``read_plan`` publishes lists it among the ops a caller
    may write, so judging the batch from that grammar accepted a push against a
    plan and let the request through to the store's read.
    """
    stub = _stubbed(monkeypatch)
    result = _refuse(
        stub,
        lambda: mcp_edit_plan._edit_plan_tool(
            project="fixture",
            slug="a-plan",
            expected_version=1,
            ops=[{"op": "push"}],
        ),
    )
    assert result["error"] == "op_error"
    assert "push" in result["detail"]
    assert "a plan" in result["detail"]
    # The verbs a plan does dispatch are named.
    assert "close" in result["detail"]


def test_the_same_verb_is_accepted_for_the_type_that_dispatches_it() -> None:
    """The other direction: a sprint's own op is not refused for a sprint."""

    assert mcp_edit_plan._refuse_unknown_op_verbs([{"op": "push"}], "sprint") is None
    assert mcp_edit_plan._refuse_unknown_op_verbs([{"op": "push"}], "plan") is not None
    # The accepted set is the dispatch, per document type.
    assert "push" not in mcp_edit_plan._accepted_op_verbs("plan")
    assert "push" in mcp_edit_plan._accepted_op_verbs("sprint")
