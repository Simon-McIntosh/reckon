"""The brief is the roadmap's default answer, and the skills lead with it.

A coordinator opening a project asks the roadmap what to do next. The default
answer to that question is the brief: the composed lines, each carrying the
payload rows behind it, so a reader acts on one line without a second call. The
naming views stay for a caller that asks for them, and a named ``summary`` read
answers exactly what it answered before the brief existed.

The skills are where an agent learns which read to make first, so each fast
path names the brief before it names any other roadmap or plan view. A skill
that lists ``summary`` first sends every status question through the full
report and nothing tells the reader the brief exists.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

import reckon.mcp as mcp_module
from reckon._plan_html import write_state
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "fixture"
ROOT = Path(__file__).resolve().parents[1]

#: A roadmap payload whose ranked ready sections are known in advance, so the
#: reason each brief row carries can be compared against the row it came from.
RANKED = [
    {
        "ref": "foundation#s1",
        "plan": "foundation",
        "ready": True,
        "reason": "no section waits on it and its gate has passed",
        "unlocks": ["foundation#s2", "foundation#s3", "consumer#s1"],
    },
    {
        "ref": "foundation#s2",
        "plan": "foundation",
        "ready": True,
        "reason": "its declared upstream landed",
        "unlocks": ["consumer#s1"],
    },
    {
        "ref": "consumer#s1",
        "plan": "consumer",
        "ready": True,
        "reason": "the section declares no wait",
        "unlocks": [],
    },
]


def _raw() -> dict[str, Any]:
    """One project's lossless roadmap report, shaped as the analyzer returns it."""

    return {
        "project": PROJECT,
        "completion": {
            "plans": 2,
            "completed": 0,
            "pending": 2,
            "lifecycle_completion_pct": 25.0,
            "implementation_pct": 40.0,
        },
        "ready_now": [{"slug": "foundation"}],
        "blocked": [],
        "deferred": [],
        "active_sprint_id": "sprint-1",
        "sprints": [
            {
                "id": "sprint-1",
                "theme": "make the roadmap answer the question asked",
                "status": "active",
            }
        ],
        "summary_sprints": [
            {
                "id": "sprint-1",
                "theme": "make the roadmap answer the question asked",
                "status": "active",
            }
        ],
        "critical_path": {
            "plans": ["foundation", "consumer"],
            "sections": ["foundation#s1", "foundation#s2", "consumer#s1"],
            "length_hours": 6.0,
            "worker_hours": 5.0,
        },
        "immediate_roadmap": [dict(row) for row in RANKED],
        "wiring_findings": [],
        "pending_work": [],
    }


@pytest.fixture()
def stubbed_payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Answer every scan the roadmap read makes with the fixture payload.

    The composition under test is the brief over a report, so the report is the
    fixture: a scan that read a live mount would make the assertion depend on
    the checkout rather than on the composition.
    """

    raw = _raw()
    monkeypatch.setattr(
        mcp_module,
        "_discover_project",
        lambda *args, **kwargs: {"inventory": [], "sprints": []},
    )
    monkeypatch.setattr(
        mcp_module,
        "read_plan",
        lambda *args, **kwargs: ({"projects": [], "active_sprint_id": None}, 0),
    )
    monkeypatch.setattr(mcp_module, "list_followups_across", lambda *args, **kwargs: [])
    monkeypatch.setattr(mcp_module, "build_roadmap", lambda *args, **kwargs: raw)
    return raw


@pytest.fixture()
def mounted_project(tmp_path, monkeypatch):
    """One mounted project on disk, so the default read runs the real route."""

    project = PROJECT
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    reload_mcp_family()

    _write_plan(docs, "foundation")
    _write_plan(docs, "consumer", depends_on=["foundation"])
    return project, docs


def _write_plan(docs: Path, slug: str, *, depends_on: list[str] | None = None) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        write_state(
            bare,
            {
                "slug": slug,
                "title": slug.title(),
                "status": "active",
                "depends_on": depends_on or [],
                "decisions": {},
                "gates": [],
                "followups": [{"id": "next-action", "prompt": f"/reckon-build {slug}"}],
                "version": 0,
            },
        ),
        encoding="utf-8",
    )
    return path


def test_a_default_roadmap_read_answers_with_the_brief(mounted_project) -> None:
    """No view named: the answer is the brief, not the summary."""

    project, _docs = mounted_project

    answer = mcp_module._roadmap_tool(project)

    assert answer["view"] == "brief"
    assert answer["project"] == project
    assert isinstance(answer["text"], str) and answer["text"]
    assert answer["lines"], "the brief carries the typed rows behind its lines"
    for line in answer["lines"]:
        assert set(line) == {"key", "text", "values"}, line
    keys = [line["key"] for line in answer["lines"]]
    assert "ready_sections" in keys
    assert "critical_path" in keys


def test_the_briefs_ranked_rows_carry_the_rankings_own_reason(
    stubbed_payload: dict[str, Any],
) -> None:
    """Each ranked row in the brief repeats the reason the ranking gave it."""

    answer = mcp_module._roadmap_scoped(PROJECT, view="brief")

    assert answer["view"] == "brief"
    line = next(item for item in answer["lines"] if item["key"] == "ready_sections")
    ranked = line["values"]["sections"]
    assert [entry["ref"] for entry in ranked] == [row["ref"] for row in RANKED]
    for entry, row in zip(ranked, RANKED, strict=True):
        assert entry["reason"] == row["reason"], entry
        assert entry["reason"], "a ranked row with no reason tells a reader nothing"


def test_a_named_summary_read_is_unchanged(stubbed_payload: dict[str, Any]) -> None:
    """A caller naming ``summary`` receives the summary, and only the summary."""

    scoped = mcp_module._roadmap_scoped(PROJECT, view="summary")
    tool = mcp_module._roadmap_tool(PROJECT, view="summary")

    assert scoped["view"] == "summary"
    assert tool["view"] == "summary"
    assert "text" not in tool and "lines" not in tool
    assert tool["critical_path"] == stubbed_payload["critical_path"]
    assert tool["completion"] == scoped["completion"]


def test_the_brief_carries_the_summarys_own_figures(
    stubbed_payload: dict[str, Any],
) -> None:
    """The brief's summary fields are the summary view's, not a second derivation."""

    brief = mcp_module._roadmap_scoped(PROJECT, view="brief")
    summary = mcp_module._roadmap_scoped(PROJECT, view="summary")

    for key, value in summary.items():
        if key == "view":
            continue
        assert brief[key] == value, key


def _fast_path(skill: str) -> str:
    """Return the fast-path block of one skill, up to its next heading."""

    text = (ROOT / skill).read_text(encoding="utf-8")
    block = re.search(
        r"^## Fast path[ \t]*\n(.*?)(?=^## |\Z)", text, re.DOTALL | re.MULTILINE
    )
    assert block is not None, f"{skill} declares no fast path"
    return block.group(1)


#: A roadmap or plan view a fast path could name before the brief.
_OTHER_VIEW = re.compile(
    r"""view\s*=\s*["'](?:summary|detail|raw|history|version|schema|section)["']"""
    r"""|read_plan\s*\("""
)


@pytest.mark.parametrize(
    "skill", ["skills/reckon-roadmap/SKILL.md", "skills/reckon-status/SKILL.md"]
)
def test_each_skill_leads_its_fast_path_with_the_brief(skill: str) -> None:
    """Both skills name the brief before any other roadmap or plan view."""

    fast = _fast_path(skill)

    assert "brief" in fast.lower(), f"{skill}'s fast path never names the brief"
    first_brief = fast.lower().index("brief")
    other = _OTHER_VIEW.search(fast)
    assert other is not None, (
        f"{skill}'s fast path names no other view, so this test proves nothing "
        "the brief's position; name the drill-down view the brief replaces"
    )
    assert first_brief < other.start(), (
        f"{skill}'s fast path names {other.group(0)!r} before the brief, so the "
        "reader is sent to the full report first. Detail: "
        f"{fast[: other.end()]!r}"
    )
