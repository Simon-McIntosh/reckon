"""A meta tag's content keeps quote characters of the other kind.

The inventory reads each document's <meta> tags by regex. An attribute delimited by
double quotes may contain apostrophes (and one delimited by single quotes may contain
double quotes); the value runs to the matching closing quote, not to the first quote
character of either kind.
"""

from reckon._plan_html import parse_meta

HEAD = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="demo">
  <meta name="reckon-type" content="research">
  <meta name="plan-slug" content="quotes-demo">
  <meta name="plan-summary" content="Pumping split, the helium leak's source circuit and flow law">
  <meta name='plan-source' content='the mimic labelled "MCTB VACUUM"'>
  <meta name="plan-status" content="reference">
  <title>Quotes demo</title>
</head>
<body><main class="plan-doc"><h1>Quotes demo</h1></main></body>
</html>
"""


def test_double_quoted_content_keeps_its_apostrophe(tmp_path):
    path = tmp_path / "quotes-demo.html"
    path.write_text(HEAD, encoding="utf-8")
    rec = parse_meta(path, "quotes-demo")
    assert (
        rec["summary"] == "Pumping split, the helium leak's source circuit and flow law"
    )


def test_single_quoted_content_keeps_its_double_quotes(tmp_path):
    path = tmp_path / "quotes-demo.html"
    path.write_text(HEAD, encoding="utf-8")
    rec = parse_meta(path, "quotes-demo")
    assert rec["source"] == 'the mimic labelled "MCTB VACUUM"'
    assert rec["status"] == "reference"
