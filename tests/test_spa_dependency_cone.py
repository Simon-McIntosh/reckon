from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.spa_browser_harness import (
    authored_shell_source,
    file_spa,
    installed_browser_or_skip,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = authored_shell_source(ROOT)


def _function_source(name: str) -> str:
    source = SOURCE.read_text(encoding="utf-8")
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


def _plan(slug: str, *, depends_on: list[str] | None = None, **extra: object):
    return {
        "slug": slug,
        "nav_key": slug,
        "title": slug.replace("-", " ").title(),
        "type": "plan",
        "status": "active",
        "effective_status": "active",
        "impl": 0.4,
        "effort_hours": 5,
        "depends_on": depends_on or [],
        **extra,
    }


def _state() -> dict[str, object]:
    plans = [
        _plan("prerequisite-a", impl=1, status="shipped"),
        _plan("prerequisite-b", impl=0.7, effort_hours=3),
        _plan(
            "focal-plan",
            depends_on=["prerequisite-a", "prerequisite-b"],
            blocks=["blocks-only"],
        ),
        _plan("dependent-plan", depends_on=["focal-plan"]),
        _plan("blocks-only", blocks=["focal-plan"]),
        _plan("isolated-plan"),
        _plan("evidence-owner"),
    ]
    evidence = [
        {
            "slug": f"receipt-{index}",
            "nav_key": f"evidence:receipt-{index}",
            "title": f"Receipt {index}",
            "type": "evidence",
            "gate": "passed",
        }
        for index in range(3)
    ]
    research = {
        "slug": "research-input",
        "nav_key": "research:research-input",
        "title": "Research input",
        "type": "research",
        "informs": ["focal-plan"],
    }
    inventory = [*plans, *evidence, research]
    return {
        "project": "sample",
        "projects": [{"project": "sample", "plans_count": len(plans)}],
        "inventory": inventory,
        "plans": {item["nav_key"]: item for item in inventory},
        "attachment_relations": [
            {"source": item["nav_key"], "target": "evidence-owner"} for item in evidence
        ],
        "sprints": [],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "active_sprints": [],
        "active_sprint_conflict": False,
    }


def test_cone_rows_use_depends_on_and_its_reverse_only() -> None:
    inventory = _state()["inventory"]
    expression = (
        f"(() => {{ const cone = dependencyConeRows({json.dumps(inventory)}, 'focal-plan'); "
        "return { up: cone.prerequisites.map(item => item.slug), "
        "down: cone.dependents.map(item => item.slug), label: dependencyConeLabel(cone) }; })()"
    )

    result = _evaluate(
        ["dependencyRefSlug", "dependencyConeRows", "dependencyConeLabel"],
        expression,
    )

    assert result == {
        "up": ["prerequisite-a", "prerequisite-b"],
        "down": ["dependent-plan"],
        "label": "2 ↑ 1 ↓",
    }


def test_cone_is_closed_until_its_labelled_toggle_is_used(tmp_path: Path) -> None:
    probe = r"""(async () => {
      const toggle = document.querySelector('.r-dependency-cone-toggle');
      const before = {
        expanded: toggle?.getAttribute('aria-expanded'),
        columns: document.querySelectorAll('.r-dependency-cone-columns').length,
        label: document.querySelector('.r-dependency-cone-count')?.textContent.trim(),
      };
      toggle.click();
      await new Promise(resolve => setTimeout(resolve, 30));
      const slugs = direction => [...document.querySelectorAll(`[data-cone-column="${direction}"] [data-cone-edge-card="true"]`)]
        .map(card => card.dataset.coneSlug);
      return {
        before,
        expanded: toggle.getAttribute('aria-expanded'),
        up: slugs('up'),
        down: slugs('down'),
        columns: document.querySelectorAll('.r-dependency-cone-column').length,
      };
    })()"""

    with file_spa(
        tmp_path,
        installed_browser_or_skip(),
        _state(),
        project="sample",
        route="#plan/focal-plan",
    ) as spa:
        result = spa.run_probe(
            probe,
            ready_expression="Boolean(document.querySelector('.r-dependency-cone-toggle'))",
        )

    assert result == {
        "before": {"expanded": "false", "columns": 0, "label": "2 ↑ 1 ↓"},
        "expanded": "true",
        "up": ["prerequisite-a", "prerequisite-b"],
        "down": ["dependent-plan"],
        "columns": 3,
    }


def test_isolated_plan_toggle_reads_standalone_with_reduced_opacity(
    tmp_path: Path,
) -> None:
    probe = r"""(() => {
      const cone = document.querySelector('.r-dependency-cone');
      const toggle = document.querySelector('.r-dependency-cone-toggle');
      return {
        label: document.querySelector('.r-dependency-cone-count')?.textContent.trim(),
        standalone: cone?.classList.contains('is-standalone'),
        opacity: Number(getComputedStyle(toggle).opacity),
      };
    })()"""

    with file_spa(
        tmp_path,
        installed_browser_or_skip(),
        _state(),
        project="sample",
        route="#plan/isolated-plan",
    ) as spa:
        result = spa.run_probe(
            probe,
            ready_expression="Boolean(document.querySelector('.r-dependency-cone-toggle'))",
        )

    assert result["label"] == "standalone"
    assert result["standalone"] is True
    assert result["opacity"] < 1


def test_evidence_stays_in_reader_provenance_and_out_of_the_cone(
    tmp_path: Path,
) -> None:
    probe = r"""(async () => {
      document.querySelector('.r-dependency-cone-toggle').click();
      await new Promise(resolve => setTimeout(resolve, 30));
      return {
        edgeCards: document.querySelectorAll('[data-cone-edge-card="true"]').length,
        evidenceProvenance: document.querySelectorAll('[data-attachment-type="evidence"] .r-reader-attachment-entries > button').length,
      };
    })()"""

    with file_spa(
        tmp_path,
        installed_browser_or_skip(),
        _state(),
        project="sample",
        route="#plan/evidence-owner",
    ) as spa:
        result = spa.run_probe(
            probe,
            ready_expression=(
                "Boolean(document.querySelector('.r-dependency-cone-toggle') "
                "&& document.querySelector('[data-attachment-type=\"evidence\"]'))"
            ),
        )

    assert result == {"edgeCards": 0, "evidenceProvenance": 3}
