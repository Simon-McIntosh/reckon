"""The invocation parser spells a section like every other reader.

A followup's ``/reckon-build <slug> §<n>`` prompt names a section. The parser's
section token composes the shared section-number core, so a dotted or
hyphenated spelling the plan writer emits resolves through
``section_record_id`` to the one identity the plan's records carry. An
alphabetic suffix is rejected because the plan writer never emits one.
"""

import ast
import re
from pathlib import Path

import pytest

from reckon import _plan_html, followup_pointers


def _bare_token_regex():
    """The section token before it composed the shared core (bare number)."""
    return re.compile(
        r"/reckon-[a-z][a-z0-9-]*"
        r"\s+(?P<target>[^\s§]+)"
        r"(?:\s*§\s*(?P<section>[0-9][0-9a-z]*))?"
    )


def _at_base(monkeypatch):
    """Restore the base parser: the bare token and the ``s<n>`` join."""
    monkeypatch.setattr(followup_pointers, "_INVOCATION_RE", _bare_token_regex())
    monkeypatch.setattr(
        followup_pointers, "section_record_id", lambda value: f"s{value}"
    )


@pytest.mark.parametrize(
    ("written", "base", "head"),
    [
        ("§5", "s5", "s5"),
        ("§5.1", "s5", "s5-1"),
        ("§5-1", "s5", "s5-1"),
        ("§5a", "s5a", None),
    ],
)
def test_section_token_resolves_spellings_through_the_shared_core(
    written, base, head, monkeypatch
):
    prompt = f"/reckon-build slug {written}"

    parsed = followup_pointers.parse_invocation(prompt)
    assert parsed is not None
    assert parsed.section == head

    with monkeypatch.context() as context:
        _at_base(context)
        parsed = followup_pointers.parse_invocation(prompt)
        assert (parsed.section if parsed else None) == base


def test_dotted_and_hyphenated_spellings_share_one_identity():
    for written in ("5.1", "5-1"):
        parsed = followup_pointers.parse_invocation(f"/reckon-build slug §{written}")
        assert parsed is not None
        assert parsed.section == _plan_html.section_record_id(written) == "s5-1"


def test_alphabetic_suffix_is_rejected():
    parsed = followup_pointers.parse_invocation("/reckon-build slug §5a")
    assert parsed is not None
    assert parsed.section is None


def _active_plan(declarations):
    return {
        "slug": "host",
        "project": "reckon",
        "workflow_status": "active",
        "section_declarations": declarations,
    }


@pytest.mark.parametrize(
    ("written", "base_reason", "head_reason"),
    [
        # s5 is declared work and s5-1 is not, so the dotted spelling moves from
        # the parent's verdict to the child's.
        ("§5", "implementable-section", "implementable-section"),
        ("§5.1", "implementable-section", "section-not-implementable"),
        ("§5-1", "implementable-section", "section-not-implementable"),
        ("§5a", "section-not-implementable", "no-section"),
    ],
)
def test_classify_followup_resolves_the_named_section(
    written, base_reason, head_reason, monkeypatch
):
    plan = _active_plan({"s5": "implementable"})
    followup = {"prompt": f"/reckon-build host {written}"}

    assert (
        followup_pointers.classify_followup(plan, followup, project="reckon").reason
        == head_reason
    )
    with monkeypatch.context() as context:
        _at_base(context)
        verdict = followup_pointers.classify_followup(plan, followup, project="reckon")
        assert verdict.reason == base_reason


def test_widened_census_reports_seven_core_composed_expressions():
    """Every section-spelling frame under reckon/ composes the shared core.

    The frame search is widened past expressions whose context names a
    section: a section matched inside a command or a reference (a section sign
    in the pattern, or an invocation frame) is a section-spelling expression
    too, and it must compose the core like the rest.
    """
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
            if not any(
                marker in pattern
                for marker in [r"\d", "[0-9]", "[1-9]", "SECTION_NUMBER"]
            ):
                continue
            if (
                "section" not in context.lower()
                and "§" not in pattern
                and "invocation" not in context.lower()
            ):
                continue
            assert "SECTION_NUMBER" in pattern, (path, context, pattern)
            sites.add((path.relative_to(root).as_posix(), context))
    assert sites == {
        ("_plan_html.py", "_SECTION_IDENTITY"),
        ("_store.py", "_require_new_section_contracts"),
        ("doccheck.py", "_NUMBERED_SECTION_ID"),
        ("evidence.py", "_section_key"),
        ("followup_pointers.py", "_INVOCATION_RE"),
        ("ledger.py", "normalize_section"),
        ("roadmap.py", "_SECTION_WORD_RE"),
    }


def test_invocation_expression_is_composed_from_the_exported_core():
    from reckon import _plan_html as html_module

    assert (
        html_module.SECTION_NUMBER_PATTERN in followup_pointers._INVOCATION_RE.pattern
    )
    for written, section in [
        ("§5", "s5"),
        ("§5.1", "s5-1"),
        ("§5-1", "s5-1"),
        ("§5a", None),
    ]:
        parsed = followup_pointers.parse_invocation(f"/reckon-build slug {written}")
        assert parsed is not None
        assert parsed.section == section
