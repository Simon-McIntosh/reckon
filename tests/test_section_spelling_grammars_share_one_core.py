"""Section number continuations agree while each reader keeps its own frame."""

import ast
import re
from pathlib import Path

import pytest

from reckon import _plan_html, _store, doccheck, evidence, ledger, roadmap


def test_undeclared_fallback_requires_prefixed_nonzero_heading():
    ids = ["s5", "s5.1", "s5-1", "5", "§5", "s0", "s0.1", "decisions"]
    source = "".join(f'<h2 id="{sid}">Work</h2>' for sid in ids)
    headings = [h.own_id for h in _plan_html.plan_headings(source)]
    state = _plan_html.read_state(source)
    assert not state.get("section_declarations")
    assert [sid for sid in headings if re.fullmatch(r"s[1-9][0-9]*", sid)] == ["s5"]
    assert doccheck._implementable_section_ids(state, headings) == [
        "s5",
        "s5.1",
        "s5-1",
    ]
    assert not doccheck._NUMBERED_SECTION_ID.fullmatch("section 5")
    assert not doccheck._NUMBERED_SECTION_ID.fullmatch("q1")


@pytest.mark.parametrize("prefix", ["", "s", "S", "#s", "section ", "§ "])
@pytest.mark.parametrize("number", ["5.1", "5.1.2", "05.01", "0.1"])
def test_dotted_labels_keep_their_ledger_and_evidence_outputs(prefix, number):
    value = prefix + number
    assert ledger.normalize_section(value) == "§" + number
    assert evidence._section_key(value) == "s" + number.replace(".", "-")


@pytest.mark.parametrize(
    ("value", "label", "key"),
    [
        ("s5-1", "§5-1", "s5-1"),
        ("a / b", "a / b", "a-b"),
        ("", "", "_top"),
        ("!!!", "!!!", "_top"),
    ],
)
def test_evidence_slugging_and_empty_bucket_keep_their_contract(value, label, key):
    assert ledger.normalize_section(value) == label
    assert evidence._section_key(value) == key


@pytest.mark.parametrize(
    ("text", "base", "head"),
    [
        ("section 5", ["s5"], ["s5"]),
        ("sections 5, 6 and 7", ["s5", "s6", "s7"], ["s5", "s6", "s7"]),
        ("section 5.1", ["s5"], ["s5.1"]),
        ("section 5-1", ["s5"], ["s5-1"]),
    ],
)
def test_gate_word_bindings_preserve_whole_numbers_and_bind_continuations(
    text, base, head, monkeypatch
):
    plan = {"sections": [{"id": sid} for sid in ["s5", "s6", "s7", "s5.1", "s5-1"]]}
    gate = {"measure": text}
    assert roadmap._gate_section_refs(plan, "sample", gate, {}) == [
        ("sample#" + sid, sid, "gate-text") for sid in head
    ]
    with monkeypatch.context() as context:
        context.setattr(
            roadmap,
            "_SECTION_WORD_RE",
            re.compile(
                r"\b(?:section|sections)\s+(\d+(?:\s*(?:,|and)\s*\d+)*)", re.IGNORECASE
            ),
        )
        assert roadmap._gate_section_refs(plan, "sample", gate, {}) == [
            ("sample#" + sid, sid, "gate-text") for sid in base
        ]


def test_gate_list_keeps_each_continuation_and_requires_a_declared_target():
    plan = {"sections": [{"id": sid} for sid in ["s5", "s5.1", "s6-2", "s7"]]}
    assert roadmap._gate_section_refs(
        plan, "sample", {"measure": "sections 5.1, 6-2 and 7"}, {}
    ) == [
        ("sample#s5.1", "s5.1", "gate-text"),
        ("sample#s6-2", "s6-2", "gate-text"),
        ("sample#s7", "s7", "gate-text"),
    ]
    assert (
        roadmap._gate_section_refs(plan, "sample", {"measure": "section 5.2"}, {}) == []
    )
    assert roadmap._gate_section_refs(
        plan, "sample", {"measure": "section 05"}, {}
    ) == [("sample#s5", "s5", "gate-text")]


@pytest.mark.parametrize("sid", ["s5", "s5.1", "s5-1"])
def test_new_numbered_heading_without_contract_is_refused(sid):
    with pytest.raises(ValueError, match="missing effort_hours and capability"):
        _store._require_new_section_contracts("", f'<h2 id="{sid}">Work</h2>')


@pytest.mark.parametrize("operation", ["declaration", "authored", "collapse"])
def test_store_identity_validations_use_schema_and_refuse_invalid_ids(
    operation, monkeypatch
):
    calls = []
    predicate = _store.is_section_identity

    def observed(value):
        calls.append(value)
        return predicate(value)

    monkeypatch.setattr(_store, "is_section_identity", observed)
    actions = {
        "declaration": lambda: _store._apply_set(
            {},
            {"path": "section_declarations.bad/id", "value": "implementable"},
            False,
            [],
        ),
        "authored": lambda: _store._require_authored_section_fields(
            {"id": "bad/id", "title": "Work", "body": ""}
        ),
        "collapse": lambda: _store._apply_collapse_section(
            {}, {"section": "bad/id"}, False, []
        ),
    }
    with pytest.raises(_store.OpError, match="safe section identity"):
        actions[operation]()
    assert calls == ["bad/id"]


def test_candidate_spellings_reuse_the_identity_grammar():
    assert _plan_html.section_id_candidates("section 5.1") == {
        "section 5.1",
        "s5.1",
        "s5-1",
    }
    assert _plan_html.section_id_candidates("a-b") == {"a-b"}


def test_number_core_is_owned_once_and_frames_compose_it():
    root = Path(_plan_html.__file__).parent
    core = _plan_html.SECTION_NUMBER_PATTERN
    assert core == r"\d+(?:[.-]\d+)*"
    assert core[len(r"\d+") :] == _plan_html.SECTION_NUMBER_CONTINUATION
    owners = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Constant) and node.value == core
            for node in ast.walk(tree)
        ):
            owners.append(path.relative_to(root).as_posix())
    assert owners == ["_plan_html.py"]
    assert core in _plan_html._SECTION_IDENTITY.pattern
    assert core in roadmap._SECTION_WORD_RE.pattern
    assert (
        doccheck._NUMBERED_SECTION_ID.pattern
        == "s[1-9][0-9]*" + _plan_html.SECTION_NUMBER_CONTINUATION
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
        )


#: ``re.fullmatch`` sites that compose the resource-segment grammar over an
#: argument that is not a section id, named by enclosing function and argument
#: so the exception is a fact about those two arguments rather than a
#: permitted count.
_SEGMENT_GRAMMAR_NON_SECTION = {
    ("_apply_append_evidence", "plan"),
    ("_apply_append_evidence", "anchor"),
}


def _function_nodes(tree):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _is_section_identity_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "is_section_identity"
    )


def _is_grammar_full_match(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fullmatch"
        and node.args
        and "A-Za-z0-9" in ast.unparse(node.args[0])
    )


def test_section_identity_validation_reuses_the_shared_helper():
    """The store validates a section id through the shared helper, never by
    re-spelling the identity grammar inline.

    A section identity is a resource path segment, so the grammar is owned by
    ``is_section_identity``. A validation that re-implements the full match
    drifts the moment the grammar moves, so the store's section-id validations
    route through the helper and no function anywhere in the store composes the
    segment grammar inline over an argument that is a section id. The two
    ``append_evidence`` arguments that share the grammar without being section
    ids — the plan slug and the evidence anchor — are the named exceptions.
    """
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


def test_numbered_heading_with_adjacent_contract_is_accepted():
    for sid in ["s5", "s5.1", "s5-1"]:
        source = (
            f'<h2 id="{sid}">Work</h2>'
            f'<section data-reckon="section" data-id="{sid}" '
            'data-effort-hours="1" data-capability-version="1.0" '
            'data-capability-class="general" data-capability-reasoning="standard" '
            'data-capability-verification="strict" data-capability-risk="low" '
            'data-status="implementable"></section>'
        )
        _store._require_new_section_contracts("", source)


def test_number_grammar_census_has_six_composed_frames():
    root = Path(_plan_html.__file__).parent
    sites = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "re"
                and call.args
            ):
                continue
            current = call
            owner = ""
            operand = ""
            while current in parents:
                current = parents[current]
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = current.name
                    break
                if isinstance(current, ast.Assign):
                    operand = ast.unparse(current.targets[0])
            context = owner or operand
            pattern = ast.unparse(call.args[0])
            if "section" not in context.lower() or not any(
                marker in pattern
                for marker in [r"\d", "[0-9]", "[1-9]", "SECTION_NUMBER"]
            ):
                continue
            assert "SECTION_NUMBER" in pattern, (path, context, pattern)
            sites.add((path.relative_to(root).as_posix(), context))
    assert sites == {
        ("_plan_html.py", "_SECTION_IDENTITY"),
        ("_store.py", "_require_new_section_contracts"),
        ("doccheck.py", "_NUMBERED_SECTION_ID"),
        ("evidence.py", "_section_key"),
        ("ledger.py", "normalize_section"),
        ("roadmap.py", "_SECTION_WORD_RE"),
    }


def test_every_authored_dotted_heading_keeps_its_normalizer_outputs():
    plans = Path(_plan_html.__file__).parent.parent / "docs" / "plans"
    inputs = {
        heading.own_id
        for path in plans.rglob("*.html")
        for heading in _plan_html.plan_headings(path.read_text())
        if heading.own_id is not None and "." in heading.own_id
    }
    assert inputs
    for value in inputs:
        match = re.fullmatch(
            r"(?:§\s*|#?s(?:ection)?\s*)?(\d+(?:\.\d+)*)", value, re.IGNORECASE
        )
        assert match, value
        assert ledger.normalize_section(value) == "§" + match.group(1)
        assert evidence._section_key(value) == "s" + match.group(1).replace(".", "-")


def test_fallback_delta_on_every_plan_is_its_numbered_continuation_headings():
    plans = Path(_plan_html.__file__).parent.parent / "docs" / "plans"
    observed = set()
    for path in plans.rglob("*.html"):
        headings = [
            h.own_id
            for h in _plan_html.plan_headings(path.read_text())
            if h.own_id is not None
        ]
        base = [sid for sid in headings if re.fullmatch(r"s[1-9][0-9]*", sid)]
        head = doccheck._implementable_section_ids({}, headings)
        additions = {
            sid for sid in headings if re.fullmatch(r"s[1-9][0-9]*(?:[.-]\d+)+", sid)
        }
        assert set(head) - set(base) == additions, path
        assert [sid for sid in head if sid in base] == base, path
        observed.update(additions)
    assert observed
