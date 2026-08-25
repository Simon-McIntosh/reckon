import re
from pathlib import Path

from tests.test_spa_rendered_semantics import _rendered_probe


ROOT = Path(__file__).resolve().parents[1]
STYLES = (ROOT / "docs" / "ui" / "overview.css").read_text()
SHELL = (ROOT / "docs" / "ui" / "shell.jsx").read_text()


def _declarations(selector: str) -> dict[str, str]:
    match = re.search(
        rf"(?m)(?<!,\n)^{re.escape(selector)}[ \t]*\{{([^}}]+)\}}",
        STYLES,
    )
    assert match, f"missing CSS rule for {selector}"
    return {
        key.strip(): value.strip()
        for declaration in match.group(1).split(";")
        if ":" in declaration
        for key, value in [declaration.split(":", 1)]
    }


def test_overview_owns_the_canvas_scroll_geometry() -> None:
    assert 'route.view === "cockpit" ? "r-overview-container" : ""' in SHELL
    assert 'route.view === "cockpit" ? "r-overview-view" : ""' in SHELL
    container = _declarations(".r-overview-container")
    view = _declarations(".r-reader-with-attachments > .r-body.r-overview-view")

    assert container["display"] == "flex"
    assert container["flex"] == "1"
    assert container["min-height"] == "0"
    assert view["flex"] == "1"
    assert view["overflow"] == "auto"
    assert view["padding"] == "20px 26px 40px"


def test_statistics_are_one_bordered_row_with_fixed_cell_geometry() -> None:
    strip = _declarations(".r-overview-stats")
    cell = _declarations(".r-overview-stat")

    assert strip == {
        "display": "flex",
        "align-items": "center",
        "gap": "0",
        "margin-bottom": "18px",
        "border": "1px solid var(--line)",
        "border-radius": "8px",
        "background": "var(--bg)",
        "overflow": "hidden",
    }
    assert cell["padding"] == "11px 16px"
    assert cell["min-width"] == "130px"
    assert cell["border-right"] == "1px solid var(--line)"


def test_blockers_keep_the_declared_card_and_type_scale() -> None:
    cards = _declarations(".r-overview-blocker-list")
    blocker = _declarations(".r-overview-blockers article")
    summary = _declarations(".r-overview-blocker-summary")
    next_action = _declarations(".r-overview-blocker-next")

    assert cards["display"] == "flex"
    assert cards["gap"] == "8px"
    assert blocker["padding"] == "12px 14px"
    assert blocker["border"] == "1px solid var(--bad)"
    assert summary["font-size"] == "13.5px"
    assert summary["max-width"] == "110ch"
    assert next_action["padding"] == "6px 9px"
    assert next_action["font"] == "11.5px var(--mono)"


def test_projects_table_matches_the_canvas_columns_and_row_paddings() -> None:
    shared = _declarations(".r-overview-project-head,\n.r-overview-project-row")
    head = _declarations(".r-overview-project-head")
    row = _declarations(".r-overview-project-row")
    project = _declarations(".r-overview-project-row > strong")
    sprint = _declarations(".r-overview-sprints")

    assert shared["display"] == "grid"
    assert shared["grid-template-columns"] == (
        "130px minmax(0, 1fr) 70px 70px 60px 60px"
    )
    assert shared["gap"] == "12px"
    assert head["padding"] == "8px 12px"
    assert head["font"] == "10px var(--mono)"
    assert row["align-items"] == "center"
    assert row["padding"] == "10px 12px"
    assert project["font-size"] == "12.5px"
    assert sprint["font-size"] == "13px"


def test_empty_overview_sections_omit_their_heading_at_canvas_width(
    tmp_path: Path,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#cockpit",
        wait_selector=".r-overview-view .r-ck-h",
        probe="""(async () => {
          const headings = [...document.querySelectorAll(".r-overview-view .r-ck-h")];
          const emptyHeadings = headings.filter(heading => {
            const content = heading.nextElementSibling;
            return !content || content.getBoundingClientRect().height === 0;
          }).map(heading => heading.textContent.trim());
          const populatedHeadings = headings
            .filter(heading => {
              const content = heading.nextElementSibling;
              return content && content.getBoundingClientRect().height > 0;
            })
            .map(heading => heading.textContent.trim());
          const previousState = window.STATE;
          const populatedHost = document.createElement("div");
          populatedHost.id = "populated-overview";
          document.body.appendChild(populatedHost);
          window.STATE = {
            ...previousState,
            projects: [{
              ...(previousState.projects?.[0] || {}),
              milestones: [{ id: "delivery", name: "Delivery", status: "active", pct: 50 }],
            }],
          };
          const populatedRoot = ReactDOM.createRoot(populatedHost);
          populatedRoot.render(React.createElement(CockpitBody, {
            onNav: () => {}, projects: [], fleetRuns: [], mountedProjectCount: 0,
          }));
          for (let attempt = 0; attempt < 100; attempt++) {
            if (populatedHost.querySelector(".r-ms-tile")) break;
            await new Promise(resolve => setTimeout(resolve, 20));
          }
          const populatedMilestoneHeading = [...populatedHost.querySelectorAll(".r-ck-h")]
            .find(heading => heading.textContent.trim() === "Milestones");
          const populatedMilestones = Boolean(
            populatedMilestoneHeading
            && populatedMilestoneHeading.nextElementSibling?.querySelector(".r-ms-tile")
          );
          populatedRoot.unmount();
          populatedHost.remove();
          window.STATE = previousState;
          return {
            ok: innerWidth === 1374
              && emptyHeadings.length === 0
              && populatedHeadings.includes("Fleet")
              && populatedMilestones,
            viewportWidth: innerWidth,
            emptyHeadings,
            populatedHeadings,
            populatedMilestones,
          };
        })()""",
        remove_signal="""document.querySelectorAll(
          ".r-overview-fleet > :not(.r-ck-h)"
        ).forEach(node => node.remove())""",
    )

    assert result["baseline"]["ok"] is True
    assert result["baseline"]["viewportWidth"] == 1374
    assert result["baseline"]["emptyHeadings"] == []
    assert "Milestones" not in result["baseline"]["populatedHeadings"]
    assert "Fleet" in result["baseline"]["populatedHeadings"]
    assert result["baseline"]["populatedMilestones"] is True
    assert result["removed"]["ok"] is False
    assert result["removed"]["emptyHeadings"] == ["Fleet"]
