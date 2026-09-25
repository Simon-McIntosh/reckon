"""The header-duplicate check exempts a landed section's summary header.

The collapse-on-landing template puts a ``<header>`` as the direct child of a
``<section class="section-landed">``, so a document that has collapsed three
sections legitimately carries four headers. Only the shell header is the
document's own; the rest are summary-card chrome.
"""

from __future__ import annotations

from reckon.doccheck import audit_html

SHELL_HEADER = "<header><h1>Plan summary</h1></header>"


def _plan(body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="plan-slug" content="test">'
        '<meta name="plan-status" content="active">'
        "<title>test</title></head>"
        f'<body><main class="plan-doc">{body}</main></body></html>'
    )


def _landed_section(index: int) -> str:
    return (
        f'<section id="s{index}" class="section-landed">'
        f'<header><span class="badge badge-shipped">landed</span>'
        f"<h2>§{index} — a landed section</h2></header>"
        f'<p class="landed-summary">Built the thing; suite green.</p>'
        "</section>"
    )


def _duplicate_findings(html: str):
    return [f for f in audit_html(html) if f.code == "header-duplicate"]


def test_three_landed_sections_add_no_duplicate_finding():
    body = SHELL_HEADER + "".join(_landed_section(n) for n in (1, 2, 3))
    assert _duplicate_findings(_plan(body)) == []


def test_landed_section_headers_do_not_mask_a_second_shell_header():
    body = SHELL_HEADER + _landed_section(1) + "<header><h1>Second</h1></header>"
    findings = _duplicate_findings(_plan(body))
    assert len(findings) == 1
    # Only the two shell headers count; the landed section's header is exempt.
    assert "document carries 2 <header> elements" in findings[0].message


def test_header_not_a_direct_child_of_landed_section_still_counts():
    body = SHELL_HEADER + (
        '<section id="s1" class="section-landed">'
        "<div><header><h2>boxed summary</h2></header></div>"
        "</section>"
    )
    findings = _duplicate_findings(_plan(body))
    assert len(findings) == 1
    assert "document carries 2 <header> elements" in findings[0].message


def test_unlanded_section_header_still_counts():
    body = SHELL_HEADER + '<section id="s1"><header><h2>live</h2></header></section>'
    findings = _duplicate_findings(_plan(body))
    assert len(findings) == 1
    assert "document carries 2 <header> elements" in findings[0].message
