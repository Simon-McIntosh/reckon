from __future__ import annotations

from pathlib import Path

from tests.spa_browser_harness import file_spa, installed_browser_or_skip

ROOT = Path(__file__).resolve().parents[1]
PALETTE = ROOT / "docs" / "ui" / "shell-palette.jsx"


def _resource(kind: str) -> dict[str, object]:
    return {
        "slug": f"shared-{kind}",
        "title": f"Shared {kind}",
        "summary": f"A shared {kind} palette result.",
        "type": kind,
        "status": "active",
        "effective_status": "active",
        "impl": 0.25,
        "sprint": "current",
    }


def _composed_state() -> dict[str, object]:
    inventory = [_resource(kind) for kind in ("plan", "research", "evidence", "figure")]
    return {
        "project": "reckon",
        "projects": [{"project": "reckon", "plans_count": len(inventory)}],
        "inventory": inventory,
        "plans": {item["slug"]: item for item in inventory},
        "sprints": [],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "active_sprints": [],
        "active_sprint_conflict": False,
    }


def test_matching_palette_results_render_without_unmounting_the_shell(
    tmp_path: Path,
) -> None:
    preload = r"""
window.__referenceErrors = [];
window.__consoleMessages = [];
window.addEventListener("error", event => {
  if (event.error instanceof ReferenceError) window.__referenceErrors.push(event.error.message);
});
window.addEventListener("unhandledrejection", event => {
  if (event.reason instanceof ReferenceError) window.__referenceErrors.push(event.reason.message);
});
for (const method of ["error", "warn", "log"]) {
  const original = console[method].bind(console);
  console[method] = (...args) => {
    window.__consoleMessages.push(args.map(String).join(" "));
    original(...args);
  };
}
"""
    probe = r"""(async () => {
      const waitFor = async predicate => {
        const deadline = performance.now() + 2000;
        while (performance.now() < deadline) {
          if (predicate()) return true;
          await new Promise(resolve => setTimeout(resolve, 25));
        }
        return false;
      };

      document.querySelector(".r-topbar-search").click();
      const opened = await waitFor(() => Boolean(document.querySelector(".r-cmdk")));
      const input = document.querySelector(".r-cmdk input");
      if (input) {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
        setter.call(input, "shared");
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      await waitFor(() => document.querySelectorAll(".r-cmdk .item").length === 4);

      const rows = [...document.querySelectorAll(".r-cmdk .item")];
      const renderedLabels = rows.map(row => row.querySelectorAll(".meta")[1]?.textContent.trim() || "");
      const expectedLabels = window.STATE.inventory.map(item =>
        window.ReckonShell.plans.paletteKindLabel(item.type)
      );
      return {
        viewportWidth: innerWidth,
        opened,
        palettePresent: Boolean(document.querySelector(".r-cmdk")),
        rowCount: rows.length,
        renderedLabels,
        expectedLabels,
        appMounted: Boolean(document.querySelector(".r-app")),
        referenceErrors: window.__referenceErrors,
        referenceConsoleMessages: window.__consoleMessages.filter(message =>
          message.includes("ReferenceError")
        ),
      };
    })()"""

    with file_spa(
        tmp_path,
        installed_browser_or_skip(),
        _composed_state(),
        route="#plans",
    ) as spa:
        result = spa.run_probe(
            probe,
            viewport=(1374, 900),
            ready_expression=(
                'Boolean(document.querySelector(".r-app") '
                '&& document.querySelector(".r-topbar-search"))'
            ),
            preload_expression=preload,
        )

    assert result["viewportWidth"] == 1374
    assert result["opened"] is True
    assert result["palettePresent"] is True
    assert result["rowCount"] == 4
    assert result["renderedLabels"] == [
        f"{label} · reckon · active" for label in result["expectedLabels"]
    ]
    assert result["appMounted"] is True
    assert result["referenceErrors"] == []
    assert result["referenceConsoleMessages"] == []


def test_palette_label_helper_calls_use_the_published_namespace() -> None:
    source = PALETTE.read_text(encoding="utf-8")

    assert source.count("paletteKindLabel(") == 1
    assert source.count("window.ReckonShell.plans.paletteKindLabel(") == 1
