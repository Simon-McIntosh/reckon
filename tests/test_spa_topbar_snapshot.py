import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "docs" / "ui" / "shell.jsx"
SHARED = ROOT / "docs" / "ui" / "_shared.jsx"
TOPBAR_CSS = ROOT / "docs" / "ui" / "topbar.css"


def _run_shell_helpers(expression: str) -> object:
    source = SHELL.read_text()
    helpers = source[
        source.index('const PROJECT_VISIBILITY_STORAGE =') : source.index(
            "function TopBar("
        )
    ]
    script = f"""
{helpers}
console.log(JSON.stringify({expression}));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def _projects(*names: str) -> list[dict]:
    return [{"project": name, "plans_count": 1} for name in names]


def test_hiding_focused_project_moves_focus_to_a_survivor() -> None:
    change = _run_shell_helpers(
        'projectVisibilityChange('
        f'{json.dumps(_projects("alpha", "beta", "gamma"))}, [], "beta", "beta")'
    )

    assert change == {
        "changed": True,
        "hidden": ["beta"],
        "focus": "alpha",
    }


def test_last_visible_project_refuses_to_hide() -> None:
    change = _run_shell_helpers(
        'projectVisibilityChange('
        f'{json.dumps(_projects("alpha", "beta"))}, ["beta"], "alpha", "alpha")'
    )

    assert change == {
        "changed": False,
        "hidden": ["beta"],
        "focus": "alpha",
    }
    assert _run_shell_helpers(
        'visibleProjectRows('
        f'{json.dumps(_projects("alpha", "beta"))}, ["alpha", "beta"])'
        ".map(row => row.project)"
    ) == ["alpha"]


def test_project_without_mounted_plan_state_is_not_visible() -> None:
    visible = _run_shell_helpers(
        'visibleProjectRows('
        '[{"project":"empty","plans_count":0},'
        '{"project":"ready","plans_count":2}], []).map(row => row.project)'
    )

    assert visible == ["ready"]


def test_snapshot_receipt_counts_payload_resource_versions() -> None:
    payload = {
        "source_format": "distributed",
        "resource_versions": {f"resource:{index}": index for index in range(12)},
        "loaded_at": "2000-01-01T06:00:00+00:00",
    }
    receipt = _run_shell_helpers(f"snapshotReceipt({json.dumps(payload)})")

    assert receipt["sourceFormat"] == "distributed"
    assert receipt["resourceCount"] == 12
    assert receipt["loadedAt"] != "unknown time"

    topbar = SHELL.read_text()
    settings = SHARED.read_text()
    assert "snapshot={snapshot}" in topbar
    assert "onRefresh={() => window.location.reload()}" in topbar
    assert "{snapshot.resourceCount} resources" in settings
    assert 'className="r-snapshot-receipt" role="status"' in settings
    assert '<button type="button" className="settings-item" onClick={onRefresh}>Refresh</button>' in settings


def test_topbar_is_data_driven_and_owns_one_navigation_shell() -> None:
    source = SHELL.read_text()
    css = TOPBAR_CSS.read_text()

    assert "<details className=\"r-project-manage\">" in source
    assert "PROJECT_VISIBILITY_STORAGE" in source
    assert 'fetch("/_projects/index.json")' in source
    assert 'fetch("/crew")' in source
    assert "window.Cockpit" not in source
    assert "r-project-chip" in css
    assert all(
        project not in source
        for project in ("imas-ambix", "imas-codex", "nova-jax")
    )
