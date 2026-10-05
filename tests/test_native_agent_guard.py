"""The pre-tool-use guard on harness-native background-agent spawns."""

from __future__ import annotations

import io
import json
from pathlib import Path

from reckon.crew.node import dispatch_invocation_shape
from reckon.hooks import native_agent_guard as guard


def _crew_managed_repo(tmp_path: Path, *, project: str = "proj") -> Path:
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / project).mkdir(parents=True)
    (repo / "docs" / "state" / project / "crew.json").write_text("{}")
    return repo


def _plain_repo(tmp_path: Path) -> Path:
    # The .git marker bounds the guard's upward walk at this repository, so the
    # verdict cannot depend on whatever directory holds the test's tmp_path.
    repo = tmp_path / "plain-repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _write_pointer(home: Path, run_id: str, record: dict) -> None:
    live_dir = home / "crew" / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / f"{run_id}.json").write_text(json.dumps(record))


def _payload(cwd: Path) -> dict:
    return {"tool_name": "Agent", "cwd": str(cwd), "tool_input": {}}


# ── Branch 1: non-crew repository passes through ────────────────────────────


def test_non_crew_repository_passes_through(tmp_path: Path) -> None:
    repo = _plain_repo(tmp_path)

    allowed, message = guard.decide(_payload(repo))

    assert allowed is True
    assert message is None


def test_a_tool_other_than_agent_is_never_evaluated(tmp_path: Path) -> None:
    repo = _crew_managed_repo(tmp_path)

    allowed, message = guard.decide(
        {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "ls"}}
    )

    assert allowed is True
    assert message is None


# ── The scope is the repository, wherever in it the session stands ────────


def test_a_session_in_a_subdirectory_is_governed_by_the_repository(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)

    allowed, message = guard.decide(_payload(nested))

    assert allowed is False
    assert "reckon crew dispatch --project proj" in message


def test_a_repository_nested_inside_a_crew_managed_one_is_judged_by_its_own_state(
    tmp_path: Path,
) -> None:
    repo = _crew_managed_repo(tmp_path)
    inner = repo / "vendor" / "inner"
    (inner / ".git").mkdir(parents=True)
    below_inner = inner / "lib"
    below_inner.mkdir()

    for cwd in (inner, below_inner):
        allowed, message = guard.decide(_payload(cwd))
        assert allowed is True, cwd
        assert message is None


def test_a_worktree_git_file_bounds_the_walk_like_a_git_directory(
    tmp_path: Path,
) -> None:
    repo = _crew_managed_repo(tmp_path)
    inner = repo / "checkouts" / "linked"
    inner.mkdir(parents=True)
    (inner / ".git").write_text("gitdir: /elsewhere/.git/worktrees/linked\n")

    allowed, _message = guard.decide(_payload(inner))

    assert allowed is True


def test_the_in_harness_waiver_matches_from_a_subdirectory(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    nested = repo / "docs"
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    attach_command = "reckon crew attach --run r-native-4 --task <task-id>"
    _write_pointer(
        home,
        "r-native-4",
        {
            "run_id": "r-native-4",
            "repo": str(repo.resolve()),
            "launch": "in-harness",
            "task": None,
            "directive": {"attach_with": attach_command},
        },
    )

    allowed, message = guard.decide(_payload(nested))

    assert allowed is True
    assert attach_command in message


# ── Branch 2: crew-prepared in-harness run awaiting attachment ─────────────


def test_in_harness_run_awaiting_attach_is_allowed_and_names_the_bind(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    attach_command = "reckon crew attach --run r-native-1 --task <task-id>"
    _write_pointer(
        home,
        "r-native-1",
        {
            "run_id": "r-native-1",
            "project": "proj",
            "repo": str(repo.resolve()),
            "launch": "in-harness",
            "task": None,
            "directive": {"attach_with": attach_command},
        },
    )

    allowed, message = guard.decide(_payload(repo))

    assert allowed is True
    assert message is not None
    assert attach_command in message


def test_an_already_attached_pointer_does_not_waive_the_guard(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _write_pointer(
        home,
        "r-native-2",
        {
            "run_id": "r-native-2",
            "project": "proj",
            "repo": str(repo.resolve()),
            "launch": "in-harness",
            "task": "already-bound",
            "directive": {
                "attach_with": "reckon crew attach --run r-native-2 --task X"
            },
        },
    )

    allowed, message = guard.decide(_payload(repo))

    assert allowed is False
    assert "reckon crew dispatch" in message


def test_a_pointer_for_a_different_repository_does_not_waive_the_guard(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    other_repo = tmp_path / "elsewhere"
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _write_pointer(
        home,
        "r-native-3",
        {
            "run_id": "r-native-3",
            "project": "proj",
            "repo": str(other_repo.resolve()),
            "launch": "in-harness",
            "task": None,
            "directive": {
                "attach_with": "reckon crew attach --run r-native-3 --task X"
            },
        },
    )

    allowed, _message = guard.decide(_payload(repo))

    assert allowed is False


# ── Branch 3: explicit environment override ─────────────────────────────────


def test_the_environment_override_allows_the_spawn(tmp_path: Path, monkeypatch) -> None:
    repo = _crew_managed_repo(tmp_path)
    monkeypatch.setenv(guard.OVERRIDE_ENV, "1")

    allowed, message = guard.decide(_payload(repo))

    assert allowed is True
    assert guard.OVERRIDE_ENV in message


# ── Branch 4: otherwise refused, teaching the re-route ──────────────────────


def test_otherwise_refused_naming_the_dispatch_shape_and_the_watch_line(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)

    allowed, message = guard.decide(_payload(repo))

    assert allowed is False
    assert "reckon crew dispatch --project proj" in message
    assert "--role investigate" in message
    assert "reckon crew watch --project proj" in message
    assert guard.OVERRIDE_ENV in message


def test_the_refusal_names_the_override_that_teaches_the_escape(
    tmp_path: Path,
) -> None:
    repo = _crew_managed_repo(tmp_path)

    allowed, message = guard.decide(_payload(repo))

    assert allowed is False
    assert f"{guard.OVERRIDE_ENV}=1" in message


def test_the_coordinator_refusal_names_the_inline_path_for_a_lookup(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)

    allowed, message = guard.decide(_payload(repo))

    assert allowed is False
    assert "inline" in message
    assert "reckon crew dispatch" in message


def test_a_crew_worker_is_told_to_read_inline_rather_than_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    monkeypatch.setenv(guard.RUN_ID_ENV, "r-worker-7")

    allowed, message = guard.decide(_payload(repo))

    assert allowed is False
    assert "r-worker-7" in message
    assert "inline" in message
    assert "reckon crew dispatch" not in message
    assert "reckon crew watch" not in message


def test_main_denies_with_a_stdout_decision_the_harness_parses(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = _crew_managed_repo(tmp_path)
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(repo))))

    exit_code = guard.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    decision = json.loads(captured.out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert "reckon crew dispatch" in decision["permissionDecisionReason"]


def test_main_hands_the_waiver_note_to_the_calling_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    attach_command = "reckon crew attach --run r-native-9 --task <task-id>"
    _write_pointer(
        home,
        "r-native-9",
        {
            "run_id": "r-native-9",
            "repo": str(repo.resolve()),
            "launch": "in-harness",
            "task": None,
            "directive": {"attach_with": attach_command},
        },
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(repo))))

    exit_code = guard.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    emitted = json.loads(captured.out)
    assert "permissionDecision" not in emitted["hookSpecificOutput"]
    assert attach_command in emitted["hookSpecificOutput"]["additionalContext"]
    assert attach_command in emitted["systemMessage"]


def test_main_exits_0_silently_on_a_non_crew_repository(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = _plain_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(repo))))

    exit_code = guard.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


# ── Scope test isolation: repository-local only, mounts file irrelevant ────


def test_scope_test_reads_only_repository_local_state_and_ignores_mounts(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path, project="proj")
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    # A mounts file naming a wholly different project/repository. If the scope
    # test consulted it, detection would still succeed only by accident; this
    # asserts it succeeds from repository-local state even when mounts is
    # absent, and fails when the repository-local markers are removed even
    # though mounts.json (if present) would say nothing about this directory.
    (home / "mounts.json").write_text(
        json.dumps({"unrelated-project": str(tmp_path / "nowhere" / "docs")})
    )

    assert guard.crew_managed_projects(repo) == ["proj"]

    unmanaged = _plain_repo(tmp_path)
    assert guard.crew_managed_projects(unmanaged) == []


def test_scope_test_detects_a_flight_marker_without_a_ledger(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / "proj").mkdir(parents=True)
    (repo / "docs" / "state" / "proj" / "flight.yaml").write_text("backends: {}\n")

    assert guard.crew_managed_projects(repo) == ["proj"]


# ── The composer node.py exposes stays in step with the hook's own copy ────


def test_the_hook_and_node_composer_produce_the_same_shape() -> None:
    from_node = dispatch_invocation_shape(project="proj", role="investigate")
    from_hook = guard.DISPATCH_INVOCATION_SHAPE.format(
        project="proj", role="investigate"
    )

    assert from_node == from_hook
