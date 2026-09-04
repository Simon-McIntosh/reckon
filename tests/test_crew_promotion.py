"""Promotion records narrative and measurements at their durable homes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, _store, crew, ledger
from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path


PROJECT = "proj"
PLAN = "plan-a"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def test_promotion_splits_narrative_from_run_measurements(repository: Path) -> None:
    run_id = "r-20260824T190800000000-node-a"
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-08-24T19:08:00Z",
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "35m",
                "write_paths": [],
            },
        },
    )

    narrative = "The shared write boundary now refuses an unclaimed closure."
    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome=narrative,
        tests_added=23,
        completed_at="2026-08-24T19:10:17Z",
        root=repository,
    )

    plan, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    comment = plan["comments"]["s2"][0]
    assert promoted["plan_comment"] == {
        "recorded": True,
        "comment_id": comment["id"],
        "section": "s2",
        "already_recorded": False,
    }
    assert narrative in comment["body"]
    assert "23" not in comment["body"]
    assert "137" not in comment["body"]

    run = ledger.load(PROJECT, repository)[0]["runs"][0]
    assert run["tests_added"] == 23
    assert run["wall_seconds"] == 137
    assert run["gate"] == "passed"
    assert run["outcome"] == ""
    assert narrative not in json.dumps(run)
    assert not (repository / "docs" / "evidence").exists()


def test_terminal_write_requires_a_back_linking_evidence_record(
    repository: Path,
) -> None:
    plan, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    with pytest.raises(_store.OpError) as excinfo:
        _store.write_plan(
            PROJECT,
            PLAN,
            {**plan, "status": "done"},
            version,
            repository,
            artifact_type="plan",
        )

    detail = str(excinfo.value)
    assert f"docs/evidence/archive/{PLAN}-landed.html" in detail
    assert f'plan-evidence-for" content="{PLAN}' in detail
    with pytest.raises(_store.OpError, match=f"{PLAN}-landed.html"):
        _store.validate_landing_patch({**plan, "status": "done"}, {"status": "done"})
    refused, refused_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    assert refused["status"] == "active"
    assert refused_version == version

    evidence_path = repository / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"
    _write_resource(
        evidence_path,
        {
            "type": "evidence",
            "slug": f"{PLAN}-landed",
            "title": "Plan A execution evidence",
            "evidence_for": [PLAN],
            "version": 0,
        },
    )
    assert ledger.evidence_records_for_plan(PROJECT, PLAN, repository) == [
        evidence_path
    ]

    new_version = _store.write_plan(
        PROJECT,
        PLAN,
        {**refused, "status": "done"},
        refused_version,
        repository,
        artifact_type="plan",
    )
    terminal, stored_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    assert new_version == stored_version == refused_version + 1
    assert terminal["status"] == "done"


def test_a_commit_from_another_repository_names_that_repository(
    tmp_path, monkeypatch
) -> None:
    """A sha that resolves elsewhere is a routing mistake, not a bad sha.

    A node dispatched without ``--repo`` has its run repository set to the
    dispatching one, so a commit it made in a foreign checkout cannot resolve
    and the refusal is correct. Stating only that it does not resolve leaves the
    reader to guess; naming the repository it does belong to makes the refusal
    the instruction for the next dispatch.
    """
    from reckon.crew import promotion

    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    def _repo(name: str) -> Path:
        root = tmp_path / name
        (root / "docs").mkdir(parents=True)
        (root / "seed.txt").write_text(f"{name}\n")
        for arguments in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "worker@example.invalid"],
            ["config", "user.name", "Worker"],
            ["add", "seed.txt"],
            ["commit", "-q", "-m", "chore: seed"],
        ):
            subprocess.run(
                ["git", *arguments], cwd=root, check=True, capture_output=True
            )
        return root

    run_repo = _repo("dispatching")
    foreign = _repo("written-to")
    (config_home / "mounts.json").write_text(
        json.dumps(
            {
                "dispatching": str(run_repo / "docs"),
                "written-to": str(foreign / "docs"),
            }
        )
    )
    stray = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=foreign,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(crew.CrewError) as refusal:
        promotion._resolve_commits(cwd=run_repo, revisions=[stray], run_id="r-x")

    message = str(refusal.value)
    assert str(run_repo) in message, "the repository it was checked against"
    assert str(foreign) in message, "the repository it belongs to"
    assert f"--repo {foreign}" in message, "the remedy for the next dispatch"


def test_an_unresolvable_commit_says_what_else_to_check(tmp_path, monkeypatch) -> None:
    """With no other repository holding it, the likely cause is a worker that
    staged without committing — so the refusal says that instead of guessing."""
    from reckon.crew import promotion

    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "solo"
    root.mkdir()
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["commit", "-q", "--allow-empty", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)

    with pytest.raises(crew.CrewError) as refusal:
        promotion._resolve_commits(cwd=root, revisions=["0" * 40], run_id="r-y")

    assert "committed rather than only staging" in str(refusal.value)


# ── A promotion that would delete a resume path ─────────────────────────────
#
# Promotion removes the pointer, and the pointer is where a resume finds its
# session. The measured loss: a wave refused per-request on a spend limit, the
# blocked runs promoted, and sixty-four turns of worker orientation discarded
# while promotion reported success. The refusal below is the one this operation
# was missing; the waiver is what keeps a deliberate discard possible and
# afterwards legible.

STREAM_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "backends" / "claude-turn.jsonl"
)


def _blocked_pointer(
    repository: Path,
    run_id: str,
    *,
    manifest: Path,
    session_id: str = "",
    stream: Path | None = None,
    status: str = "blocked",
) -> None:
    """A pointer and manifest in the shape dispatch leaves behind."""
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: node-a\nstatus: {status}\ncommits: none\n"
        "blockers: the provider refused the turn on a spend limit\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "cli" if stream else "in-harness",
        "role": "implement",
        "member": "worker-a",
        "backend": "beta",
        "created_at": "2026-09-03T10:00:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "§2",
            "time_budget": "35m",
            "write_paths": [],
        },
    }
    if session_id:
        record["session_id"] = session_id
    if stream:
        # What dispatch writes for a CLI backend: the argv it launched and the
        # stream that turn produced, which is where a resume re-reads a session
        # id the pointer never carried.
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_bytes(STREAM_FIXTURE.read_bytes())
        record["argv"] = ["claude", "-p"]
        record["log_path"] = str(stream)
    _write_json(pointer_path(run_id), record)


def _stream_session_id() -> str:
    """The session the fixture stream carries, read the way promotion reads it."""
    events = [
        json.loads(line)
        for line in STREAM_FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    found = next(
        (str(event["session_id"]) for event in events if event.get("session_id")), ""
    )
    assert found
    return found


def _real_crew_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where this workstation's pointers actually live, with the patch lifted."""
    with monkeypatch.context() as fresh:
        fresh.delenv("RECKON_HOME", raising=False)
        return crew.crew_home()


def test_promoting_a_blocked_run_with_a_live_session_is_refused(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal has to be actionable: the remedy and the session it found."""
    run_id = "r-20260903T100000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="sess-live-1",
    )

    with pytest.raises(crew.CrewError) as excinfo:
        crew.complete(
            run_id,
            gate="not-run",
            outcome="the provider refused the turn",
            root=repository,
        )

    message = str(excinfo.value)
    assert "reckon crew resume" in message
    assert run_id in message
    assert "sess-live-1" in message
    # Refused means nothing happened: the pointer a resume needs is still there
    # and the ledger has no row for the run.
    assert pointer_path(run_id).is_file()
    data, _version = ledger.load(PROJECT, root=repository)
    assert [item for item in data["runs"] if item["run_id"] == run_id] == []
    assert not (_real_crew_home(monkeypatch) / "live" / f"{run_id}.json").exists()


def test_a_session_only_in_the_stream_also_refuses(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A null session id on the pointer is not evidence of an unresumable run.

    Resume already recovers this case by re-reading the stream, so a promotion
    that consults only the pointer discards a session that was there all along
    — which is the misreading the loss turned on.
    """
    run_id = "r-20260903T101000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        stream=tmp_path / "runs" / run_id / "stream.jsonl",
    )
    assert not (json.loads(pointer_path(run_id).read_text()).get("session_id"))

    with pytest.raises(crew.CrewError) as excinfo:
        crew.complete(
            run_id,
            gate="not-run",
            outcome="the provider refused the turn",
            root=repository,
        )

    message = str(excinfo.value)
    assert _stream_session_id() in message
    assert "reckon crew resume" in message
    assert pointer_path(run_id).is_file()


def test_a_stated_waiver_promotes_and_lands_on_the_ledger_row(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deliberate discard stays possible, and stays legible afterwards."""
    run_id = "r-20260903T102000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="sess-live-2",
    )
    reason = "the node was re-scoped and its orientation no longer applies"

    promoted = crew.complete(
        run_id,
        gate="not-run",
        outcome="the provider refused the turn",
        root=repository,
        resume_waiver=reason,
    )

    waiver = promoted["record"]["resume_waiver"]
    assert waiver["reason"] == reason
    assert waiver["session_id"] == "sess-live-2"
    assert waiver["source"] == "pointer"
    data, _version = ledger.load(PROJECT, root=repository)
    row = next(item for item in data["runs"] if item["run_id"] == run_id)
    assert row["resume_waiver"] == waiver
    assert promoted["pointer_removed"] is True
    assert not (_real_crew_home(monkeypatch) / "live" / f"{run_id}.json").exists()


def test_complete_command_accepts_a_reasoned_resume_path_waiver(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public command reaches the promotion guard and its recorded waiver."""
    run_id = "r-20260903T102500000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        stream=tmp_path / "runs" / run_id / "stream.jsonl",
    )
    real_pointer = _real_crew_home(monkeypatch) / "live" / f"{run_id}.json"
    assert not real_pointer.exists()
    arguments = [
        "crew",
        "complete",
        "--run",
        run_id,
        "--gate",
        "not-run",
        "--outcome",
        "the provider refused the turn",
    ]

    refused = CliRunner().invoke(cli_main, arguments)

    assert refused.exit_code == 1
    assert "--waive-resume-path REASON" in refused.output
    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []

    reason = "the replacement run has already recovered the useful context"
    accepted = CliRunner().invoke(
        cli_main,
        [*arguments, "--waive-resume-path", reason],
    )

    assert accepted.exit_code == 0, accepted.output
    waiver = json.loads(accepted.output)["record"]["resume_waiver"]
    assert waiver == {
        "session_id": _stream_session_id(),
        "source": "stream",
        "reason": reason,
    }
    row = next(
        item
        for item in ledger.runs(PROJECT, root=repository)
        if item["run_id"] == run_id
    )
    assert row["resume_waiver"] == waiver
    assert not pointer_path(run_id).exists()
    assert not real_pointer.exists()


def test_complete_command_refuses_a_resume_path_waiver_without_a_reason(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260903T102600000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="sess-live-command",
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "not-run",
            "--outcome",
            "the provider refused the turn",
            "--waive-resume-path",
            "  ",
        ],
    )

    assert result.exit_code == 1
    assert "--waive-resume-path REASON" in result.output
    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_complete_command_promotes_a_blocked_run_without_a_recoverable_session(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260903T102700000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "not-run",
            "--outcome",
            "the provider refused the turn",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "resume_waiver" not in json.loads(result.output)["record"]
    assert not pointer_path(run_id).exists()


def test_complete_help_describes_the_recoverable_session_refusal() -> None:
    result = CliRunner().invoke(cli_main, ["crew", "complete", "--help"])

    assert result.exit_code == 0
    assert "--waive-resume-path REASON" in result.output
    assert "promotion refused because" in result.output
    assert "session is still recoverable" in result.output


def test_a_blocked_run_with_no_recoverable_session_promotes_unchanged(
    repository: Path, tmp_path: Path
) -> None:
    """The existing path has to stay reachable, or the guard is a wall."""
    run_id = "r-20260903T103000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
    )

    promoted = crew.complete(
        run_id,
        gate="not-run",
        outcome="the provider refused the turn",
        root=repository,
    )

    assert promoted["pointer_removed"] is True
    assert "resume_waiver" not in promoted["record"]


def test_a_passing_gate_promotes_whatever_its_session(
    repository: Path, tmp_path: Path
) -> None:
    """Delivered work is not a resume candidate, however live its session."""
    run_id = "r-20260903T104000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="sess-live-3",
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the node landed",
        root=repository,
    )

    assert promoted["pointer_removed"] is True
    assert "resume_waiver" not in promoted["record"]
    assert promoted["record"]["session_id"] == "sess-live-3"


def test_a_run_terminal_for_another_reason_is_unaffected(
    repository: Path, tmp_path: Path
) -> None:
    """Only the blocked classification is guarded; a delivery is not."""
    run_id = "r-20260903T105000000000-node-a"
    _blocked_pointer(
        repository,
        run_id,
        manifest=tmp_path / "manifests" / f"{run_id}.md",
        session_id="sess-live-4",
        status="complete",
    )

    promoted = crew.complete(
        run_id,
        gate="not-run",
        outcome="delivered, gate not run in this tier",
        root=repository,
    )

    assert promoted["pointer_removed"] is True
    assert "resume_waiver" not in promoted["record"]
