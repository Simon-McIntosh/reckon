import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "ui" / "shell.jsx"


def _function_source(name: str) -> str:
    source = SOURCE.read_text()
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _evaluate(functions: list[str], expression: str):
    script = "\n".join(_function_source(name) for name in functions)
    result = subprocess.run(
        ["node", "-e", f"{script}\nconsole.log(JSON.stringify({expression}));"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_every_active_sprint_survives_the_overview_projection() -> None:
    state = {
        "active_sprint_id": "middle",
        "active_sprint_conflict": True,
        "sprints": [
            {"id": "first", "status": "active"},
            {"id": "middle", "status": "active"},
            {"id": "last", "status": "active"},
            {"id": "closed", "status": "done"},
        ],
    }
    result = _evaluate(
        ["projectActiveSprints"],
        f"projectActiveSprints({json.dumps(state)})",
    )

    assert [sprint["id"] for sprint in result["active"]] == [
        "first",
        "middle",
        "last",
    ]
    assert result["focus"] == "middle"
    assert result["conflict"] is True


def test_legacy_focus_and_conflict_warning_link_the_sprint_resources() -> None:
    source = SOURCE.read_text()

    assert "sprint.id === row.focus" in source
    assert "legacy focus" in source
    assert 'className="r-overview-conflict" role="alert"' in source
    assert "row.active.map(sprint => sprint.id)" in source
    assert 'href={`#sprint/${id}`}' in source


def test_unresolved_blocker_keeps_summary_owner_gated_count_and_next_action() -> None:
    blocker = {
        "id": "capacity",
        "summary": "No execution seat is available",
        "owner": "operations",
        "n": 3,
        "next": "Open one execution seat",
    }
    result = _evaluate(
        ["blockerIsUnresolved"],
        f"blockerIsUnresolved({json.dumps(blocker)})",
    )
    source = SOURCE.read_text()

    assert result is True
    assert "blocker.summary" in source
    assert "blocker.owner" in source
    assert "blocker.n" in source
    assert "blocker.next" in source


def test_resolved_blockers_do_not_enter_project_rows() -> None:
    current = {
        "project": "sample",
        "inventory": [],
        "sprints": [],
        "blockers": [
            {"id": "open", "summary": "Still open", "next": "Act"},
            {
                "id": "closed",
                "summary": "Resolved: service restored",
                "next": "Resolved with no further action",
            },
        ],
    }
    result = _evaluate(
        ["blockerIsUnresolved", "projectActiveSprints", "overviewProjectRows"],
        f"overviewProjectRows([], {json.dumps(current)}, []).flatMap(row => row.blockers.map(blocker => blocker.id))",
    )

    assert result == ["open"]
