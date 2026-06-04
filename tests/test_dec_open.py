"""dec_open must mirror the SPA decision widget's isTaken predicate.

The widget (docs/ui/decision.jsx) renders a decision GREEN/taken when it has a
choice OR a recorded rationale. The inventory's dec_open count (which drives the
"Resolve N" button) must agree: a decision deferred with a rationale but no
choice is taken, not open. Otherwise the button shows "Resolve 1" over a green
decision (reported bug, 2026-06-04).
"""

from pathlib import Path

from reckon import _plan_html


_DOC = """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="dec-open-fixture">
<meta name="plan-status" content="active">
</head><body><main class="plan-doc">
<section data-reckon="decisions" class="r-decisions">
  <div class="r-dec" data-key="has-choice" data-choice="opt-a">
    <p class="r-dec-q">Decided with a choice?</p>
    <p class="r-dec-rat">picked a</p>
  </div>
  <div class="r-dec" data-key="rationale-only" data-choice="">
    <p class="r-dec-q">Deferred with a rationale, no choice?</p>
    <p class="r-dec-opts"><button class="r-opt" data-value="x">x</button></p>
    <p class="r-dec-rat">Resolve at D1; default to the S9 set.</p>
  </div>
  <div class="r-dec" data-key="truly-open" data-choice="">
    <p class="r-dec-q">Neither choice nor rationale?</p>
    <p class="r-dec-rat"></p>
  </div>
</section>
</main></body></html>
"""


def _write(tmp_path: Path) -> Path:
    p = tmp_path / "dec-open-fixture.html"
    p.write_text(_DOC, encoding="utf-8")
    return p


def test_parse_meta_dec_open_honours_rationale(tmp_path):
    # Only `truly-open` is open. `has-choice` (choice) and `rationale-only`
    # (rationale) are both taken.
    rec = _plan_html.parse_meta(_write(tmp_path))
    assert rec["dec_open"] == 1


def test_parse_plan_dec_open_honours_rationale(tmp_path):
    rec = _plan_html.parse_plan(_write(tmp_path))
    assert rec["dec_open"] == 1


def test_meta_and_plan_agree(tmp_path):
    p = _write(tmp_path)
    assert _plan_html.parse_meta(p)["dec_open"] == _plan_html.parse_plan(p)["dec_open"]
