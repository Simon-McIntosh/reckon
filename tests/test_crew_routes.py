from __future__ import annotations

import http.client
import importlib
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, ledger, serve
from reckon.crew import routing


CONTEXT_CONFIG = {
    "default_backend": "bounded",
    "backends": {
        "bounded": {
            "launch": "cli",
            "command": "clive",
            "model": "worker-model",
            "sandbox": "worktree-full",
            "usable_input_window": 73_728,
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "8m", "needs_help_after_failures": 2},
}

ROOT = Path(__file__).resolve().parent.parent


def _add_project(root: Path, name: str, mounts: dict[str, str]) -> Path:
    repo = root / name
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "visible-work.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{name}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="visible-work">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-sprint" content="current">'
        '<meta name="plan-effort-hours" content="3.25">'
        "<title>Visible work</title>"
        '</head><body><main class="plan-doc"></main></body></html>',
        encoding="utf-8",
    )
    ledger.register_member(
        name,
        "observer",
        harness="codex",
        role="review",
        root=repo,
    )
    mounts[name] = str(repo / "docs")
    return repo


@pytest.fixture()
def crew_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_home = tmp_path / "config"
    mounts_file = config_home / "mounts.json"
    state_root = config_home / "state"
    config_home.mkdir()
    state_root.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    mounts: dict[str, str] = {}
    repos = {name: _add_project(tmp_path, name, mounts) for name in ("reckon", "other")}
    mounts_file.write_text(json.dumps(mounts), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    serve._DISC_CACHE.clear()

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "config_home": config_home,
            "mounts": mounts,
            "mounts_file": mounts_file,
            "port": server.server_port,
            "repos": repos,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        serve._DISC_CACHE.clear()


@pytest.fixture()
def context_dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    fleet_scripts = repo / "skills" / "reckon-ship" / "scripts"
    target = repo / "tests" / "large_context.py"
    plans.mkdir(parents=True)
    fleet_scripts.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    (plans / "visible-work.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="context-project">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="visible-work">'
        '</head><body><h2 id="dispatch">Dispatch</h2></body></html>',
        encoding="utf-8",
    )
    target.write_text("#" * 100_000 + "\n", encoding="utf-8")
    source = ROOT / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py"
    (fleet_scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills", "tests/large_context.py"],
        ["commit", "-q", "-m", "chore: seed repository"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"context-project": str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _write_pointer(
    config_home: Path,
    run_id: str,
    *,
    project: str = "reckon",
    event: dict | None = None,
) -> Path:
    log_path = config_home / "crew" / "runs" / run_id / "stream.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(json.dumps(event or {"type": "thread.started"}) + "\n")
    pointer = {
        "run_id": run_id,
        "project": project,
        "member": "observer",
        "role": "implement",
        "backend": "local",
        "dialect": "codex",
        "argv": ["codex", "exec"],
        "agent": {"model": "frontier", "effort": "high"},
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "log_path": str(log_path),
        "node": {
            "plan": "visible-work",
            "section": "delivery",
            "role": "implement",
            "done_when": "the route returns the live run",
        },
    }
    live_path = config_home / "crew" / "live" / f"{run_id}.json"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(json.dumps(pointer), encoding="utf-8")
    return log_path


def _get(port: int, path: str) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = body.decode()
        return response.status, payload
    finally:
        connection.close()


def test_all_projects_route_joins_roster_and_navigation(crew_server) -> None:
    _write_pointer(crew_server["config_home"], "run-all")

    status, payload = _get(crew_server["port"], "/crew")

    assert status == 200
    assert payload["project"] is None
    assert len(payload["runs"]) == 1
    row = payload["runs"][0]
    assert {
        "run_id": row["run_id"],
        "project": row["project"],
        "member": row["member"],
        "role": row["role"],
        "plan": row["plan"],
        "section": row["section"],
        "phase": row["phase"],
        "plan_href": row["plan_href"],
        "sprint_href": row["sprint_href"],
    } == {
        "run_id": "run-all",
        "project": "reckon",
        "member": "observer",
        "role": "review",
        "plan": "visible-work",
        "section": "delivery",
        "phase": "working",
        "plan_href": "/reckon/#plan/visible-work",
        "sprint_href": "/reckon/#sprint/current",
    }
    assert row["last_activity"].endswith("Z")
    assert row["model"] == "frontier"
    assert row["effort"] == "high"
    assert row["elapsed_seconds"] >= 0
    assert row["gate"] == "the route returns the live run"


def test_project_route_filters_other_mounted_runs(crew_server) -> None:
    _write_pointer(crew_server["config_home"], "run-reckon")
    _write_pointer(
        crew_server["config_home"],
        "run-other",
        project="other",
    )

    all_status, all_payload = _get(crew_server["port"], "/crew")
    status, payload = _get(crew_server["port"], "/crew/reckon")

    assert all_status == status == 200
    assert {row["project"] for row in all_payload["runs"]} == {"reckon", "other"}
    other_run = next(row for row in all_payload["runs"] if row["project"] == "other")
    assert other_run["effort_hours"] == 3.25
    assert other_run["backend"] == "local"
    assert payload["project"] == "reckon"
    assert [row["run_id"] for row in payload["runs"]] == ["run-reckon"]


def test_terminal_event_reports_done(crew_server) -> None:
    _write_pointer(
        crew_server["config_home"],
        "run-done",
        event={"type": "turn.completed", "usage": {}},
    )

    status, payload = _get(crew_server["port"], "/crew/reckon")

    assert status == 200
    assert payload["runs"][0]["phase"] == "done"


def test_recent_nonterminal_stream_reports_working(crew_server) -> None:
    log_path = _write_pointer(crew_server["config_home"], "run-recent")
    os.utime(log_path, None)

    status, payload = _get(crew_server["port"], "/crew/reckon")

    assert status == 200
    assert payload["runs"][0]["phase"] == "working"


def test_stale_nonterminal_stream_reports_idle(crew_server) -> None:
    log_path = _write_pointer(crew_server["config_home"], "run-stale")
    stale = time.time() - crew.LOG_STALE_AFTER_SECONDS - 5
    os.utime(log_path, (stale, stale))

    status, payload = _get(crew_server["port"], "/crew/reckon")

    assert status == 200
    assert payload["runs"][0]["phase"] == "idle"


def test_log_tail_read_is_bounded_by_byte_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = tmp_path / "stream.jsonl"
    line = json.dumps({"type": "thread.started", "padding": "x" * 256}) + "\n"
    stream.write_text(line * (serve.CREW_LOG_TAIL_BYTES // len(line) + 10))
    actual_open = Path.open
    read_sizes: list[int] = []

    class TrackedReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def seek(self, offset: int):
            return self.handle.seek(offset)

        def read(self, size: int = -1):
            read_sizes.append(size)
            return self.handle.read(size)

    def tracked_open(path: Path, *args, **kwargs):
        handle = actual_open(path, *args, **kwargs)
        return TrackedReader(handle) if path == stream else handle

    monkeypatch.setattr(Path, "open", tracked_open)

    lines = serve._read_log_tail(stream)

    assert stream.stat().st_size > serve.CREW_LOG_TAIL_BYTES
    assert read_sizes == [serve.CREW_LOG_TAIL_BYTES]
    assert lines


def test_unknown_project_route_is_not_found(crew_server) -> None:
    status, payload = _get(crew_server["port"], "/crew/missing")

    assert status == 404
    assert payload == "project not found"


def test_discovery_serves_declared_directions_and_plan_alignment(crew_server) -> None:
    project = "reckon"
    docs_dir = crew_server["repos"][project] / "docs"
    plan_path = docs_dir / "plans" / "visible-work.html"
    plan_path.write_text(
        plan_path.read_text().replace(
            "</head>",
            '<meta name="plan-north-star" content="reliable-delivery"></head>',
        ),
        encoding="utf-8",
    )
    state_dir = crew_server["config_home"] / "state" / project
    state_dir.mkdir(parents=True, exist_ok=True)
    directions = [
        {
            "id": "reliable-delivery",
            "name": "Reliable delivery",
            "statement": "Every release remains reproducible and observable.",
        }
    ]
    (state_dir / "index.json").write_text(
        json.dumps({"project": project, "data": {"north_stars": directions}}),
        encoding="utf-8",
    )
    serve._DISC_CACHE.clear()

    status, payload = _get(crew_server["port"], f"/_discover/{project}")
    state_status, state_payload = _get(
        crew_server["port"], f"/{project}/state/{project}/index.json"
    )

    assert status == 200
    assert payload["north_stars"] == directions
    assert payload["inventory"][0]["north_star"] == "reliable-delivery"
    assert state_status == 200
    assert state_payload["data"]["north_stars"] == directions
    assert state_payload["data"]["inventory"][0]["north_star"] == "reliable-delivery"


def _context_node(*, node_id: str = "context-reader") -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="measure the declared repository input",
        plan="visible-work",
        section="dispatch",
        role="implement",
        spec_level="exact",
        done_when=(
            "uv run pytest tests/large_context.py reports no failures and names "
            "tests/large_context.py in the estimate"
        ),
        write_paths=["tests/large_context.py"],
        time_budget="8m",
    )


def _fixed_standing_input(*_args, **_kwargs) -> tuple[int, dict]:
    return 50_392, {
        "calculated_tokens": 50_392,
        "floor_tokens": 50_392,
        "effective_tokens": 50_392,
        "token_estimator": "measured-fixture",
        "files": [],
    }


def test_standing_context_records_every_effective_instruction_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def context_manifest(request):
        captured["request"] = request
        return {
            "instructions": {
                "effective_chain": [
                    {
                        "path": "/policy/repository.md",
                        "resolved_path": "/policy/repository.md",
                        "readable": True,
                        "bytes": 140_000,
                    },
                    {
                        "path": "/policy/missing.md",
                        "resolved_path": "/policy/missing.md",
                        "readable": False,
                        "bytes": 9_999,
                    },
                ]
            },
            "canonical_policy": {
                "path": "/policy/canonical.md",
                "resolved_path": "/policy/canonical.md",
                "readable": True,
                "bytes": 42_000,
            },
        }

    monkeypatch.setattr(
        routing.agent_context, "build_context_manifest", context_manifest
    )

    tokens, inputs = routing._standing_context_input(tmp_path, {"launch": "in-harness"})

    assert captured["request"].target == tmp_path
    assert captured["request"].agent == "codex"
    assert tokens == inputs["effective_tokens"] == 52_000
    assert inputs["calculated_tokens"] == 52_000
    assert [item["path"] for item in inputs["files"]] == [
        "/policy/repository.md",
        "/policy/canonical.md",
    ]


def test_context_refusal_is_exit_five_before_worktree_creation(
    context_dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_module, "_resolved_flight", lambda *_a, **_k: CONTEXT_CONFIG
    )
    monkeypatch.setattr(routing, "_standing_context_input", _fixed_standing_input)
    monkeypatch.setattr(
        crew.capabilities, "load_capabilities", lambda: {"configurations": []}
    )
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    def forbid_worktree(*_args, **_kwargs):
        raise AssertionError("context refusal reached worktree creation")

    arguments = [
        "crew",
        "dispatch",
        "--project",
        "context-project",
        "--plan",
        "visible-work",
        "--section",
        "dispatch",
        "--spec-level",
        "exact",
        "--node",
        "context-reader",
        "--goal",
        "measure the declared repository input",
        "--done-when",
        (
            "uv run pytest tests/large_context.py reports no failures and names "
            "tests/large_context.py in the estimate"
        ),
        "--write-path",
        "tests/large_context.py",
        "--session",
        "session",
        "--repo",
        str(context_dispatch_repo),
    ]
    with monkeypatch.context() as guarded:
        guarded.setattr(dispatch_module, "_create_worktree", forbid_worktree)
        real = CliRunner().invoke(cli_module.main, arguments)
    dry = CliRunner().invoke(cli_module.main, [*arguments, "--dry-run"])

    assert real.output, repr(real.exception)
    real_payload = json.loads(real.output)
    dry_payload = json.loads(dry.output)
    assert real.exit_code == dry.exit_code == 5
    assert real_payload["error"] == dry_payload["error"] == "competence-refusal"
    assert real_payload["competence"] == dry_payload["competence"]
    verdict = real_payload["competence"]
    assert verdict["reason"] == "context-window-exceeded"
    assert verdict["estimated_tokens"] > verdict["window_tokens"] == 73_728
    assert verdict["shortfall_tokens"] == (
        verdict["estimated_tokens"] - verdict["window_tokens"]
    )
    assert verdict["context"]["inputs"]["repository_file_tokens"] > 0
    worktrees = subprocess.run(
        ["git", "worktree", "list"],
        cwd=context_dispatch_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "context-reader" not in worktrees
    assert crew.list_live() == []


def test_fitting_and_unbounded_backends_dispatch_without_context_refusal(
    context_dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routing, "_standing_context_input", _fixed_standing_input)
    monkeypatch.setattr(
        crew.capabilities, "load_capabilities", lambda: {"configurations": []}
    )
    fitting = {
        **CONTEXT_CONFIG,
        "backends": {
            "bounded": {
                **CONTEXT_CONFIG["backends"]["bounded"],
                "usable_input_window": 492_288,
            }
        },
    }

    record = crew.dispatch(
        node=_context_node(node_id="fitting-reader"),
        project="context-project",
        repo=context_dispatch_repo,
        config=fitting,
        session="session",
        launcher=lambda *_a, **_k: 4242,
    )

    context_fit = record["competence"]["context"]
    assert context_fit["allowed"] is True
    assert context_fit["estimated_tokens"] < context_fit["window_tokens"] == 492_288
    assert context_fit["shortfall_tokens"] == 0
    assert context_fit["inputs"]["repository_files"]["write_paths"]
    assert record["node"]["write_paths"] == ["tests/large_context.py"]
    assert record["agent"]["usable_input_window"] == 492_288

    unbounded = {
        **CONTEXT_CONFIG,
        "backends": {
            "bounded": {
                key: value
                for key, value in CONTEXT_CONFIG["backends"]["bounded"].items()
                if key != "usable_input_window"
            }
        },
    }
    resolution = crew.plan_dispatch(node=_context_node(), config=unbounded)
    verdict = routing._context_fit_verdict(
        resolution=resolution, repo=context_dispatch_repo
    )
    assert verdict is None


@pytest.mark.parametrize(
    ("write_paths", "done_when"),
    [
        (
            ["tests/test_crew.py"],
            (
                "uv run pytest tests/test_crew.py "
                "tests/test_crew_session_keying.py reports no failures"
            ),
        ),
        (
            [
                "reckon/crew/dispatch.py",
                "reckon/crew/routing.py",
                "tests/test_crew_shadow_paths.py",
            ],
            (
                "uv run pytest tests/test_crew_shadow_paths.py tests/test_crew_gc.py "
                "reports no failures"
            ),
        ),
    ],
)
def test_recorded_repository_scopes_refuse_glm_and_fit_flash(
    monkeypatch: pytest.MonkeyPatch,
    write_paths: list[str],
    done_when: str,
) -> None:
    monkeypatch.setattr(routing, "_standing_context_input", _fixed_standing_input)
    node = crew.TaskNode(
        id="recorded-context-reader",
        goal="exercise the recorded repository scope",
        plan="visible-work",
        section="dispatch",
        role="implement",
        spec_level="exact",
        done_when=done_when,
        write_paths=write_paths,
        time_budget="8m",
    )
    glm_resolution = crew.plan_dispatch(node=node, config=CONTEXT_CONFIG)
    glm = routing._context_fit_verdict(resolution=glm_resolution, repo=ROOT)
    flash_config = {
        **CONTEXT_CONFIG,
        "backends": {
            "bounded": {
                **CONTEXT_CONFIG["backends"]["bounded"],
                "usable_input_window": 492_288,
            }
        },
    }
    flash_resolution = crew.plan_dispatch(node=node, config=flash_config)
    flash = routing._context_fit_verdict(resolution=flash_resolution, repo=ROOT)

    assert glm is not None
    assert glm["allowed"] is False
    assert glm["window_tokens"] == 73_728
    assert glm["shortfall_tokens"] > 0
    assert flash is not None
    assert flash["allowed"] is True
    assert flash["window_tokens"] == 492_288
    assert flash["shortfall_tokens"] == 0
