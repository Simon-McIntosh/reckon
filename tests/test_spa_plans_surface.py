import json
import subprocess
from pathlib import Path

from tests.spa_browser_harness import authored_shell_source

ROOT = Path(__file__).resolve().parents[1]
SOURCE = authored_shell_source(ROOT)


def _function_source(name: str) -> str:
    source = SOURCE.read_text()
    start = source.index(f"function {name}(")
    brace = source.index(") {", start) + 2
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


def test_plan_list_excludes_research_and_evidence() -> None:
    items = [
        {"slug": "plan", "type": "plan", "last": "2026-08-24"},
        {"slug": "study", "type": "research", "last": "2026-08-25"},
        {"slug": "receipt", "type": "evidence", "last": "2026-08-25"},
    ]
    result = _evaluate(
        ["sortItems"],
        f"sortItems({json.dumps(items)}, 'edited', 'desc').map(item => item.slug)",
    )

    assert result == ["plan"]


def test_typed_attachments_are_reachable_from_plan_and_attachment_selection() -> None:
    state = {
        "plans": {
            "work": {"slug": "work", "type": "plan"},
            "research:study": {
                "nav_key": "research:study",
                "slug": "study",
                "type": "research",
            },
            "evidence:receipt": {
                "nav_key": "evidence:receipt",
                "slug": "receipt",
                "type": "evidence",
            },
        },
        "attachment_relations": [
            {"relation": "informs", "source": "research:study", "target": "work"},
            {
                "relation": "verifies",
                "source": "evidence:receipt",
                "target": "work#checks",
            },
        ],
    }
    expression = f"[attachmentGroups({json.dumps(state)}, 'work'), attachmentGroups({json.dumps(state)}, 'research:study')]"
    plan_view, attachment_view = _evaluate(["attachmentGroups"], expression)

    assert plan_view["planKey"] == "work"
    assert [item["slug"] for item in plan_view["research"]] == ["study"]
    assert [item["slug"] for item in plan_view["evidence"]] == ["receipt"]
    assert attachment_view == plan_view


def test_shell_passes_the_canonical_attachment_groups_into_the_reader() -> None:
    shell = SOURCE.read_text()
    plan = (ROOT / "docs" / "ui" / "plan.jsx").read_text()

    assert shell.count("function attachmentGroups(") == 1
    assert "attachmentGroups={attachmentGroups}" in shell
    assert "attachmentGroups={attachmentGroups(state, selected)}" in shell
    assert "M.attachment_relations" not in plan
    assert "readerAttachments" not in plan
    assert "function AttachmentRail(" not in shell


def test_status_transition_reports_the_real_open_gate_count() -> None:
    plan = {
        "workflow_status": "active",
        "effective_status": "blocked",
        "gates": [
            {"status": "open", "verdict": "pending"},
            {"status": "closed", "verdict": "passed", "passed": True},
        ],
    }
    count = _evaluate(["openGateCount"], f"openGateCount({json.dumps(plan)})")
    source = SOURCE.read_text()

    assert count == 1
    assert "authored !== effective" in source
    assert '"gate-state-heading"' in source
    assert 'open {gates === 1 ? "gate" : "gates"}' in source


def test_artifact_rows_publish_progress_stamps_and_status() -> None:
    source = SOURCE.read_text()
    index = _function_source("ArtifactIndex")

    assert 'className="r-artifact-progress"' in index
    assert 'className="r-artifact-stamps"' in index
    assert "className={`r-artifact-status-chip ${itemState}`}" in index
    assert "className={`r-artifact-verdict r-artifact-verdict-${itemState}`}" in index
    assert 'className="r-artifact-thumb"' in index
    assert 'className="r-artifact-dimensions"' in index
    assert 'className="r-sort-segments"' in source
    assert "created {artifactStamp(item.created, true)}" in index
    assert "edited {artifactStamp(item.edited || item.last)}" in index
