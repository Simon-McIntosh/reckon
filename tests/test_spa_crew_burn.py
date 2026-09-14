from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from reckon import serve

ROOT = Path(__file__).resolve().parents[1]
CREW = ROOT / "docs" / "ui" / "crew.jsx"

NODE_PRELUDE = r"""
globalThis.window = globalThis;
const noop = () => {};
globalThis.React = {
  createElement(type, props, ...children) { return { type, props: props || {}, children }; },
  Fragment: Symbol("Fragment"),
  useState(value) { return [typeof value === "function" ? value() : value, noop]; },
  useEffect() {},
};
globalThis.navigator = { clipboard: { writeText: noop } };
globalThis.fetch = async () => ({ ok: true, json: async () => ({ runs: [] }) });
window.setInterval = noop;
window.clearInterval = noop;
window.flashSaved = noop;

function walk(node, visit) {
  if (node == null || node === false || node === true) return;
  if (Array.isArray(node)) { node.forEach(child => walk(child, visit)); return; }
  if (typeof node !== "object") return;
  visit(node);
  for (const child of node.children || []) walk(child, visit);
}

function hasClass(node, name) {
  return String(node?.props?.className || "").split(/\s+/).includes(name);
}

function findAll(node, predicate) {
  const matches = [];
  walk(node, candidate => { if (predicate(candidate)) matches.push(candidate); });
  return matches;
}

function textContent(node) {
  if (node == null || node === false || node === true) return "";
  if (Array.isArray(node)) return node.map(textContent).join("");
  if (typeof node === "string" || typeof node === "number") return String(node);
  return (node.children || []).map(textContent).join("");
}
"""

TEST_EXPORTS = """
window.__burnTest = {
  CrewView,
  CrewQuotaMeter,
  CrewQuotaMeters,
};
"""

# Shared timestamps: observed mid-window, a seven-day reset at the far edge.
OBSERVED_AT = "2026-09-14T12:00:00Z"
RESETS_AT = "2026-09-21T00:00:00Z"
PERIOD_MINUTES = 10080

# A backend at 20% used, burning 10.3x: the projection precedes the reset.
# Self-consistent with the Python derivation (projected = observed +
# remaining / burn): observed + 561600s / 10.3 lands at 2026-09-15T03:09Z,
# inside the window and before the reset.
UNSUSTAINABLE = {
    "backend": "codex",
    "used_percent": 20,
    "rate_limit_period_minutes": PERIOD_MINUTES,
    "resets_at": RESETS_AT,
    "observed_at": OBSERVED_AT,
    "burn_multiple": 10.3,
    "projected_exhaustion_at": "2026-09-15T03:09:00Z",
    "rate_windows": [
        {"period_minutes": 60, "burn_multiple": 10.3, "observed_at": OBSERVED_AT},
        {
            "period_minutes": PERIOD_MINUTES,
            "burn_multiple": 1.71,
            "observed_at": OBSERVED_AT,
        },
    ],
}

# The same 20% position at a sustainable rate (burn <= 1): the projection is
# the reset itself, so the mark sits at the reset edge and the bar is calm.
SUSTAINABLE = {
    "backend": "codex",
    "used_percent": 20,
    "rate_limit_period_minutes": PERIOD_MINUTES,
    "resets_at": RESETS_AT,
    "observed_at": OBSERVED_AT,
    "burn_multiple": 0.7,
    "projected_exhaustion_at": RESETS_AT,
    "rate_windows": [
        {"period_minutes": 60, "burn_multiple": 7.5, "observed_at": OBSERVED_AT},
        {
            "period_minutes": PERIOD_MINUTES,
            "burn_multiple": 0.7,
            "observed_at": OBSERVED_AT,
        },
    ],
}

# A lane whose payload reports no window at all: no period, no reset, no
# position. It must render no meter and no default period.
NO_WINDOW = {
    "backend": "spark",
    "headroom": "unknown",
    "detail": "backend reports token usage but no headroom",
}

ONLY_WINDOW = {
    "backend": "codex",
    "used_percent": 20,
    "rate_limit_period_minutes": PERIOD_MINUTES,
    "resets_at": RESETS_AT,
    "observed_at": OBSERVED_AT,
    "burn_multiple": 1.71,
    "projected_exhaustion_at": "2026-09-19T00:00:00Z",
    "rate_windows": [
        {
            "period_minutes": PERIOD_MINUTES,
            "burn_multiple": 1.71,
            "observed_at": OBSERVED_AT,
        },
    ],
}


def _render(readings, *, surface: bool = False) -> dict:
    """Probe the CrewQuotaMeters region (or the full Crew surface) in node."""

    source = CREW.read_text(encoding="utf-8") + TEST_EXPORTS
    compiled = serve.compile_jsx(source, filename="crew-quota-probe.jsx").decode()
    if surface:
        render = """
window.STATE = {};
const region = window.__burnTest.CrewView({
  visibleProjects: [], mountedProjectCount: 0, selectedProject: null, quota: readings,
});
const regionIsNull = false;
"""
    else:
        render = """
const region = window.__burnTest.CrewQuotaMeters({ readings });
const regionIsNull = region === null;
"""
    script = "\n".join(
        (
            NODE_PRELUDE,
            compiled,
            f"const readings = {json.dumps(readings)};",
            render,
            """
const meters = regionIsNull ? [] : findAll(region, node => hasClass(node, "r-crew-meter"));
const figures = regionIsNull
  ? []
  : findAll(region, node => hasClass(node, "r-crew-figure"))
      .map(node => ({ text: textContent(node), observedAt: node.props["data-observed-at"] }));
const rateFigures = regionIsNull
  ? []
  : findAll(region, node => hasClass(node, "r-crew-figure--rate"))
      .map(node => ({
        text: textContent(node),
        boundTexts: findAll(node, child => hasClass(child, "r-crew-bound"))
          .map(child => textContent(child)),
      }));
const positionNodes = regionIsNull ? [] : findAll(region, node => hasClass(node, "r-crew-position"));
const summary = {
  regionIsNull,
  meterCount: meters.length,
  rateFigureMarks: rateFigures,
  positionMarkCounts: positionNodes.map(node =>
    findAll(node, child => hasClass(child, "r-crew-bound")).length
  ),
  positionMarkTexts: positionNodes.map(node =>
    findAll(node, child => hasClass(child, "r-crew-bound")).map(child => textContent(child))
  ),
  meterAria: meters.map(node => node.props["aria-label"] || ""),
  fillWidths: meters.map(node => {
    const fill = findAll(node, child => hasClass(child, "r-crew-position"))[0];
    return fill ? fill.props.style.width : null;
  }),
  projectionLefts: meters.map(node => {
    const mark = findAll(node, child => hasClass(child, "r-crew-projection"))[0];
    return mark ? mark.props.style.left : null;
  }),
  projectionObservedAt: meters.map(node => {
    const mark = findAll(node, child => hasClass(child, "r-crew-projection"))[0];
    return mark ? mark.props["data-observed-at"] : null;
  }),
  warned: meters.map(node => hasClass(node, "r-crew-meter--warn")),
  rowTexts: (regionIsNull ? [] : [region]).map(textContent),
  figureTexts: figures,
  cacheClasses: regionIsNull ? [] : findAll(region, node => hasClass(node, "r-crew-cache")),
};
process.stdout.write(JSON.stringify(summary));
""",
        )
    )
    result = subprocess.run(
        ["node"],
        cwd=ROOT,
        input=script,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_window_duration_is_read_from_the_payload_and_names_the_meter() -> None:
    result = _render([UNSUSTAINABLE])

    assert result["regionIsNull"] is False
    assert result["meterCount"] == 1
    # One meter per backend, labelled with the reported seven-day period.
    assert "codex" in result["rowTexts"][0]
    assert "7-day window" in result["meterAria"][0]
    assert "7-day window" in result["rowTexts"][0]


def test_backend_reporting_no_window_renders_no_meter_and_no_period() -> None:
    region = _render([NO_WINDOW])
    assert region["regionIsNull"] is True
    assert region["meterCount"] == 0
    assert "".join(region["rowTexts"]) == ""

    # The whole surface also carries no meter for such a backend.
    surface = _render([NO_WINDOW], surface=True)
    assert surface["meterCount"] == 0
    assert not re.search(
        r"\d+-(day|hour)-?\s*window|window", "".join(surface["rowTexts"])
    )


def test_projection_before_the_reset_renders_left_of_it_and_warns() -> None:
    result = _render([UNSUSTAINABLE])

    assert result["meterCount"] == 1
    assert result["fillWidths"][0] == "20%"
    assert result["warned"][0] is True
    left = float(result["projectionLefts"][0].rstrip("%"))
    assert 0 < left < 100
    # The reset is the right edge; the projection is visibly before it.
    assert "projected exhaustion before the reset" in result["meterAria"][0]


def test_lower_rate_at_the_same_position_renders_at_the_reset_and_does_not_warn() -> (
    None
):
    result = _render([SUSTAINABLE])

    assert result["meterCount"] == 1
    assert result["fillWidths"][0] == "20%"
    assert result["warned"][0] is False
    left = float(result["projectionLefts"][0].rstrip("%"))
    assert left >= 100
    assert "projection at or after the reset" in result["meterAria"][0]


def test_every_rendered_figure_carries_an_observation_timestamp() -> None:
    result = _render([UNSUSTAINABLE])

    # Position + two rate windows each render as a figure with an observed-at.
    figures = result["figureTexts"]
    assert len(figures) >= 3
    for figure in figures:
        assert re.match(r"^\d{4}-\d{2}-\d{2}T", figure["observedAt"]), figure
    assert len([figure for figure in figures if figure["observedAt"]]) == len(figures)
    # The projection mark carries the position's observation time.
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", result["projectionObservedAt"][0])


def test_no_cache_hit_figure_appears_in_the_rendered_region() -> None:
    result = _render([UNSUSTAINABLE])

    assert result["cacheClasses"] == []
    rendered = " ".join(result["rowTexts"]).lower()
    assert not re.search(r"cache|hit\s*-?\s*rate", rendered)


def test_at_least_two_rate_windows_render_together() -> None:
    result = _render([UNSUSTAINABLE])

    # Figures minus the position figure are the rate windows.
    rateFigures = [f for f in result["figureTexts"] if "observed" not in f["text"]]
    assert len(rateFigures) >= 2
    assert any(
        "10.3\u00d7" in f["text"] and "1-hour window" in f["text"] for f in rateFigures
    )
    assert any(
        "1.71\u00d7" in f["text"] and "7-day window" in f["text"] for f in rateFigures
    )


def test_a_single_available_rate_window_is_labelled_as_the_only_window() -> None:
    result = _render([ONLY_WINDOW])

    rateFigures = [f for f in result["figureTexts"] if "observed" not in f["text"]]
    assert len(rateFigures) == 1
    assert "only window" in rateFigures[0]["text"]
    assert "1.71\u00d7" in rateFigures[0]["text"]
    # No second window is implied beside it.
    assert not re.search(
        r"\d+\.\d+\u00d7.*(?:1-hour|5-hour) window", rateFigures[0]["text"]
    )


def test_every_derived_rate_figure_is_marked_as_an_upper_bound() -> None:
    result = _render([UNSUSTAINABLE])

    rateMarks = result["rateFigureMarks"]
    # Two rate windows render, and every derived rate figure is marked.
    assert len(rateMarks) == 2
    for figure in rateMarks:
        assert len(figure["boundTexts"]) == 1
        mark = figure["boundTexts"][0]
        # The marking names that it is an upper bound, not a measurement...
        assert "upper bound" in mark
        # ...and why it is one: a resumed session re-reads its whole
        # accumulated context on every turn, so input over-counts.
        assert "resumed" in mark.lower()
        assert "context" in mark.lower()


def test_the_meter_position_carries_no_upper_bound_marking() -> None:
    result = _render([UNSUSTAINABLE])

    assert result["meterCount"] == 1
    # The position is the authority; it carries no bound mark, so the
    # distinction is between the derived rate and the meter position rather
    # than decoration on both.
    assert result["positionMarkCounts"] == [0]
    assert result["positionMarkTexts"] == [[]]
