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


def _resource(
    kind: str,
    *,
    slug: str | None = None,
    title: str | None = None,
    created: int = 100,
    edited: str = "2020-01-03T12:00:00",
    **extra: object,
) -> dict[str, object]:
    resource_slug = slug or f"shared-{kind}"
    return {
        "slug": resource_slug,
        "nav_key": resource_slug,
        "title": title or f"Shared {kind}",
        "summary": f"A shared {kind} palette result.",
        "type": kind,
        "created": created,
        "edited": edited,
        **extra,
    }


def _composed_state() -> dict[str, object]:
    inventory = [
        _resource(
            "plan",
            status="active",
            effective_status="active",
            impl=0.4,
            effort_hours=5,
            sprint="current",
        ),
        _resource(
            "plan",
            slug="finished-plan",
            title="Finished plan",
            summary="A completed plan outside the palette query.",
            created=200,
            edited="2020-01-02T12:00:00",
            status="shipped",
            effective_status="shipped",
            impl=1,
            effort_hours=3,
            sprint="current",
        ),
        _resource("research", verdict="supports"),
        _resource("evidence", gate="running", verdict="running"),
        _resource(
            "figure",
            dims="640 \u00d7 480",
            href="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==",
        ),
    ]
    return {
        "project": "sample",
        "projects": [{"project": "sample", "plans_count": len(inventory)}],
        "inventory": inventory,
        "plans": {item["nav_key"]: item for item in inventory},
        "sprints": [],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "active_sprints": [],
        "active_sprint_conflict": False,
    }


def test_index_row_derivation_hides_completed_plans_and_passed_evidence() -> None:
    plans = [
        _resource("plan", slug="open", status="active", impl=0.5),
        _resource("plan", slug="done", status="shipped", impl=1),
    ]
    evidence = [
        _resource("evidence", slug="passing", gate="passed"),
        _resource("evidence", slug="running", gate="running"),
    ]
    functions = ["sortItems", "artifactState", "artifactIsDone", "artifactIndexRows"]

    visible_plans = _evaluate(
        functions,
        f"artifactIndexRows({json.dumps(plans)}, 'plan', 'edited', 'desc', '', true).map(item => item.slug)",
    )
    shipped_plans = _evaluate(
        functions,
        f"artifactIndexRows({json.dumps(plans)}, 'plan', 'edited', 'desc', 'shipped', true).map(item => item.slug)",
    )
    visible_evidence = _evaluate(
        functions,
        f"artifactIndexRows({json.dumps(evidence)}, 'evidence', 'edited', 'desc', '', true).map(item => item.slug)",
    )

    assert visible_plans == ["open"]
    assert shipped_plans == ["done"]
    assert visible_evidence == ["running"]


def test_all_artifact_feeds_and_palette_render_from_one_inventory(
    tmp_path: Path,
) -> None:
    probe = r"""(async () => {
      const waitFor = async predicate => {
        const deadline = performance.now() + 2500;
        while (performance.now() < deadline) {
          if (predicate()) return true;
          await new Promise(resolve => setTimeout(resolve, 25));
        }
        return false;
      };
      const navigate = async (hash, kind) => {
        window.location.hash = hash;
        await waitFor(() => document.querySelector(".r-canvas-view")?.dataset.artifactKind === kind);
        await waitFor(() => Boolean(document.querySelector(".r-artifact-index")));
      };
      const feedSnapshot = kind => {
        const rows = [...document.querySelectorAll(".r-artifact-row")];
        return {
          kind,
          rows: rows.map(row => ({
            slug: row.dataset.artifactSlug,
            created: [...row.querySelectorAll(".r-artifact-stamps span")].some(stamp => stamp.textContent.trim().startsWith("created ")),
            edited: [...row.querySelectorAll(".r-artifact-stamps span")].some(stamp => stamp.textContent.trim().startsWith("edited ")),
          })),
          statusFilters: document.querySelectorAll(".r-feed-status-filters").length,
        };
      };

      await navigate("#plans", "plan");
      const hideDone = document.querySelector(".r-hide-done input");
      hideDone.click();
      await waitFor(() => document.querySelectorAll(".r-artifact-row").length === 2);
      const editedFirst = document.querySelector(".r-artifact-row")?.dataset.artifactSlug;
      [...document.querySelectorAll(".r-sort-segments button")].find(button => button.textContent.trim() === "Created").click();
      await waitFor(() => document.querySelector(".r-artifact-row")?.dataset.artifactSlug === "finished-plan");
      const createdFirst = document.querySelector(".r-artifact-row")?.dataset.artifactSlug;
      const plans = feedSnapshot("plan");

      hideDone.click();
      await waitFor(() => !document.querySelector('[data-artifact-slug="finished-plan"]'));
      const hiddenDoneSlugs = [...document.querySelectorAll(".r-artifact-row")].map(row => row.dataset.artifactSlug);
      [...document.querySelectorAll(".r-feed-status-filters button")].find(button => button.textContent.trim().startsWith("shipped ")).click();
      await waitFor(() => Boolean(document.querySelector('[data-artifact-slug="finished-plan"]')));
      const shippedSlugs = [...document.querySelectorAll(".r-artifact-row")].map(row => row.dataset.artifactSlug);
      [...document.querySelectorAll(".r-feed-status-filters button")].find(button => button.textContent.trim().startsWith("blocked ")).click();
      await waitFor(() => Boolean(document.querySelector(".r-artifact-empty")));
      const emptyText = document.querySelector(".r-artifact-empty").textContent.trim();

      const snapshots = [plans];
      for (const [hash, kind] of [["#research", "research"], ["#evidence", "evidence"], ["#figures", "figure"]]) {
        await navigate(hash, kind);
        snapshots.push(feedSnapshot(kind));
      }
      const figure = document.querySelector(".r-artifact-row-figure");
      const figureResult = {
        thumbnail: Boolean(figure?.querySelector(".r-artifact-thumb[width='50'][height='34']")),
        dimensions: figure?.querySelector(".r-artifact-dimensions")?.textContent.trim() || "",
      };

      document.querySelector(".r-topbar-search").click();
      await waitFor(() => Boolean(document.querySelector(".r-cmdk input")));
      const input = document.querySelector(".r-cmdk input");
      const placeholder = input.placeholder;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(input, "shared");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await waitFor(() => document.querySelectorAll(".r-cmdk .item").length === 4);
      const paletteLabels = [...document.querySelectorAll(".r-cmdk .item")]
        .map(row => row.querySelectorAll(".meta")[1]?.textContent.trim().split(" · ")[0] || "");

      return {
        snapshots,
        editedFirst,
        createdFirst,
        hiddenDoneSlugs,
        shippedSlugs,
        emptyText,
        figureResult,
        placeholder,
        paletteRows: document.querySelectorAll(".r-cmdk .item").length,
        paletteLabels,
      };
    })()"""

    with file_spa(
        tmp_path,
        installed_browser_or_skip(),
        _composed_state(),
        project="sample",
        route="#plans",
    ) as spa:
        result = spa.run_probe(
            probe,
            viewport=(1374, 900),
            ready_expression='Boolean(document.querySelector(".r-artifact-row"))',
        )

    snapshots = {snapshot["kind"]: snapshot for snapshot in result["snapshots"]}
    assert set(snapshots) == {"plan", "research", "evidence", "figure"}
    assert all(snapshot["rows"] for snapshot in snapshots.values())
    assert all(
        row["created"] and row["edited"]
        for snapshot in snapshots.values()
        for row in snapshot["rows"]
    )
    assert snapshots["plan"]["statusFilters"] == 1
    assert all(
        snapshots[kind]["statusFilters"] == 0
        for kind in ("research", "evidence", "figure")
    )
    assert result["editedFirst"] == "shared-plan"
    assert result["createdFirst"] == "finished-plan"
    assert "finished-plan" not in result["hiddenDoneSlugs"]
    assert result["shippedSlugs"] == ["finished-plan"]
    assert "plans" in result["emptyText"].lower()
    assert "sample" in result["emptyText"].lower()
    assert result["figureResult"] == {
        "thumbnail": True,
        "dimensions": "640 \u00d7 480",
    }
    assert result["paletteRows"] == 4
    assert result["paletteLabels"] == [
        "Plans",
        "Research",
        "Evidence",
        "Figures",
    ]
    assert all(
        kind in result["placeholder"].lower()
        for kind in ("plans", "research", "evidence", "figures")
    )
