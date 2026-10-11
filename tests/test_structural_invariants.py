"""Repository-wide structural invariants that no runtime behaviour shows.

Most tests here drive the code and assert its effect. A few invariants are
about the *shape of the source itself* and are invisible to any call: that one
module owns the timestamp parser, that one module defines the number core and
every frame composes it, that one helper validates a section identity, and that
the test fixtures name no plan document or node identity. Those cannot be shown
by running a function, so they are asserted by reading the source through
``ast``. This module is the single place in the suite where reading source is
the point; keeping them together gives each invariant one home rather than a
copy beside every file it constrains.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import reckon
from reckon import _plan_html, _store, doccheck, evidence, ledger, roadmap
from reckon.crew.prescription import PRESCRIBED_PROPERTIES

_ROOT = Path(reckon.__file__).parent
_TESTS = Path(__file__).resolve().parent
_REPO = _TESTS.parent


def _module_paths() -> list[Path]:
    return sorted(_ROOT.rglob("*.py"))


def _enclosing_fromisoformat_calls(
    path: Path,
) -> list[tuple[str, str]]:
    """Every ``fromisoformat`` call site, named by enclosing function.

    A site is returned as ``(enclosing-function, "")`` so a caller can tell a
    module-level call from one inside a function, and a function's own name is
    the unit a reader reasons about rather than a line number that moves.
    """

    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
            else:
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "fromisoformat"
                ):
                    found.append((owner, ""))
                visit(child, owner)

    visit(tree, "<module>")
    return found


#: The one timestamp parser lives in ``reckon/_timestamps.py``; every other
#: call site is a retained exception, each with its reason. Each entry is
#: ``(module path relative to reckon/, enclosing function) -> reason``. The
#: reason states what that site reads that the shared parser does not, so a
#: reader can judge whether a new site belongs here or should migrate.
_RETAINED_FROMISOFORMAT: dict[tuple[str, str], str] = {
    ("doccheck.py", "modified_age_days"): (
        "reads the calendar date a stamp states in its first ten characters, "
        "not the instant a zone on the value names, so a zone must not move "
        "the day it reports"
    ),
    ("ledger.py", "_parse_timestamp"): (
        "returns the moment in the zone the stamp was written in; the shared "
        "parser normalises every zone to UTC"
    ),
    ("crew/dispatch_admission.py", "_lane_reading_carry"): (
        "its refusal quotes the parser's own exception text, which the shared "
        "parser swallows to return None"
    ),
    ("crew/quota_weight.py", "_as_date"): (
        "an as_of is a calendar date, not an instant: parse_utc returns a "
        "datetime and would widen the accepted domain, truncating a datetime "
        "string to its day and reading a bare integer as an epoch"
    ),
    ("crew/budget_reset.py", "_parse"): (
        "parses a reset store's stored stamps over the same string domain as "
        "parse_utc; retained here rather than routed, and a candidate to "
        "collapse onto the shared parser"
    ),
    ("crew/reports.py", "_instant"): (
        "reads only the fence grammar's own Z stamps and returns a POSIX "
        "instant; retained rather than routed, and a candidate to collapse "
        "onto the shared parser"
    ),
}


def _shared_parser_module() -> str:
    return "_timestamps.py"


def test_only_the_shared_parser_calls_fromisoformat() -> None:
    """No module parses an ISO stamp outside the shared parser and its named
    exceptions. Every deviation is listed with a reason, so a second parser
    cannot appear silently, and a listed exception that no longer exists is a
    stale entry that fails this test.
    """

    shared = _shared_parser_module()
    seen: set[tuple[str, str]] = set()
    offenders: list[str] = []
    for path in _module_paths():
        rel = path.relative_to(_ROOT).as_posix()
        if rel == shared:
            continue
        for owner, _ in _enclosing_fromisoformat_calls(path):
            seen.add((rel, owner))
            if (rel, owner) not in _RETAINED_FROMISOFORMAT:
                offenders.append(f"{rel}:{owner}")

    assert offenders == [], (
        "these modules parse ISO stamps without the shared parser or a named "
        f"exception: {offenders}"
    )
    assert seen == set(_RETAINED_FROMISOFORMAT), (
        "a retained exception no longer holds at its site: "
        f"{seen ^ set(_RETAINED_FROMISOFORMAT)}"
    )
    assert all(reason.strip() for reason in _RETAINED_FROMISOFORMAT.values())


def test_the_shared_parser_is_the_one_definition_of_its_helpers() -> None:
    """``parse_utc`` and ``parse_iso`` are defined once, in the shared module.

    A second top-level definition of either name is a competing parser, so
    the one-parser invariant rests on the helper's implementation too, not
    only on which call sites reach ``fromisoformat``.
    """

    definitions: dict[str, list[str]] = {"parse_utc": [], "parse_iso": []}
    for path in _module_paths():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name in definitions
            ):
                definitions[node.name].append(path.relative_to(_ROOT).as_posix())
    for name, owners in definitions.items():
        assert owners == [_shared_parser_module()], (name, owners)


def test_the_number_core_is_defined_once_and_every_frame_composes_it() -> None:
    """The number core is one string literal owned by ``_plan_html``.

    Every section-number grammar composes that core rather than re-spelling
    it, so a continuation the readers must agree on cannot drift in one frame
    while the others stay put.
    """

    core = _plan_html.SECTION_NUMBER_PATTERN
    assert core == r"\d+(?:[.-]\d+)*"
    assert core[len(r"\d+") :] == _plan_html.SECTION_NUMBER_CONTINUATION

    owners = [
        path.relative_to(_ROOT).as_posix()
        for path in _module_paths()
        if any(
            isinstance(node, ast.Constant) and node.value == core
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path)))
        )
    ]
    assert owners == ["_plan_html.py"]

    assert core in _plan_html._SECTION_IDENTITY.pattern
    assert core in roadmap._SECTION_WORD_RE.pattern
    assert doccheck._NUMBERED_SECTION_ID.pattern == (
        "s[1-9][0-9]*" + _plan_html.SECTION_NUMBER_CONTINUATION
    )

    for module, function in [
        (ledger, "normalize_section"),
        (evidence, "_section_key"),
        (_store, "_require_new_section_contracts"),
    ]:
        tree = ast.parse(Path(module.__file__).read_text())
        body = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == function
        )
        assert any(
            (isinstance(n, ast.Name) and n.id == "SECTION_NUMBER_PATTERN")
            or (isinstance(n, ast.Attribute) and n.attr == "SECTION_NUMBER_PATTERN")
            for n in ast.walk(body)
        ), (module.__name__, function)


#: ``re.fullmatch`` sites that compose the resource-segment grammar over an
#: argument that is not a section id, named by enclosing function and argument
#: so the exception is a fact about those two arguments rather than a
#: permitted count.
_SEGMENT_GRAMMAR_NON_SECTION = {
    ("_apply_append_evidence", "plan"),
    ("_apply_append_evidence", "anchor"),
}


def _function_nodes(tree: ast.AST) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _is_section_identity_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "is_section_identity"
    )


def _is_grammar_full_match(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fullmatch"
        and bool(node.args)
        and "A-Za-z0-9" in ast.unparse(node.args[0])
    )


def test_section_identity_has_one_definition_and_the_store_routes_through_it() -> None:
    """A section identity is validated by the shared helper, never re-spelled.

    The identity grammar is a resource path segment, owned by
    ``is_section_identity``. The store's section-id validations route through
    that helper, and no function in the store composes the segment grammar
    inline over an argument that is a section id. The two
    ``append_evidence`` arguments that share the grammar without being section
    ids — the plan slug and the evidence anchor — are the named exceptions.
    """

    definers = [
        path.relative_to(_ROOT).as_posix()
        for path in _module_paths()
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "is_section_identity"
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path)))
        )
    ]
    assert definers == ["_schema.py"], definers

    tree = ast.parse(Path(_store.__file__).read_text())
    callers = {
        node.name
        for node in _function_nodes(tree)
        if any(_is_section_identity_call(call) for call in ast.walk(node))
    }
    assert {
        "_apply_set",
        "_require_authored_section_fields",
        "_apply_collapse_section",
    } <= callers

    inline = {
        (node.name, ast.unparse(call.args[1]) if len(call.args) > 1 else "")
        for node in _function_nodes(tree)
        for call in ast.walk(node)
        if _is_grammar_full_match(call)
    }
    assert inline - _SEGMENT_GRAMMAR_NON_SECTION == set()


def test_the_prescription_fixtures_name_no_plan_document_that_exists() -> None:
    """The prescription fixtures name no real plan document, so a fixture
    cannot go stale the moment the plan moves. Scoped to that fixture's own
    source: other tests legitimately name real plan documents, so a
    repository-wide form of this check would fail on them. The check requires
    the file to reference a ``docs/plans/`` path at all, so a scan that finds
    nothing fails rather than passing vacuously.
    """

    source = (_TESTS / "test_prescription.py").read_text(encoding="utf-8")
    referenced = re.findall(r"docs/plans/([A-Za-z0-9._-]+\.html)", source)
    assert referenced, (
        "the fixture should exercise a plans/ path, or this checks nothing"
    )
    existing = [
        name for name in referenced if (_REPO / "docs" / "plans" / name).exists()
    ]
    assert existing == [], existing


def test_the_prescription_fixtures_name_no_node_identity() -> None:
    """The prescription fixtures are synthetic: every hyphenated identity they
    carry is one of the prescribed property names, never a real node id."""

    source = (_TESTS / "test_prescription.py").read_text(encoding="utf-8")
    candidates = set(re.findall(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){3,}\b", source))
    assert candidates <= set(PRESCRIBED_PROPERTIES)
