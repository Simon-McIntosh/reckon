"""The duplicate-scalar audit states the rule the plan reader follows.

A duplicated ``plan-*`` scalar is resolved by ``reckon._plan_html.read_state``
to its first occurrence in document order, and ``_set_meta`` rewrites that same
first line. The audit that reports the duplicate names the copy in force in its
message; the two must name the same copy in either order, or the audit tells
its reader the reverse of what the reader does.
"""

from __future__ import annotations

import inspect
import re

import pytest

from reckon import doccheck
from reckon._plan_html import read_state

_SHELL = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="docs-project" content="proj">'
    '<meta name="plan-slug" content="dup-scalar">'
    '<meta name="plan-status" content="active">'
    '<meta name="plan-standalone" content="fixture carries no wire">'
    "{copies}"
    "<title>dup-scalar</title></head>"
    '<body><main class="plan-doc"><p>The duplicate is reported.</p></main>'
    "</body></html>"
)


def _document(*copies: str) -> str:
    metas = "".join(f'<meta name="plan-impl" content="{value}">' for value in copies)
    return _SHELL.format(copies=metas)


def _duplicate_finding(text: str) -> doccheck.Finding:
    findings = [
        finding
        for finding in doccheck.audit_html(text)
        if finding.code == "duplicate-plan-scalar"
    ]
    assert len(findings) == 1, [finding.message for finding in findings]
    return findings[0]


def _copy_named_in_force(message: str, copies: list[str]) -> str:
    """The copy the message tells its reader is the one in force.

    The message states the reader's rule (first or last) and quotes the copy
    the rule keeps, so the named value is read out of the message rather than
    assumed. A claim naming an end without quoting a value resolves to that
    end of the document-order list.
    """
    claim = re.search(
        r"the reader takes the (first|last)\b[^']*(?:'([^']*)')?", message
    )
    assert claim is not None, message
    if claim.group(2) is not None:
        return claim.group(2)
    return copies[0] if claim.group(1) == "first" else copies[-1]


@pytest.mark.parametrize("copies", [("0.9", "0.1"), ("0.1", "0.9")])
def test_the_audit_names_the_copy_the_reader_returns(copies: tuple[str, str]):
    text = _document(*copies)

    reader_value = read_state(text)["impl"]
    assert reader_value == float(copies[0])

    message = _duplicate_finding(text).message
    named = _copy_named_in_force(message, list(copies))
    assert float(named) == reader_value, message


def test_the_finding_docstring_states_the_readers_rule():
    docstring = inspect.getdoc(doccheck._scalar_duplicate_findings) or ""
    assert "first occurrence in document order" in docstring
    assert "last-wins" not in docstring
