from __future__ import annotations

from pathlib import Path

import pytest

from tests.spa_browser_harness import file_spa, installed_browser_or_skip

PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="


def _resource(
    kind: str,
    name: str,
    created: int,
    *,
    status: str = "active",
) -> dict[str, object]:
    slug = f"{kind}-{name}.png" if kind == "figure" else f"{kind}-{name}"
    nav_key = slug if kind == "plan" else f"{kind}:{slug}"
    item: dict[str, object] = {
        "slug": slug,
        "nav_key": nav_key,
        "title": f"{kind.title()} {name.title()}",
        "summary": f"The {name} {kind} fixture.",
        "type": kind,
        "created": created,
        "edited": f"2026-09-{created // 100:02d}T12:00:00",
    }
    if kind == "plan":
        item.update(
            status=status,
            effective_status=status,
            impl=0.25,
            effort_hours=3,
        )
    elif kind == "research":
        item["verdict"] = "supports"
    elif kind == "evidence":
        item.update(gate="running", verdict="running")
    else:
        item.update(href=PIXEL, dims="640 x 480")
    return item


def _composed_state(kind: str) -> dict[str, object]:
    inventory = [
        _resource(kind, "first", 100),
        _resource(kind, "second", 200),
        _resource(kind, "third", 300),
    ]
    if kind == "plan":
        inventory.append(_resource(kind, "filtered-out", 400, status="blocked"))
    return {
        "project": f"queue-{kind}",
        "projects": [],
        "inventory": inventory,
        "plans": {item["nav_key"]: item for item in inventory},
        "sprints": [],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "active_sprints": [],
        "active_sprint_conflict": False,
        "attachment_relations": [],
    }


@pytest.mark.parametrize("kind", ["plan", "research", "evidence", "figure"])
def test_reader_steps_the_filtered_sorted_rows_published_by_each_index(
    tmp_path: Path,
    kind: str,
) -> None:
    probe = r"""(async () => {
      const kind = document.querySelector(".r-canvas-view").dataset.artifactKind;
      const waitFor = async predicate => {
        const deadline = performance.now() + 3000;
        while (performance.now() < deadline) {
          if (predicate()) return true;
          await new Promise(resolve => setTimeout(resolve, 20));
        }
        return false;
      };
      const rows = () => [...document.querySelectorAll(".r-artifact-row")];
      if (kind === "plan") {
        const active = [...document.querySelectorAll(".r-feed-status-filters button")]
          .find(button => button.textContent.trim().startsWith("active "));
        active.click();
        await waitFor(() => rows().length === 3);
      }
      const created = [...document.querySelectorAll(".r-artifact-index .r-sort-segments button")]
        .find(button => button.textContent.trim() === "Created");
      created.click();
      await waitFor(() => rows().length === 3 && rows()[0].dataset.artifactSlug.includes("third"));

      const renderedOrder = rows().map(row => row.dataset.artifactSlug);
      const readouts = [];
      const visited = [];
      const observedReadouts = [];
      const captureReadout = () => {
        const value = document.querySelector(".r-reading-position")?.textContent.trim();
        if (value) observedReadouts.push(value);
      };
      const observer = new MutationObserver(captureReadout);
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });

      rows()[0].click();
      await waitFor(() => document.querySelector(".r-reading-position")?.textContent.trim() === "1 / 3");
      captureReadout();
      readouts.push(document.querySelector(".r-reading-position")?.textContent.trim() || "");
      visited.push(document.querySelector(".r-canvas-view")?.dataset.artifactSelection || "");
      const previousAtStart = document.querySelector('[aria-label="Previous item in rendered list"]');
      const nextAtStart = document.querySelector('[aria-label="Next item in rendered list"]');
      const start = { previousDisabled: previousAtStart.disabled, nextDisabled: nextAtStart.disabled };

      nextAtStart.click();
      await waitFor(() => document.querySelector(".r-reading-position")?.textContent.trim() === "2 / 3");
      captureReadout();
      readouts.push(document.querySelector(".r-reading-position")?.textContent.trim() || "");
      visited.push(document.querySelector(".r-canvas-view")?.dataset.artifactSelection || "");

      document.querySelector('[aria-label="Next item in rendered list"]').click();
      await waitFor(() => document.querySelector(".r-reading-position")?.textContent.trim() === "3 / 3");
      captureReadout();
      readouts.push(document.querySelector(".r-reading-position")?.textContent.trim() || "");
      visited.push(document.querySelector(".r-canvas-view")?.dataset.artifactSelection || "");
      const previousAtEnd = document.querySelector('[aria-label="Previous item in rendered list"]');
      const nextAtEnd = document.querySelector('[aria-label="Next item in rendered list"]');
      const end = { previousDisabled: previousAtEnd.disabled, nextDisabled: nextAtEnd.disabled };

      previousAtEnd.click();
      await waitFor(() => document.querySelector(".r-reading-position")?.textContent.trim() === "2 / 3");
      captureReadout();
      readouts.push(document.querySelector(".r-reading-position")?.textContent.trim() || "");
      visited.push(document.querySelector(".r-canvas-view")?.dataset.artifactSelection || "");
      observer.disconnect();

      const withoutKindPrefix = value => value.startsWith(`${kind}:`)
        ? value.slice(kind.length + 1)
        : value;
      return {
        renderedOrder: renderedOrder.map(withoutKindPrefix),
        visited: visited.map(withoutKindPrefix),
        readouts,
        start,
        end,
        observedReadouts,
        zeroOfZeroSeen: observedReadouts.includes("0 / 0"),
      };
    })()"""

    with file_spa(
        tmp_path,
        installed_browser_or_skip(),
        _composed_state(kind),
        project=f"queue-{kind}",
        route=f"#{'plans' if kind == 'plan' else 'figures' if kind == 'figure' else kind}",
    ) as spa:
        result = spa.run_probe(
            probe,
            viewport=(1374, 900),
            ready_expression=(
                'document.querySelectorAll(".r-artifact-row").length >= 3'
            ),
        )

    expected = [f"{kind}-third", f"{kind}-second", f"{kind}-first"]
    if kind == "figure":
        expected = [f"{value}.png" for value in expected]
    assert result["renderedOrder"] == expected
    assert result["visited"] == [expected[0], expected[1], expected[2], expected[1]]
    assert result["readouts"] == ["1 / 3", "2 / 3", "3 / 3", "2 / 3"]
    assert result["start"] == {
        "previousDisabled": True,
        "nextDisabled": False,
    }
    assert result["end"] == {
        "previousDisabled": False,
        "nextDisabled": True,
    }
    assert result["observedReadouts"]
    assert result["zeroOfZeroSeen"] is False
