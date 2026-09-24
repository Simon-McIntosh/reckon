"""Gate the rendered captures of the plans that own them.

Each gated plan keeps its capture measure in its own figure directory
(docs/figures/<plan>/after/capture-index.json). This module reads that
index for each plan and asserts the listed captures exist, carry the
recorded viewport width, and show the populated state their owner
required. The producer factory below (invoked only from __main__)
regenerates captures under docs/figures/rendered-evidence.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple
from unittest import SkipTest

import pytest

from tests.spa_browser_harness import (
    file_spa,
    installed_browser_or_skip,
    temporary_browser_profile,
)

ROOT = Path(__file__).resolve().parents[1]
CAPTURE_ROOT = ROOT / "docs" / "figures" / "rendered-evidence"
VIEWPORT = (1374, 900)
CONSTRAINED_VIEWPORT = (900, 420)


def _inventory_item(slug: str, title: str, sprint: str) -> dict[str, object]:
    return {
        "slug": slug,
        "title": title,
        "summary": f"Rendered work for {title.lower()}.",
        "status": "active",
        "effective_status": "active",
        "impl": 0.25,
        "sprint": sprint,
        "decisions": [],
        "dec_open": 0,
    }


def _composed_state() -> dict[str, object]:
    inventory = [
        _inventory_item("alpha-route", "Alpha route", "focus"),
        _inventory_item("beta-route", "Beta route", "focus"),
        _inventory_item("gamma-route", "Gamma route", "concurrent"),
    ]
    focus_items = [
        {
            "slug": "alpha-route",
            "title": "Alpha route",
            "why_now": "It unblocks the first consumer.",
            "done_when": "The alpha route is rendered.",
            "status": "active",
        },
        {
            "slug": "beta-route",
            "title": "Beta route",
            "why_now": "It unblocks the second consumer.",
            "done_when": "The beta route is rendered.",
            "status": "active",
        },
    ]
    sprints = [
        {
            "id": "concurrent",
            "theme": "Concurrent delivery",
            "status": "active",
            "starts": "2026-09-01",
            "ends": "2026-09-03",
            "items": [{"slug": "gamma-route"}],
            "metrics": {
                "item_count": 1,
                "by_effective_status": {"active": 1},
                "mean_impl": 0.5,
                "current_work": [{"slug": "gamma-route", "title": "Gamma route"}],
            },
        },
        {
            "id": "focus",
            "theme": "Focused delivery",
            "status": "active",
            "starts": "2026-09-01",
            "ends": "2026-09-02",
            "items": focus_items,
            "metrics": {
                "item_count": 2,
                "by_effective_status": {"active": 2},
                "mean_impl": 0.25,
                "current_work": [
                    {"slug": "alpha-route", "title": "Alpha route"},
                    {"slug": "beta-route", "title": "Beta route"},
                ],
            },
        },
        {
            "id": "queued",
            "theme": "Queued delivery",
            "status": "planned",
            "starts": "2026-09-04",
            "ends": "2026-09-05",
            "items": [],
            "metrics": {
                "item_count": 0,
                "by_effective_status": {},
                "mean_impl": 0,
                "current_work": [],
            },
        },
    ]
    ready_set = {
        "ready": [
            {
                "slug": "alpha-route",
                "sprint": "focus",
                "progress_pct": 25,
                "reason": "ready with all hard prerequisites satisfied",
                "section_readiness": [
                    {"section": "s1", "ready": True, "blockers": []},
                    {"section": "s2", "ready": True, "blockers": []},
                ],
            },
            {
                "slug": "beta-route",
                "sprint": "focus",
                "progress_pct": 25,
                "reason": "ready with all hard prerequisites satisfied",
                "section_readiness": [{"section": "s3", "ready": True, "blockers": []}],
            },
        ]
    }
    return {
        "project": "reckon",
        "today": "2026-09-01",
        "loaded_at": "2026-09-01T19:30:00+02:00",
        "source_format": "distributed",
        "active_sprint_id": "focus",
        "active_sprints": sprints[:2],
        "active_sprint_conflict": True,
        "inventory": inventory,
        "plans": {item["slug"]: item for item in inventory},
        "projects": [{"project": "reckon", "plans_count": len(inventory)}],
        "sprints": sprints,
        "milestones": [],
        "blockers": [],
        "timeline": [],
        "north_stars": [],
        "ready_set": ready_set,
    }


def _capture_screenshot(
    browser: str,
    tmp_path: Path,
    url: str,
    destination: Path,
    viewport: tuple[int, int],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with temporary_browser_profile(tmp_path) as profile:
        result = subprocess.run(
            [
                browser,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                f"--user-data-dir={profile}",
                f"--window-size={viewport[0]},{viewport[1]}",
                "--virtual-time-budget=2500",
                f"--screenshot={destination}",
                url,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    if result.returncode != 0 or not destination.is_file():
        raise AssertionError(
            f"screenshot failed with {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _probe_capture(
    tmp_path: Path,
    browser: str,
    *,
    output_root: Path,
    name: str,
    expression: str,
    remove_expression: str,
    ready_expression: str,
    viewport: tuple[int, int] = VIEWPORT,
    preload_expression: str | None = None,
    prepare_expression: str | None = None,
    screenshot_setup: str | None = None,
) -> dict[str, object]:
    with file_spa(
        tmp_path,
        browser,
        _composed_state(),
        route="#sprint/focus",
    ) as context:
        observed = context.run_probe(
            expression,
            viewport=viewport,
            ready_expression=ready_expression,
            preload_expression=preload_expression,
            prepare_expression=prepare_expression,
            remove_expression=remove_expression,
        )
        assert isinstance(observed, dict)
        baseline = observed["baseline"]
        removed = observed["removed"]
        assert baseline["signal"] is True, baseline
        assert removed["signal"] is False, removed

        if screenshot_setup:
            page = Path(context.url.removeprefix("file:").split("#", 1)[0])
            document = page.read_text(encoding="utf-8")
            injected = f"<script>{screenshot_setup}</script></body>"
            page.write_text(document.replace("</body>", injected), encoding="utf-8")
        image = output_root / f"{name}.png"
        _capture_screenshot(browser, tmp_path, context.url, image, viewport)

    geometry = {
        "capture": name,
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "positive": baseline,
        "mutation_control": {
            "signal_removed": True,
            "positive_assertion_after_removal": removed["signal"],
            "observation": removed,
        },
        "image": image.name,
    }
    geometry_path = output_root / f"{name}.geometry.json"
    geometry_path.write_text(json.dumps(geometry, indent=2) + "\n", encoding="utf-8")
    return geometry


def _owned_process_count(marker: str) -> int:
    count = 0
    for command_line in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if marker in command_line.read_bytes().decode(errors="replace"):
                count += 1
        except OSError:
            continue
    return count


def generate_captures(output_root: Path = CAPTURE_ROOT) -> dict[str, object]:
    browser = installed_browser_or_skip()
    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="owed-captures-") as temporary:
        tmp_path = Path(temporary)
        marker = str(tmp_path)

        now_line = _probe_capture(
            tmp_path,
            browser,
            output_root=output_root,
            name="now-line-advance",
            ready_expression="Boolean(document.querySelector('.r-now-line'))",
            preload_expression="""(() => {
              let clock = Date.parse('2026-09-01T06:00:00Z');
              const nativeInterval = window.setInterval.bind(window);
              Date.now = () => clock;
              window.setInterval = (callback, delay, ...args) => nativeInterval(() => {
                if (delay === 30000) clock += 60 * 60 * 1000;
                callback(...args);
              }, delay === 30000 ? 80 : delay);
            })()""",
            expression="""(() => new Promise(resolve => {
              const line = document.querySelector('.r-now-line');
              if (!line) {
                resolve({
                  signal: false, lineCount: 0, firstLeft: null, secondLeft: null,
                  advancePercentagePoints: 0, navigationBefore: 1,
                  navigationAfter: 1, navigationDelta: 0,
                });
                return;
              }
              const firstLeft = Number.parseFloat(line.style.left);
              const navigationBefore = performance.getEntriesByType('navigation').length;
              setTimeout(() => {
                const current = document.querySelector('.r-now-line');
                const secondLeft = current ? Number.parseFloat(current.style.left) : firstLeft;
                const navigationAfter = performance.getEntriesByType('navigation').length;
                resolve({
                  signal: Boolean(current) && secondLeft > firstLeft
                    && navigationAfter === navigationBefore,
                  lineCount: current ? 1 : 0,
                  firstLeft, secondLeft,
                  advancePercentagePoints: secondLeft - firstLeft,
                  navigationBefore, navigationAfter,
                  navigationDelta: navigationAfter - navigationBefore,
                });
              }, 220);
            }))()""",
            remove_expression="document.querySelector('.r-now-line')?.remove()",
        )

        sprint_table = _probe_capture(
            tmp_path,
            browser,
            output_root=output_root,
            name="sprint-table-state",
            ready_expression="Boolean(document.querySelector('.r-sprint-table tbody tr'))",
            expression="""(() => {
              const rows = [...document.querySelectorAll('.r-sprint-table tbody tr:not([hidden])')];
              const order = rows.map(row => row.querySelector('th strong')?.textContent.trim());
              const badge = document.querySelector('.r-sprint-conflict');
              return {
                signal: rows.length > 0 && order.join(',') === 'concurrent,focus,queued'
                  && Boolean(badge),
                rowCount: rows.length,
                rowOrder: order,
                conflictBadgeCount: badge ? 1 : 0,
                conflictBadgeText: badge?.textContent.trim() || '',
              };
            })()""",
            remove_expression="""(() => {
              document.querySelectorAll('.r-sprint-table tbody tr').forEach(row => row.remove());
              document.querySelector('.r-sprint-conflict')?.remove();
            })()""",
        )

        reachability = _probe_capture(
            tmp_path,
            browser,
            output_root=output_root,
            name="constrained-reachability",
            viewport=CONSTRAINED_VIEWPORT,
            ready_expression="Boolean(document.querySelector('.r-completed-work'))",
            prepare_expression="""(() => {
              const owner = document.querySelector('.r-sprint-view .r-reader-with-attachments > .r-body');
              owner.scrollTop = owner.scrollHeight;
            })()""",
            expression="""(() => {
              const owner = document.querySelector('.r-sprint-view .r-reader-with-attachments > .r-body');
              const target = document.querySelector('.r-completed-work');
              const ownerRect = owner.getBoundingClientRect();
              const targetRect = target?.getBoundingClientRect();
              const exceeding = [...document.querySelectorAll('body *')].filter(element => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0
                  && (rect.left < -1 || rect.right > innerWidth + 1);
              });
              const reachable = Boolean(targetRect)
                && targetRect.top >= ownerRect.top - 1
                && targetRect.bottom <= ownerRect.bottom + 1;
              return {
                signal: reachable && exceeding.length === 0 && owner.scrollTop > 0,
                reachableTargetCount: reachable ? 1 : 0,
                elementsPastViewport: exceeding.length,
                ownerScrollTop: owner.scrollTop,
                ownerScrollHeight: owner.scrollHeight,
                ownerClientHeight: owner.clientHeight,
                targetBottom: targetRect?.bottom || null,
                viewportBottom: ownerRect.bottom,
              };
            })()""",
            remove_expression="document.querySelector('.r-completed-work')?.remove()",
            screenshot_setup="""setTimeout(() => {
              const owner = document.querySelector('.r-sprint-view .r-reader-with-attachments > .r-body');
              if (owner) owner.scrollTop = owner.scrollHeight;
            }, 300);""",
        )

        ready_setup = """setTimeout(() => {
          [...document.querySelectorAll('[role=tab]')]
            .find(button => button.textContent.includes('Ready lanes'))?.click();
        }, 100);"""
        ready_lanes = _probe_capture(
            tmp_path,
            browser,
            output_root=output_root,
            name="ready-lanes",
            ready_expression="Boolean(document.querySelector('.r-sprint-tabs'))",
            prepare_expression="""(() => new Promise(resolve => {
              [...document.querySelectorAll('[role=tab]')]
                .find(button => button.textContent.includes('Ready lanes'))?.click();
              const wait = () => document.querySelector('.r-ready-lane')
                ? resolve() : setTimeout(wait, 20);
              wait();
            }))()""",
            expression="""(() => {
              const lanes = [...document.querySelectorAll('.r-ready-lane')];
              const handles = [...document.querySelectorAll('.r-ready-lane-invocation')]
                .map(element => element.textContent.trim());
              return {
                signal: lanes.length > 0 && handles.length === lanes.length
                  && handles.every(handle => handle.startsWith('/reckon-build ')),
                laneCount: lanes.length,
                handleCount: handles.length,
                handles,
              };
            })()""",
            remove_expression="document.querySelectorAll('.r-ready-lane').forEach(lane => lane.remove())",
            screenshot_setup=ready_setup,
        )

        residual_processes = _owned_process_count(marker)
        residual_profiles = len(list(tmp_path.glob("browser-profile-*"))) + len(
            list(tmp_path.glob("file-spa-*"))
        )

        captures = [now_line, sprint_table, reachability, ready_lanes]
        index = {
            "delivery": "file-url",
            "captureCount": len(captures),
            "mutationControlCount": sum(
                item["mutation_control"]["positive_assertion_after_removal"] is False
                for item in captures
            ),
            "captures": captures,
            "notAttemptedLiveFetches": [
                {
                    "subject": "new plan visible without document navigation",
                    "reason": "requires live discovery revalidation over HTTP",
                },
                {
                    "subject": "first-load and reload network-origin timing",
                    "reason": "requires a real network load and reload over HTTP",
                },
            ],
            "residualChromeProcesses": residual_processes,
            "residualTemporaryProfiles": residual_profiles,
        }
        (output_root / "capture-index.json").write_text(
            json.dumps(index, indent=2) + "\n", encoding="utf-8"
        )
        return index


class _CaptureSpec(NamedTuple):
    figures_dir: Path
    surfaces: tuple[str, ...]
    widths: tuple[int, ...]
    population_fields: tuple[str, ...]
    population_any_of: tuple[str, ...]
    require_populated: bool


class _CaptureGateReceipt(NamedTuple):
    captures_read: int
    image_bytes_opened: int


# Each entry names a figure directory the owning plan writes its capture
# measure into, the surfaces it captures at each recorded width, and the
# population signals its section states as its done-when.
_PLAN_CAPTURE_SPECS = (
    _CaptureSpec(
        figures_dir=ROOT / "docs/figures/fleet-home",
        surfaces=("home", "home-hidden"),
        widths=(1374, 1920),
        population_fields=("rowCount",),
        population_any_of=(),
        require_populated=False,
    ),
    _CaptureSpec(
        figures_dir=ROOT / "docs/figures/graph-derived-schedule",
        surfaces=("graph-named", "graph-unnamed", "sprint-detail", "crew-flow"),
        widths=(1374, 1920),
        population_fields=(),
        population_any_of=("cardCount", "laneCount"),
        require_populated=True,
    ),
    _CaptureSpec(
        figures_dir=ROOT / "docs/figures/spa-shell-scope-and-visibility",
        surfaces=("topbar", "sheet", "crew"),
        widths=(1374, 1920),
        population_fields=(),
        population_any_of=(),
        require_populated=True,
    ),
    _CaptureSpec(
        figures_dir=ROOT / "docs/figures/artifact-feeds-and-reader",
        surfaces=(
            "evidence-banner",
            "figures-index",
            "figure-reader",
            "plan-fullscreen-cone",
        ),
        widths=(1374, 1920),
        population_fields=("rowCount",),
        population_any_of=(),
        require_populated=True,
    ),
)


def assert_plan_capture_index(spec: _CaptureSpec) -> _CaptureGateReceipt:
    index_path = spec.figures_dir / "after" / "capture-index.json"
    if not index_path.is_file():
        raise AssertionError(f"capture index missing: {index_path}")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    captures = index.get("captures")
    if not captures:
        raise AssertionError(f"capture index holds no captures: {index_path}")

    expected = {
        f"{surface}-{width}" for surface in spec.surfaces for width in spec.widths
    }
    listed = {capture.get("capture") for capture in captures}
    assert listed == expected, (
        index_path,
        sorted(listed - expected),
        sorted(expected - listed),
    )
    assert index.get("captureCount") == len(captures), (
        index_path,
        index.get("captureCount"),
        len(captures),
    )
    assert set(index.get("viewportWidths", [])) == set(spec.widths), (
        index_path,
        index.get("viewportWidths"),
    )

    for capture in captures:
        width = capture.get("width")
        assert width in spec.widths, (index_path, capture)
        assert isinstance(capture.get("height"), int) and capture["height"] > 0, (
            index_path,
            capture,
        )
        image_name = capture.get("image")
        assert isinstance(image_name, str) and image_name, (index_path, capture)
        image = index_path.parent / image_name
        try:
            image_stat = image.stat()
        except FileNotFoundError as error:
            raise AssertionError(f"capture file missing: {image}") from error
        assert image_stat.st_size > 0, (index_path, image)
        for field in spec.population_fields:
            assert capture.get(field, 0) > 0, (index_path, capture, field)
        if spec.population_any_of:
            assert any(capture.get(field, 0) > 0 for field in spec.population_any_of), (
                index_path,
                capture,
            )
        if spec.require_populated:
            assert capture.get("populated") is True, (index_path, capture)
    return _CaptureGateReceipt(captures_read=len(captures), image_bytes_opened=0)


def _synthetic_spec(figures_dir: Path) -> _CaptureSpec:
    return _CaptureSpec(
        figures_dir=figures_dir,
        surfaces=("primary",),
        widths=(1374,),
        population_fields=("rowCount",),
        population_any_of=(),
        require_populated=False,
    )


@pytest.mark.parametrize(
    "spec",
    _PLAN_CAPTURE_SPECS,
    ids=[spec.figures_dir.name for spec in _PLAN_CAPTURE_SPECS],
)
def test_plan_capture_gate_reads_the_plan_own_index(spec: _CaptureSpec) -> None:
    receipt = assert_plan_capture_index(spec)
    index = json.loads(
        (spec.figures_dir / "after" / "capture-index.json").read_text(encoding="utf-8")
    )
    assert receipt == _CaptureGateReceipt(
        captures_read=index["captureCount"], image_bytes_opened=0
    )


def test_plan_capture_gate_opens_no_image_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def image_read_forbidden(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("IMAGE_BYTES_OPENED")

    monkeypatch.setattr(Path, "read_bytes", image_read_forbidden)
    receipts = [assert_plan_capture_index(spec) for spec in _PLAN_CAPTURE_SPECS]
    expected_captures = sum(
        json.loads(
            (spec.figures_dir / "after" / "capture-index.json").read_text(
                encoding="utf-8"
            )
        )["captureCount"]
        for spec in _PLAN_CAPTURE_SPECS
    )
    assert sum(receipt.captures_read for receipt in receipts) == expected_captures
    assert sum(receipt.image_bytes_opened for receipt in receipts) == 0


def _write_synthetic_capture_index(
    figures_dir: Path, *, row_count: int, image_exists: bool
) -> _CaptureSpec:
    spec = _synthetic_spec(figures_dir)
    after = figures_dir / "after"
    after.mkdir(parents=True)
    image = after / "primary-1374.png"
    if image_exists:
        image.write_text("captured", encoding="utf-8")
    (after / "capture-index.json").write_text(
        json.dumps(
            {
                "delivery": "file-url",
                "captureCount": 1,
                "viewportWidths": [1374],
                "captures": [
                    {
                        "capture": "primary-1374",
                        "surface": "primary",
                        "image": image.name,
                        "width": 1374,
                        "height": 900,
                        "populated": True,
                        "rowCount": row_count,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return spec


def test_plan_capture_gate_refuses_missing_file_and_zero_row_count(
    tmp_path: Path,
) -> None:
    missing_file = _write_synthetic_capture_index(
        tmp_path / "missing", row_count=1, image_exists=False
    )
    with pytest.raises(AssertionError, match="capture file missing"):
        assert_plan_capture_index(missing_file)

    zero_rows = _write_synthetic_capture_index(
        tmp_path / "zero", row_count=0, image_exists=True
    )
    with pytest.raises(AssertionError, match="rowCount"):
        assert_plan_capture_index(zero_rows)


def _rostered_spec(figures_name: str) -> _CaptureSpec:
    return next(
        spec for spec in _PLAN_CAPTURE_SPECS if spec.figures_dir.name == figures_name
    )


def test_plan_capture_gate_refuses_an_emptied_own_index(tmp_path: Path) -> None:
    """A roster entry is load bearing: empty the owned index and the gate trips.

    The committed artifact-feeds-and-reader index and its eight PNGs are
    copied whole into the temporary tree, so the only mutation from the known
    good state is the captures list being emptied. The gate must refuse where
    the live index passes.
    """
    source = _rostered_spec("artifact-feeds-and-reader")
    figures_dir = tmp_path / "artifact-feeds-and-reader"
    after = figures_dir / "after"
    shutil.copytree(source.figures_dir / "after", after)
    index = json.loads(
        (after / "capture-index.json").read_text(encoding="utf-8")
    )
    emptied = dict(index)
    emptied["captures"] = []
    emptied["captureCount"] = 0
    (after / "capture-index.json").write_text(
        json.dumps(emptied, indent=2) + "\n", encoding="utf-8"
    )
    spec = _CaptureSpec(
        figures_dir=figures_dir,
        surfaces=source.surfaces,
        widths=source.widths,
        population_fields=source.population_fields,
        population_any_of=source.population_any_of,
        require_populated=source.require_populated,
    )
    with pytest.raises(AssertionError, match="holds no captures"):
        assert_plan_capture_index(spec)


if __name__ == "__main__":
    try:
        result = generate_captures()
    except SkipTest as error:
        print(f"SKIP: {error}", file=sys.stderr)
        raise SystemExit(77) from error
    print(json.dumps(result, indent=2))
