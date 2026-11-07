"""A live pointer carries only a path to the detail no live reader consults.

Every follower on the node re-reads the live pointers on each pass, so the
dispatch-time detail none of them reads costs each of them on every pass. A
dispatch writes that detail once, to a file in the run's own directory, and the
pointer keeps only its path. One accessor answers for both shapes: a pointer
written after the move names the file, and one written before it still carries
the fields inline, so an old pointer keeps reading.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import queue_admission
from reckon.crew import runs as runs_module
from reckon.crew.runs import pointer_path, read_pointer
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"

DISPATCH_CONFIG = {
    "default_backend": "native",
    "backends": {
        "native": {
            "launch": "cli",
            "command": "codex",
            "model": "embedded-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

DETAIL_FIELDS = (
    "picker_selection",
    "admit_request",
    "argv",
    "competence",
    "peer_scopes",
    "unreconciled_override",
)


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc">'
        '<h2 id="s2">&sect;2 &mdash; Section two</h2>'
        "</main></body></html>\n"
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Plan A",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    fleet = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet.read_text(encoding="utf-8"), encoding="utf-8"
    )
    _git(root, "add", "skills")
    _git(root, "commit", "-q", "-m", "test: provision the fleet script")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _selection() -> SimpleNamespace:
    fields = {
        "action": "route",
        "backend": "native",
        "family": "test",
        "model": "embedded-model",
        "effort": "high",
        "probabilities": {"native": 0.9},
        "confidence": 0.9,
        "jev_model": "jev-test",
        "fallback_reason": None,
        "latency_ms": 1.0,
        "excluded": [],
    }
    return SimpleNamespace(as_dict=lambda: fields)


def _dispatch_one(
    repository: Path, tmp_path: Path, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> dict:
    from reckon.crew import picker

    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: _selection())
    node = crew.TaskNode(
        id=node_id,
        goal="keep dispatch-time detail off the live pointer",
        plan=PLAN,
        section="s2",
        spec_level="guided",
        done_when="tests/test_the_first_six_fields_leave_the_live_pointer.py passes",
        write_paths=["package/target.py"],
        time_budget="20m",
        manifest_path=str(tmp_path / f"{node_id}.md"),
    )
    return crew.dispatch(
        node=node,
        project=PROJECT,
        repo=repository,
        config=DISPATCH_CONFIG,
        session="dispatch-session",
        launcher=lambda *args, **kwargs: 42001,
        check_budget=False,
        dispatch_options={"picker_selection": {"action": "route", "backend": "native"}},
    )


def test_a_dispatch_keeps_its_detail_in_the_run_directory(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All six moved fields land in one file, and the pointer only names it."""
    record = _dispatch_one(repository, tmp_path, "detail-moves", monkeypatch)
    run_id = str(record["run_id"])
    # A queued launch records the request it was admitted from on its pointer;
    # that write goes through the same path as every other pointer write.
    queue_admission._stamp_admission(
        run_id, {"run_id": run_id, "project": PROJECT}, admitted_at="now"
    )

    text = pointer_path(run_id).read_text(encoding="utf-8")
    pointer_bytes = len(text.encode("utf-8"))
    print(f"test_dispatch_pointer_bytes={pointer_bytes}")
    pointer = json.loads(text)
    assert pointer_bytes < 12 * 1024, sorted(
        ((name, len(json.dumps(value))) for name, value in pointer.items()),
        key=lambda item: item[1],
        reverse=True,
    )[:8]
    detail_path = Path(pointer["dispatch_detail"])
    assert detail_path == runs_module.dispatch_detail_path(run_id)
    assert detail_path.parent == runs_module.run_dir(run_id)
    assert detail_path.is_file()
    assert "picker_selection" not in pointer.get("dispatch_options", {})
    for field in DETAIL_FIELDS:
        assert field not in pointer, field
        assert field in json.loads(detail_path.read_text(encoding="utf-8")), field


def test_live_pointer_readers_use_the_detail_accessor() -> None:
    """Live pointer reads across source and tests resolve moved fields."""
    project_root = Path(__file__).parents[1]
    direct_reads = []
    for source_dir in (project_root / "reckon", project_root / "tests"):
        for path in source_dir.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                key = None
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"get", "pop", "setdefault"}
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "pointer"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    key = node.args[0].value
                elif (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.ctx, ast.Load)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "pointer"
                    and isinstance(node.slice, ast.Constant)
                ):
                    key = node.slice.value
                if key in DETAIL_FIELDS:
                    direct_reads.append(
                        f"{path.relative_to(project_root)}:{node.lineno}:{key}"
                    )
    assert not direct_reads, direct_reads


def test_the_accessor_answers_for_both_pointer_shapes(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A moved field reads the same from a new-shape and an old-shape pointer."""
    record = _dispatch_one(repository, tmp_path, "detail-shapes", monkeypatch)
    run_id = str(record["run_id"])
    queue_admission._stamp_admission(
        run_id, {"run_id": run_id, "project": PROJECT}, admitted_at="now"
    )

    # The new shape: the pointer names the detail file and carries none of it.
    new_pointer = read_pointer(run_id)
    assert "dispatch_detail" in new_pointer
    stored = json.loads(
        runs_module.dispatch_detail_path(run_id).read_text(encoding="utf-8")
    )
    for field in DETAIL_FIELDS:
        assert runs_module.dispatch_field(new_pointer, field) == stored[field], field

    # The old shape: the same values are carried inline, with no detail file.
    # This is the case an accessor without the fallback cannot answer for.
    old_run_id = "r-20260101T000000000000-old-shape"
    old_pointer = {field: f"inline-{field}" for field in DETAIL_FIELDS}
    old_pointer.update({"run_id": old_run_id, "project": PROJECT})
    path = pointer_path(old_run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old_pointer), encoding="utf-8")
    assert not runs_module.dispatch_detail_path(old_run_id).exists()
    for field in DETAIL_FIELDS:
        assert (
            runs_module.dispatch_field(read_pointer(old_run_id), field)
            == f"inline-{field}"
        )


def test_promotion_reads_the_moved_fields_onto_the_ledger_row(
    repository: Path,
) -> None:
    """A promoted row still carries the pick and the unreconciled override."""
    run_id = "r-20261027T120000000000-moved-detail"
    runs_module._write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-10-27T12:00:00Z",
            "manifest_path": "/durable/manifest.md",
            "picker_selection": {"action": "route", "backend": "native"},
            "unreconciled_override": {"runs": ["r-old"], "grace": "2h"},
            "node": {
                "id": "moved-detail",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )
    assert "picker_selection" not in read_pointer(run_id)

    crew.complete(run_id, gate="passed", outcome="landed", root=repository)

    row = ledger.load(PROJECT, repository)[0]["runs"][0]
    assert row["picker_selection"] == {"action": "route", "backend": "native"}
    assert row["unreconciled_override"] == {"runs": ["r-old"], "grace": "2h"}


def test_other_live_readers_resolve_the_detail_file(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slim pointer still supplies the picker and launched command to readers."""
    from reckon import _backends, run_record, serve
    from reckon.crew import dispatch_launch, dispatch_sessions, recovery_stream

    run_id = "r-20261027T120000000000-reader-detail"
    log = tmp_path / "stream.jsonl"
    log.write_text("", encoding="utf-8")
    stderr = tmp_path / "stderr.log"
    stderr.write_text("worker failed before its first event", encoding="utf-8")
    runs_module._write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "launch": "cli",
            "backend": "native",
            "log_path": str(log),
            "stderr_path": str(stderr),
            "argv": ["recorded-harness", "-p"],
            "picker_selection": {"action": "route", "backend": "native"},
        },
    )
    pointer = read_pointer(run_id)
    assert "argv" not in pointer and "picker_selection" not in pointer
    assert run_record.promoted_picker_selection(run_id) == {
        "action": "route",
        "backend": "native",
    }

    monkeypatch.setattr(
        _backends,
        "observe_stream",
        lambda **kwargs: SimpleNamespace(
            terminal=kwargs["backend"]["command"] == "recorded-harness"
        ),
    )
    assert serve._stream_is_terminal(pointer, [])
    assert dispatch_launch._empty_stream_launch_failure(pointer, {"exit_status": 2})[
        "argv"
    ] == ["recorded-harness", "-p"]
    settings = dispatch_sessions._backend_settings(
        pointer,
        {"backends": {"native": {"launch": "cli", "command": "other-harness"}}},
    )
    assert settings["command"] == "recorded-harness"

    class CommandObservedError(Exception):
        pass

    def observe_command(_record: dict, argv: list[str]) -> str:
        assert argv == ["recorded-harness", "-p"]
        raise CommandObservedError

    monkeypatch.setattr(recovery_stream, "_harness_command", observe_command)
    with pytest.raises(CommandObservedError):
        recovery_stream._observed_stream(pointer, memo=None)
