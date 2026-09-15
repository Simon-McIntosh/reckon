"""The pre-tool-use guard on peer sends addressed to live crew workers."""

from __future__ import annotations

import io
import json
import socket
from pathlib import Path

from reckon.hooks import worker_message_guard as guard


def _crew_managed_repo(tmp_path: Path, *, project: str = "proj") -> Path:
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / project).mkdir(parents=True)
    (repo / "docs" / "state" / project / "crew.json").write_text("{}")
    return repo


def _plain_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "plain-repo"
    repo.mkdir()
    return repo


def _write_pointer(
    home: Path,
    run_id: str,
    record: dict,
    *,
    node_id: str = "some-node",
    member: str = "worker",
    session: str = "session-1",
) -> None:
    live_dir = home / "crew" / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "project": "proj",
        "repo": str(home / "repo"),
        "launcher_host": socket.gethostname(),
        "node": {"id": node_id},
        "member": member,
        "session": session,
        "session_id": f"{run_id}-session-id",
    }
    payload.update(record)
    (live_dir / f"{run_id}.json").write_text(json.dumps(payload))


def _payload(cwd: Path, *, to: str, tool: str = "SendMessage") -> dict:
    return {
        "tool_name": tool,
        "cwd": str(cwd),
        "tool_input": {"to": to, "message": "hello", "summary": "steer"},
    }


# ── Quiet half: a send that is NOT addressed to a live worker passes ────────


def test_send_to_a_recipient_that_is_not_a_live_worker_is_left_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    _write_pointer(home, "r-other", {}, node_id="someone-elses-node")

    allowed, message = guard.decide(_payload(repo, to="a-name-nobody-claims"))

    assert allowed is True
    assert message is None


def test_send_from_a_repository_without_crew_state_is_left_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _plain_repo(tmp_path)
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)

    allowed, message = guard.decide(_payload(repo, to="worker"))

    assert allowed is True
    assert message is None


def test_a_tool_other_than_send_message_is_never_evaluated(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    _write_pointer(home, "r-other", {}, node_id="some-node")

    allowed, message = guard.decide(_payload(repo, to="some-node", tool="Bash"))

    assert allowed is True
    assert message is None


def test_a_send_with_no_recipient_is_left_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    _write_pointer(home, "r-other", {}, node_id="some-node")

    payload = _payload(repo, to="some-node")
    payload["tool_input"] = {"message": "hello"}

    allowed, message = guard.decide(payload)

    assert allowed is True
    assert message is None


def test_a_pointer_launched_on_a_different_host_is_not_matched(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    foreign_host = "somedifferent-host"
    if foreign_host == socket.gethostname():
        foreign_host = "host-that-is-not-this-one"
    _write_pointer(home, "r-foreign", {"launcher_host": foreign_host})

    allowed, message = guard.decide(_payload(repo, to="some-node"))

    assert allowed is True
    assert message is None


# ── Loud half: a send addressed to a live worker is refused ─────────────────


def test_send_to_a_live_worker_by_node_id_is_refused_naming_run_and_resume(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    _write_pointer(home, "r-live-1", {}, node_id="a-working-node")

    allowed, message = guard.decide(_payload(repo, to="a-working-node"))

    assert allowed is False
    assert "r-live-1" in message
    assert "reckon crew resume --run r-live-1" in message


def test_send_to_a_live_worker_by_run_id_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    _write_pointer(home, "r-live-2", {})

    allowed, message = guard.decide(_payload(repo, to="r-live-2"))

    assert allowed is False
    assert "r-live-2" in message


def test_send_to_a_live_worker_by_member_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    _write_pointer(home, "r-live-3", {}, member="drain-worker")

    allowed, message = guard.decide(_payload(repo, to="drain-worker"))

    assert allowed is False
    assert "r-live-3" in message


def test_send_to_a_live_worker_by_session_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    _write_pointer(home, "r-live-4", {}, session="campaign-session")

    allowed, message = guard.decide(_payload(repo, to="campaign-session"))

    assert allowed is False
    assert "r-live-4" in message


# ── Branch 3: explicit environment override ─────────────────────────────────


def test_the_environment_override_allows_the_send(tmp_path: Path, monkeypatch) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv(guard.OVERRIDE_ENV, "1")
    _write_pointer(home, "r-live-5", {})

    allowed, message = guard.decide(_payload(repo, to="some-node"))

    assert allowed is True
    assert guard.OVERRIDE_ENV in message


# ── main() contract: refuse on stderr with exit 2, pass silently ────────────


def test_main_exits_2_and_denies_on_stderr_for_a_refusal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    _write_pointer(home, "r-live-6", {})
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_payload(repo, to="some-node")))
    )

    exit_code = guard.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "r-live-6" in payload["systemMessage"]


def test_main_exits_0_on_a_send_to_a_non_live_recipient(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _crew_managed_repo(tmp_path)
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(_payload(repo, to="nobody")))
    )

    exit_code = guard.main()

    assert exit_code == 0
