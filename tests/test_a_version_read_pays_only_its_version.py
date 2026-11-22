"""A version, raw or section read parses its own document and nothing else.

A cold ``read_plan(view="version")`` of a large plan resolved the plan's
section refs into the project metadata index, which builds the whole project
to answer one meta field. The ``version``, ``raw`` and ``section`` views serve
no dependency row, so they must not resolve refs at all: each parses the target
plan's own document and never builds the index. The ``summary`` and ``detail``
views keep the dependency rows they served before.
"""

from __future__ import annotations

import json
import re

import pytest

from reckon import _plan_html, mcp, metadata_index

_PROJECT = "sample"
_TARGET = "target"
_REFERENCED = ["alpha", "beta", "gamma"]

_SLUG_RE = re.compile(r'<meta name="plan-slug" content="([^"]+)">')


def _escape(content: str) -> str:
    """Escape a JSON value for an HTML meta ``content`` attribute."""

    return content.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _plan_doc(
    slug: str,
    *,
    section_refs: dict[str, list[str]] | None = None,
    sections: list[str] | None = None,
) -> str:
    header = ""
    if section_refs is not None:
        header = (
            '<meta name="plan-section-depends-on" '
            f'content="{_escape(json.dumps(section_refs))}">\n'
        )
    body = "".join(
        f'<h2 id="{ident}">§ {ident}</h2>\n'
        f'<section data-reckon="section" data-id="{ident}" '
        'data-effort-hours="1.0" data-capability-version="1.0" '
        'data-capability-class="general" data-capability-reasoning="standard" '
        'data-capability-verification="strict" '
        'data-capability-risk="moderate" data-status="implementable"></section>\n'
        for ident in sections or []
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{_PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-status" content="active">
<meta name="plan-title" content="{slug.title()} plan">
{header}<title>{slug.title()}</title></head><body>
<main class="plan-doc">
{body}</main></body></html>
"""


def _slug_of(text: str) -> str:
    found = _SLUG_RE.search(text or "")
    return found.group(1) if found else ""


def _clear_caches() -> None:
    from reckon import file_memo

    file_memo.clear()
    metadata_index.clear()


@pytest.fixture(autouse=True)
def _isolated_caches():
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A temporary checkout whose target plan declares three section refs."""

    from reckon import serve

    config_home = tmp_path / "config"
    config_home.mkdir()
    repository = tmp_path / "repository"
    docs_dir = repository / "docs"
    plans_dir = docs_dir / "plans"
    plans_dir.mkdir(parents=True)

    (plans_dir / "target.html").write_text(
        _plan_doc(
            "target",
            section_refs={
                "s1": ["alpha#s1"],
                "s2": ["beta", "gamma"],
            },
            sections=["s1", "s2"],
        )
    )
    for slug in _REFERENCED:
        (plans_dir / f"{slug}.html").write_text(_plan_doc(slug))
    # Padding so a full project build is visibly different from one document.
    for index in range(8):
        slug = f"padding-{index:02d}"
        (plans_dir / f"{slug}.html").write_text(_plan_doc(slug))

    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({_PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    return repository


@pytest.fixture()
def counter(monkeypatch):
    """Count project index builds and document parses.

    A parse is one call to the uncached plan parser, keyed by the slug the
    parsed text carries; a build is one call to the index builder that a
    discovery read reaches.
    """

    real_build = metadata_index.build_index
    real_read_state = _plan_html.read_state

    record = {"builds": 0, "parsed": []}

    def counted_build(*args, **kwargs):
        record["builds"] += 1
        return real_build(*args, **kwargs)

    def counted_read_state(text):
        record["parsed"].append(_slug_of(text))
        return real_read_state(text)

    monkeypatch.setattr(metadata_index, "build_index", counted_build)
    monkeypatch.setattr(_plan_html, "read_state", counted_read_state)
    return record


def _read(store, *, view: str, section: str | None = None):
    return mcp._read_plan(
        project=_PROJECT,
        slug=_TARGET,
        checkout_path=str(store),
        view=view,
        section=section,
    )


@pytest.mark.parametrize("view", ["version", "raw", "section"])
def test_view_builds_no_index_and_parses_one_document(store, counter, view):
    result = _read(store, view=view, section="s1" if view == "section" else None)

    assert result.get("ok") is not False, result
    assert counter["builds"] == 0, counter
    parsed = [slug for slug in counter["parsed"] if slug]
    assert parsed == ["target"], counter["parsed"]


def test_detail_keeps_the_section_rows(store):
    result = _read(store, view="detail")

    assert result.get("ok") is not False, result
    rows = result.get("section_blocking") or []
    by_section = {row["section"]: row for row in rows}

    assert set(by_section) == {"s1", "s2"}
    assert [wait["ref"] for wait in by_section["s1"]["waits_on"]] == ["alpha#s1"]
    assert [wait["ref"] for wait in by_section["s2"]["waits_on"]] == ["beta", "gamma"]
    for row in rows:
        for wait in row["waits_on"]:
            assert wait["found"] is True
            assert wait["status"] == "active"
