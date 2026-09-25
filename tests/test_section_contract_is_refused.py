"""Section creation validates its contract before any plan bytes are written."""

import json
from copy import deepcopy

import pytest
from bs4 import BeautifulSoup

from reckon import _store as store
from reckon._schema import PlanState


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "docs" / "plans" / "sample.html"
    path.parent.mkdir(parents=True)
    path.write_text(
        '<!doctype html><html><head><meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="sample">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="0">'
        '</head><body><main><h2 id="s1">Existing work</h2>'
        "<p>Existing prose.</p>\n"
        '<section data-reckon="followups" id="followups"></section>'
        "</main></body></html>"
    )
    return tmp_path, path


def _item(**updates):
    return {
        "id": "s2",
        "title": "Implement & verify",
        "body": '<p class="deliverable">Keep <strong>authored HTML</strong>.</p>',
        "effort_hours": 1.25,
        "capability": {
            "version": "1.0",
            "class": "general",
            "requirements": {
                "reasoning": "standard",
                "verification": "strict",
                "risk": "low",
            },
        },
        "links": ["inputs#ready", "other:reference#method"],
        **updates,
    }


def _append(root, *items):
    working, version = store.read_plan("sample", "sample", root=root)
    store.apply_ops(
        working,
        [{"op": "append", "target": "sections", "item": item} for item in items],
        is_index=False,
    )
    PlanState.model_validate(working).validate_for_write()
    return store.write_plan("sample", "sample", working, version, root=root)


def _text(root, replacement, preimage="<p>Existing prose.</p>"):
    _, version = store.read_plan("sample", "sample", root=root)
    return store.replace_plan_text(
        "sample", "sample", preimage, replacement, version, root=root
    )


def _assert_example_can_be_written(root, message):
    example = json.loads(message.split("Example: ", 1)[1])
    assert example["op"] == "append"
    assert example["target"] == "sections"
    assert example["item"]["id"] == "s2"
    assert _append(root, example["item"]) == 1


def test_append_writes_heading_body_and_typed_record(plan):
    root, path = plan
    item = _item()
    assert _append(root, item) == 1
    state, version = store.read_plan("sample", "sample", root=root)
    assert version == 1
    expected = {key: item[key] for key in ("id", "effort_hours", "capability", "links")}
    assert state["sections"] == [{**expected, "attempts": 0, "status": "implementable"}]
    assert state["section_declarations"] == {"s2": "implementable"}
    html = path.read_text()
    soup = BeautifulSoup(html, "html.parser")
    assert soup.find("h2", id="s2").get_text() == item["title"]
    assert soup.select_one('[data-reckon="section"][data-id="s2"]') is not None
    assert item["body"] in html
    assert '<h2 id="s1">Existing work</h2><p>Existing prose.</p>' in html
    assert html.index('id="s2"') < html.index('id="followups"')


@pytest.mark.parametrize("field", ["effort_hours", "capability", "links"])
def test_append_refuses_missing_contract_field_with_worked_example(plan, field):
    root, path = plan
    item = _item()
    del item[field]
    original = path.read_bytes()
    with pytest.raises(store.OpError) as caught:
        _append(root, item)
    assert field in str(caught.value)
    assert path.read_bytes() == original
    _assert_example_can_be_written(root, str(caught.value))


@pytest.mark.parametrize("effort", [None, 0, -0.25, 0.3, "1.25", True, float("inf")])
def test_append_refuses_invalid_effort_with_worked_example(plan, effort):
    root, path = plan
    original = path.read_bytes()
    with pytest.raises(store.OpError) as caught:
        _append(root, _item(effort_hours=effort))
    assert "effort_hours" in str(caught.value)
    assert path.read_bytes() == original
    _assert_example_can_be_written(root, str(caught.value))


@pytest.mark.parametrize("field", ["class", "reasoning", "verification", "risk"])
@pytest.mark.parametrize("missing", [False, True])
def test_append_refuses_incomplete_or_off_enum_capability(plan, field, missing):
    root, path = plan
    item = _item()
    capability = item["capability"]
    target = capability if field == "class" else capability["requirements"]
    if missing:
        # The shared capability schema deliberately defaults the class.
        if field == "class":
            item["capability"] = None
        else:
            del target[field]
    else:
        target[field] = "unknown"
    original = path.read_bytes()
    with pytest.raises(store.OpError) as caught:
        _append(root, item)
    assert "capability" in str(caught.value)
    if not (field == "class" and missing):
        assert field in str(caught.value)
    assert path.read_bytes() == original
    _assert_example_can_be_written(root, str(caught.value))


def test_text_refuses_new_heading_without_record(plan):
    root, path = plan
    original = path.read_bytes()
    with pytest.raises(ValueError, match="effort_hours") as caught:
        _text(root, '<p>Existing prose.</p><h2 id="s2">Unsized work</h2><p>More.</p>')
    message = str(caught.value)
    assert "s2" in message
    assert "effort_hours" in message
    assert "capability" in message
    assert path.read_bytes() == original
    _assert_example_can_be_written(root, message)


@pytest.mark.parametrize(
    "heading",
    [
        "<H2 ID='s2'>Work</H2>",
        '<section><h2 id="s2">Work</h2></section>',
        '<h2 id="s2" data-reckon="section">Work</h2>',
    ],
)
def test_text_refuses_unrecorded_heading_variants(plan, heading):
    root, path = plan
    original = path.read_bytes()
    with pytest.raises(ValueError, match="effort_hours"):
        _text(root, "<p>Existing prose.</p>" + heading)
    assert path.read_bytes() == original


def test_text_refuses_partial_contract_with_worked_example(plan):
    root, path = plan
    original = path.read_bytes()
    with pytest.raises(ValueError, match="capability") as caught:
        _text(
            root,
            '<h2 id="s2" data-reckon="section" data-effort-hours="1">Work</h2>',
        )
    assert "capability" in str(caught.value)
    assert path.read_bytes() == original
    _assert_example_can_be_written(root, str(caught.value))


def test_existing_unrecorded_plan_remains_editable(plan):
    root, path = plan
    original, version = store.read_plan("sample", "sample", root=root)
    assert original["sections"] == []
    assert (
        store.write_plan("sample", "sample", deepcopy(original), version, root=root)
        == 0
    )
    assert _text(root, "<p>Amended prose.</p>")[0] == 1
    assert (
        _text(
            root, '<h2 id="s1">Amended heading</h2>', '<h2 id="s1">Existing work</h2>'
        )[0]
        == 2
    )
    state, version = store.read_plan("sample", "sample", root=root)
    assert version == 2
    assert state["sections"] == []
    assert '<h2 id="s1">Amended heading</h2><p>Amended prose.</p>' in path.read_text()


def test_text_that_only_quotes_a_heading_does_not_add_a_section(plan):
    root, _ = plan
    assert _text(root, '<p>&lt;h2 id="s2"&gt;Quoted&lt;/h2&gt;</p>')[0] == 1


@pytest.mark.parametrize("identity", ["s1", "followups"])
def test_append_refuses_existing_html_id_without_writing(plan, identity):
    root, path = plan
    original = path.read_bytes()
    with pytest.raises(store.OpError, match="already exists"):
        _append(root, _item(id=identity))
    assert path.read_bytes() == original


def test_duplicate_appends_are_atomic(plan):
    root, path = plan
    original = path.read_bytes()
    with pytest.raises(store.OpError, match="already exists"):
        _append(root, _item(), _item())
    assert path.read_bytes() == original


def test_invalid_later_append_leaves_no_heading_or_record(plan):
    root, path = plan
    original = path.read_bytes()
    with pytest.raises(store.OpError, match="effort_hours"):
        _append(root, _item(), _item(id="s3", effort_hours=0))
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "body",
    ['<h2 id="s3">Nested work</h2>', '<section data-reckon="comments"></section>'],
)
def test_append_cannot_smuggle_untyped_work_or_state_in_its_body(plan, body):
    root, path = plan
    original = path.read_bytes()
    with pytest.raises(store.OpError, match="body must not contain"):
        _append(root, _item(body=body))
    assert path.read_bytes() == original
