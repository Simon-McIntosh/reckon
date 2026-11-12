"""A stored meta value is written back verbatim, whatever it carries.

``_set_meta`` passes the new tag to ``re.sub``. A replacement *string* there is
a template, so a backslash in the value is read as a replacement escape: the
JSON store escapes non-ASCII, so a descope reason holding ``§`` is stored as
``\\u00a7`` and every later write raised ``re.PatternError``.
"""

from __future__ import annotations

from reckon import _plan_html

PLAN = """<!doctype html>
<html><head>
<meta name="docs-project" content="tmp">
<meta name="plan-slug" content="tmp-plan">
</head><body><h2 id="s1">§1 — A section</h2></body></html>
"""

REASON = "the section sign § is not ASCII"


def test_a_non_ascii_descope_reason_survives_a_second_write():
    first = _plan_html.write_state(
        PLAN, {"type": "plan", "descope_reasons": {"s1": REASON}}
    )
    # The stored JSON is ASCII-escaped, so the file now holds the literal
    # backslash sequence \u00a7 and the next write replaces that tag in place.
    assert "\\u00a7" in first

    second = _plan_html.write_state(
        first, {"type": "plan", "descope_reasons": {"s1": REASON}}
    )

    assert _plan_html.descope_reasons(second) == {"s1": REASON}
    assert second == first


def test_a_meta_value_with_backslashes_is_written_back_byte_for_byte():
    values = [r"C:\Users\u00a7\d", r"\u00a7", r"\d{2}\s"]

    for index, value in enumerate(values):
        name = f"plan-probe-{index}"
        doc = PLAN.replace("</head>", f'<meta name="{name}" content="stale">\n</head>')

        out = _plan_html._set_meta(doc, name, value)

        assert _plan_html._meta_content(out, name) == value
        assert f'content="{value}"' in out
