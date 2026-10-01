"""A landing record in a section's own body is read, and a write leaves it there.

Measured on ``docs/plans/scoring-should-stop-a-promotion.html``: ``read_state``
collected ``.r-comment`` only inside ``section[data-reckon="comments"]``, so a
landing record an agent wrote into a section's body was invisible to every
agent read — ``parse_plan()['comments']`` omitted 19 of that plan's 111
records, including both §2d landing records. A record nobody can read is not a
record, and this one reads correctly to a human in a browser, which is what
lets it survive.

A comment element belongs to the section its own ``data-section`` names,
wherever it sits. Widening the read alone would make the next write copy every
body record into the comments section, so the writer renders only the records
the comments section holds; this file pins both halves.
"""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup

from reckon import _store
from reckon._plan_html import parse_plan, read_state

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_PLANS = REPO_ROOT / "docs" / "plans"

COMMENTS_SPAN = re.compile(
    r'<section[^>]*data-reckon="comments"[^>]*>.*?</section>', re.IGNORECASE | re.DOTALL
)
STAMPED_METAS = re.compile(
    r'<meta\s+name="plan-(?:version|modified)"[^>]*>\s*', re.IGNORECASE
)

# A record in the comments section (data-section="s1") and a landing record an
# agent wrote into section s2's own body, which is the shape the plan corpus
# carries. The comments section is spelled exactly as the writer renders it, so
# a write that changes nothing about it must reproduce it byte for byte.
BODY_RECORD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="docs-project" content="reckon">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="synthetic-body-record">
<meta name="plan-title" content="Synthetic body record">
<meta name="plan-version" content="3">
<title>Synthetic body record | reckon</title>
</head>
<body>
<main class="plan-doc">
<h2 id="s1">§1 — The first section</h2>
<p>The first section's authored prose.</p>
<h2 id="s2">§2 — The second section</h2>
<p>The second section's authored prose.</p>
    <div class="r-comment" data-section="s2" data-id="c-in-body" data-who="crew-worker" data-when="2026-10-01">
      <div class="r-comment-body"><p>the section-body record</p></div>
    </div>
<h2 id="s3">§3 — The third section</h2>
<p>The third section's authored prose.</p>
<section data-reckon="comments" id="comments" class="r-comments">
<div class="r-comment" data-section="s1" data-id="c-in-comments" data-who="crew-worker" data-when="2026-10-01">
    <div class="r-comment-body"><p>the comments-section record</p></div>
</div>
</section>
</main>
</body>
</html>
"""

CODE_SAMPLE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="docs-project" content="reckon">
<title>Quoted markup | reckon</title>
</head>
<body>
<main class="plan-doc">
<h2 id="s1">§1 — Quoting the markup</h2>
<pre><code>&lt;div class="r-comment" data-section="s1" data-id="c-quoted"&gt;&lt;/div&gt;</code></pre>
</main>
</body>
</html>
"""


def _write_plan_tree(tmp_path: Path, name: str, text: str) -> Path:
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    target = plans / name
    target.write_text(text, encoding="utf8")
    return target


def _record_blocks(text: str, comment_id: str) -> list[str]:
    """The raw source text of every element carrying ``data-id=comment_id``,
    extracted by tag depth counting so the comparison is on file bytes."""
    blocks = []
    for match in re.finditer(
        rf'<div class="r-comment"(?=[^>]*data-id="{re.escape(comment_id)}")', text
    ):
        depth = 0
        cursor = match.start()
        for tag in re.finditer(r"<div\b|</div>", text[match.start() :]):
            depth += 1 if tag.group(0) == "<div" else -1
            if depth == 0:
                cursor = match.start() + tag.end()
                break
        blocks.append(text[match.start() : cursor])
    return blocks


def _outside_editable(text: str) -> str:
    """Everything a state write may legitimately touch (the comments section
    and the version/modified stamps) removed."""
    return STAMPED_METAS.sub("", COMMENTS_SPAN.sub("", text))


def _body_record_ids(text: str) -> list[str]:
    soup = BeautifulSoup(text, "html.parser")
    return [
        element.get("data-id")
        for element in soup.select(".r-comment")
        if element.find_parent("section", attrs={"data-reckon": "comments"}) is None
    ]


def _project_of(text: str) -> str:
    meta = BeautifulSoup(text, "html.parser").find(
        "meta", attrs={"name": "docs-project"}
    )
    return (meta.get("content") if meta else "") or ""


def _body_record_plans() -> list[Path]:
    return [
        path
        for path in sorted(CORPUS_PLANS.glob("*.html"))
        if _body_record_ids(path.read_text(encoding="utf8"))
    ]


def test_both_records_are_read_under_the_section_that_holds_them():
    state = read_state(BODY_RECORD_HTML)

    assert [c["id"] for c in state["comments"]["s1"]] == ["c-in-comments"]
    assert [c["id"] for c in state["comments"]["s2"]] == ["c-in-body"]
    assert state["comments"]["s2"][0]["body"] == "<p>the section-body record</p>"


def test_parse_plan_returns_a_section_body_record(tmp_path):
    target = _write_plan_tree(tmp_path, "synthetic-body-record.html", BODY_RECORD_HTML)

    record = parse_plan(target, "synthetic-body-record")

    assert [c["id"] for c in record["comments"]["s1"][0:1]] == ["c-in-comments"]
    assert [c["id"] for c in record["comments"]["s2"]] == ["c-in-body"]


def test_a_write_leaves_a_section_body_record_where_it_is(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    target = _write_plan_tree(tmp_path, "synthetic-body-record.html", BODY_RECORD_HTML)
    before = target.read_text(encoding="utf8")

    state, version = _store.read_plan("reckon", "synthetic-body-record", root=tmp_path)
    comments = {key: list(items) for key, items in state["comments"].items()}
    comments["s1"].append(
        {
            "id": "c-edited-write",
            "who": "crew-worker",
            "when": "2026-10-01",
            "body": "<p>a later edit</p>",
        }
    )
    _store.write_plan(
        "reckon",
        "synthetic-body-record",
        {**state, "comments": comments},
        version,
        root=tmp_path,
    )

    after = target.read_text(encoding="utf8")
    assert _outside_editable(after) == _outside_editable(before)
    assert len(_record_blocks(after, "c-in-body")) == 1
    assert _record_blocks(after, "c-in-body") == _record_blocks(before, "c-in-body")
    # exactly one comment element was added, and it is the appended one
    ids_before = re.findall(r'class="r-comment"[^>]*data-id="([^"]*)"', before)
    ids_after = re.findall(r'class="r-comment"[^>]*data-id="([^"]*)"', after)
    assert sorted(ids_after) == sorted([*ids_before, "c-edited-write"])


def test_quoted_markup_in_a_code_sample_is_not_a_record():
    state = read_state(CODE_SAMPLE_HTML)

    assert state["comments"] == {}


def test_every_plan_record_is_returned_by_read_state():
    """The corpus sweep: the reader omits no comment element it is given."""
    checked = 0
    for path in sorted(CORPUS_PLANS.glob("*.html")):
        text = path.read_text(encoding="utf8")
        if "r-comment" not in text:
            continue
        soup = BeautifulSoup(text, "html.parser")
        elements = [
            element
            for element in soup.select(".r-comment")
            if element.find_parent("pre") is None
            and element.find_parent("code") is None
        ]
        state = read_state(text)
        read_ids = [c["id"] for group in state["comments"].values() for c in group]
        assert len(read_ids) == len(elements), path.name
        checked += 1
    assert checked > 100


def test_an_edited_write_preserves_a_corpus_section_body_record(tmp_path, monkeypatch):
    """Every plan carrying a body record keeps it byte for byte through an
    edited write, in a temporary copy of the corpus."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    plans_copy = tmp_path / "docs" / "plans"
    plans_copy.mkdir(parents=True)
    touched = 0
    for path in _body_record_plans():
        before = path.read_text(encoding="utf8")
        body_ids = _body_record_ids(before)
        project = _project_of(before)
        target = plans_copy / path.name
        target.write_text(before, encoding="utf8")
        state, version = _store.read_plan(project, path.stem, root=tmp_path)
        assert state, path.name
        comments = {key: list(items) for key, items in state["comments"].items()}
        comments.setdefault("_top", []).append(
            {
                "id": "c-edited-write",
                "who": "crew-worker",
                "when": "2026-10-01",
                "body": "<p>a later edit</p>",
            }
        )
        _store.write_plan(
            project, path.stem, {**state, "comments": comments}, version, root=tmp_path
        )
        after = target.read_text(encoding="utf8")
        assert _outside_editable(after) == _outside_editable(before), path.name
        assert _body_record_ids(after) == body_ids, path.name
        for comment_id in body_ids:
            blocks_before = _record_blocks(before, comment_id)
            blocks_after = _record_blocks(after, comment_id)
            assert blocks_after == blocks_before, (path.name, comment_id)
        touched += 1
    assert touched >= 1
