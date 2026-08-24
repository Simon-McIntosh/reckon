from __future__ import annotations

from bs4 import BeautifulSoup
import pytest

from reckon._plan_html import from_html, read_state, to_html, write_state
from reckon._schema import Sprint
from reckon.tags import normalise_tag


AUTHORED_TAGS = [" Standard_Names ", "Plasma Control"]
CANONICAL_TAGS = [normalise_tag(tag) for tag in AUTHORED_TAGS]


def _resource_html(resource_type: str) -> str:
    plan_only = (
        '<meta name="plan-status" content="active">' if resource_type == "plan" else ""
    )
    return f"""<!doctype html>
<html><head>
<meta name="docs-project" content="reckon">
<meta name="reckon-type" content="{resource_type}">
<meta name="plan-slug" content="tagged-resource">
<meta name="plan-title" content="Tagged resource">
<meta name="plan-tags" content="{",".join(AUTHORED_TAGS)}">
{plan_only}
<title>Tagged resource</title>
</head><body><main><p>Body</p></main></body></html>
"""


@pytest.mark.parametrize("resource_type", ["plan", "research", "evidence"])
def test_html_resource_round_trips_two_canonical_tags(resource_type: str) -> None:
    authored = _resource_html(resource_type)

    parsed = from_html(authored)
    written = to_html(authored, parsed)

    assert parsed.tags == CANONICAL_TAGS
    assert read_state(written)["tags"] == CANONICAL_TAGS
    tag_meta = BeautifulSoup(written, "html.parser").find(
        "meta", attrs={"name": "plan-tags"}
    )
    assert tag_meta is not None
    assert tag_meta["content"] == ",".join(CANONICAL_TAGS)


def test_sprint_round_trips_two_canonical_tags() -> None:
    parsed = Sprint.model_validate(
        {"id": "iteration", "theme": "Tagged work", "tags": AUTHORED_TAGS}
    )
    written = parsed.model_dump(exclude_unset=True)

    assert written["tags"] == CANONICAL_TAGS
    assert Sprint.model_validate(written).tags == CANONICAL_TAGS


def test_sprint_refuses_a_tag_without_a_canonical_identity() -> None:
    with pytest.raises(ValueError, match="empty identity"):
        Sprint.model_validate({"id": "iteration", "tags": ["!?"]})


def test_write_state_normalises_and_deduplicates_authored_spellings() -> None:
    authored = _resource_html("research")
    state = read_state(authored)
    state["tags"] = ["Standard_Names", "standard names", "Plasma Control"]

    written = write_state(authored, state)

    assert read_state(written)["tags"] == CANONICAL_TAGS


def test_resource_is_reported_under_every_tag_it_carries() -> None:
    resource = from_html(_resource_html("evidence"))
    resources_by_tag: dict[str, list[str]] = {}
    for tag in resource.tags:
        resources_by_tag.setdefault(tag, []).append(resource.slug)

    assert resources_by_tag == {
        CANONICAL_TAGS[0]: ["tagged-resource"],
        CANONICAL_TAGS[1]: ["tagged-resource"],
    }
