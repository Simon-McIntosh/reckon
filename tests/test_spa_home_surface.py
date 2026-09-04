import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / "docs" / "ui" / "home.jsx"
ROUTE = ROOT / "docs" / "ui" / "shell-route.jsx"
SHELL = ROOT / "docs" / "ui" / "shell.jsx"
INDEX = ROOT / "docs" / "index.html"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    brace = source.index("{", source.index(")", start))
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _evaluate(functions: list[tuple[Path, str]], expression: str):
    script = "\n".join(_function_source(path, name) for path, name in functions)
    result = subprocess.run(
        ["node", "-e", f"{script}\nconsole.log(JSON.stringify({expression}));"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_empty_hash_and_home_hash_route_to_the_shell_home() -> None:
    function = _function_source(ROUTE, "parseHash")
    result = subprocess.run(
        [
            "node",
            "-e",
            (
                f"{function}\nglobal.window={{location:{{hash:''}}}};"
                "const empty=parseHash();window.location.hash='#home';"
                "console.log(JSON.stringify([empty,parseHash()]));"
            ),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [{"view": "home"}, {"view": "home"}]
    assert 'to.view === "cockpit" ? { view: "home" } : to' in SHELL.read_text()


def test_home_module_and_styles_are_authored_before_the_shell() -> None:
    source = INDEX.read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/_ui/home.css">' in source
    assert source.index("/_ui/home.js") < source.index("/_ui/shell.js")


def test_visible_summary_uses_only_the_set_passed_by_visibility() -> None:
    visible = [
        {"project": "alpha", "plans_count": 3, "active": 1, "blocked": 1, "shipped": 1},
        {"project": "gamma", "plans_count": 2, "active": 1, "blocked": 0, "shipped": 1},
    ]
    runs = [{"project": "alpha"}]
    summary = _evaluate(
        [(HOME, "homeVisibleSummary")],
        f"homeVisibleSummary({json.dumps(visible)}, {json.dumps(runs)})",
    )
    assert summary == {
        "moving": 2,
        "plans": 5,
        "active": 2,
        "inFlight": 1,
        "held": 1,
        "shipped": 2,
    }


def test_project_rows_require_plans_and_sort_by_last_edit() -> None:
    projects = [
        {"project": "older", "plans_count": 2, "last_edited": "2026-09-01T10:00:00"},
        {"project": "dormant", "plans_count": 0, "last_edited": "2026-09-04T10:00:00"},
        {"project": "newer", "plans_count": 1, "last_edited": "2026-09-03T10:00:00"},
    ]
    rows = _evaluate(
        [(HOME, "homeProjectRows")],
        f"homeProjectRows({json.dumps(projects)}).map(project => project.project)",
    )
    assert rows == ["newer", "older"]


def test_activity_distinguishes_empty_from_recorded_series() -> None:
    empty = _evaluate([(HOME, "homeActivityProjection")], "homeActivityProjection([])")
    activity = _evaluate(
        [(HOME, "homeActivityProjection")],
        "homeActivityProjection([0,1,2,0,3])",
    )
    source = HOME.read_text(encoding="utf-8")
    streak = source[
        source.index("function HomeStreak") : source.index("function HomeStatusBar")
    ]

    assert empty is None
    assert activity["total"] == 6
    assert activity["recent"] == 5
    assert "no recorded activity" in streak
    assert "<polyline" in streak
    assert streak.index("if (!activity)") < streak.index("<polyline")


def test_dormant_and_in_flight_rows_obey_the_visible_set() -> None:
    projects = [
        {"project": "alpha", "plans_count": 2},
        {"project": "quiet", "plans_count": 0},
    ]
    runs = [
        {"project": "alpha", "run_id": "shown"},
        {"project": "hidden", "run_id": "excluded"},
    ]
    projects_json = json.dumps(projects)
    runs_json = json.dumps(runs)
    result = _evaluate(
        [(HOME, "homeDormantRows"), (HOME, "homeVisibleRuns")],
        f"({{dormant:homeDormantRows({projects_json}).map(row=>row.project),"
        f"runs:homeVisibleRuns({runs_json},{projects_json}).map(run=>run.run_id)}})",
    )
    assert result == {"dormant": ["quiet"], "runs": ["shown"]}


def test_home_surface_carries_the_six_stats_and_read_only_panels() -> None:
    source = HOME.read_text(encoding="utf-8")
    for label in ("projects moving", "plans", "active", "in flight", "held", "shipped"):
        assert f'"{label}"' in source
    assert "visible projects with no recorded work" in source
    assert "In flight" in source
    assert "polling every 3s" in source
    assert "Just landed" in source
    assert "last 72 hours" in source
    assert "action queue" not in source.lower()
