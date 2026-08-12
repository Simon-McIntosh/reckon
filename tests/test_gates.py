"""Round-trip coverage for semantic evidence gates."""

from __future__ import annotations

import re

from reckon._plan_html import read_state, write_state


GATES_SECTION = """<section data-reckon="gates" id="gates" class="r-gates">
<h2><span class="sec">§</span> Evidence gates</h2>
<div class="r-gate" data-id="round-trip" data-section="s2" data-gated-sections="s3,s4" data-status="closed" data-verdict="passed">
    <h4 class="r-gate-measure">Round-trip parity</h4>
    <p class="r-gate-required-evidence">A byte-identical <code>data-reckon</code> section</p>
    <a class="r-gate-evidence" href="/reckon/evidence/archive/gate-round-trip#result">Evidence</a>
</div>
</section>"""

PLAN_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="docs-project" content="reckon">
<title>Gate fixture | reckon</title>
</head>
<body>
<main class="plan-doc">
{GATES_SECTION}
</main>
</body>
</html>
"""


def _gates_section(html: str) -> str:
    match = re.search(
        r'<section data-reckon="gates".*?</section>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def test_gate_section_round_trips_byte_identically():
    state = read_state(PLAN_HTML)

    assert state["gates"] == [
        {
            "id": "round-trip",
            "section": "s2",
            "gated_sections": ["s3", "s4"],
            "status": "closed",
            "measure": "Round-trip parity",
            "required_evidence": "A byte-identical <code>data-reckon</code> section",
            "verdict": "passed",
            "evidence": "/reckon/evidence/archive/gate-round-trip#result",
            "passed": True,
        }
    ]

    state["gates"][0]["passed"] = False
    rendered = write_state(PLAN_HTML, state)

    assert _gates_section(rendered) == GATES_SECTION
    assert "data-passed" not in rendered
    assert read_state(rendered)["gates"][0]["passed"] is True
