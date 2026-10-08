"""A dispatch holds while the lane's own gate file says the lane is paused.

The gate file is the authority the router reads, and a pause written to it is a
fact dispatch reads rather than a message it must be told. A dispatch whose
resolved backend declares a ``gate_document`` reads it before creating a
pointer or a worktree, and waits — ``ok: false`` with ``error: lane-paused`` —
while the gate says paused or cannot be answered. The declared path is also
checked against the lane document's published ``router_generation_gate``
configuration path, so a pause written to a file the dispatch is not reading
cannot pass unseen.

Every case runs in a temporary ``RECKON_HOME`` against a stub backend, a
temporary gate file and a stub lane document; no real gate file, lane document
or scheduler is read or written.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_admission as dispatch_admission_module
from reckon import cli as cli_module
from reckon import crew

# The CLI cases reuse the stub fleet and git repository another dispatch test
# already builds, so the gate is the only new variable.
pytest_plugins = ("tests.test_dispatch_names_its_backend",)

from reckon.crew import recovery, runs  # noqa: E402
from tests import test_dispatch_names_its_backend as backend_tests  # noqa: E402

dispatch_module = importlib.import_module("reckon.crew.dispatch")

GATE_REASON = "the engine relaunch is in progress"


# ── gate file and lane document fixtures ────────────────────────────────────


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _gate_file(
    tmp_path: Path, *, paused: object = True, reason: object = GATE_REASON
) -> Path:
    payload: dict[str, object] = {}
    if paused is not _ABSENT:
        payload["paused"] = paused
    if reason is not _ABSENT:
        payload["reason"] = reason
    return _write_json(tmp_path / "router-gate.json", payload)


class _Absent:
    """Sentinel distinguishing an omitted key from a JSON null."""


_ABSENT = object()


def _lane_document(tmp_path: Path, *, config_path: object = _ABSENT) -> Path:
    gate: dict[str, object] = {"width": 16, "in_flight": 0, "waiting": 0}
    if config_path is not _ABSENT:
        gate["config_path"] = str(config_path)
    return _write_json(
        tmp_path / "lane.json",
        {
            "state": "measured",
            "running": 0,
            "observed_at": "2026-10-01T00:00:00Z",
            "suggested_shelf_life_seconds": 3600,
            "router_generation_gate": gate,
        },
    )


def _config(
    *, gate_document: object = _ABSENT, lane_document: object = _ABSENT
) -> dict:
    config = deepcopy(backend_tests.CONFIG)
    config["local_backend"] = "alpha"
    alpha = config["backends"]["alpha"]
    # The stub local lane is unmetered, so ``--local`` needs no explicit lane
    # declaration to reach the gate and the gate is the only thing under test.
    alpha.pop("budget_check", None)
    if gate_document is not _ABSENT:
        alpha["gate_document"] = str(gate_document)
    if lane_document is not _ABSENT:
        alpha["lane_document"] = str(lane_document)
    # The gate withholds the dispatch before any launch, so the local lane need
    # never be spawnable for these cases; the explicit ``--backend`` case still
    # needs a name the resolver knows.
    return config


# ── CLI driver ──────────────────────────────────────────────────────────────


def _cli(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    config: dict,
    extra=None,
    dry_run: bool = False,
):
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_a, **_k: None
    )
    result = CliRunner().invoke(
        cli_module.main,
        [
            *backend_tests._arguments(repo, node=node, dry_run=dry_run),
            *(extra or []),
        ],
    )
    payload = backend_tests._payload(result)
    return payload, result


def _live_pointer_ids() -> list[str]:
    return [str(row.get("run_id") or "") for row in crew.list_live()]


# ── the pausing row ─────────────────────────────────────────────────────────


def test_local_dispatch_waits_on_a_paused_gate(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--local reads the gate, waits, and leaves no pointer and no worktree."""
    gate = _gate_file(tmp_path)
    created: list[str] = []
    monkeypatch.setattr(
        dispatch_module,
        "_create_worktree",
        lambda *a, **k: created.append("worktree") or _boom(),
    )
    before = _live_pointer_ids()

    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="paused-local",
        config=_config(gate_document=gate),
        extra=["--local"],
    )

    assert result.exit_code == 75
    assert payload["ok"] is False
    assert payload["error"] == "lane-paused"
    assert payload["reason"] == GATE_REASON
    assert payload["lane_gate"]["state"] == "paused"
    assert payload["lane_gate"]["gate_path"] == str(gate)
    assert _live_pointer_ids() == before
    assert created == []


def test_explicit_backend_dispatch_waits_on_a_paused_gate(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit --backend naming the gated backend waits the same way."""
    gate = _gate_file(tmp_path)
    before = _live_pointer_ids()

    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="paused-named",
        config=_config(gate_document=gate),
        extra=["--backend", "alpha"],
    )

    assert result.exit_code == 75
    assert payload["error"] == "lane-paused"
    assert payload["lane_gate"]["state"] == "paused"
    assert _live_pointer_ids() == before


def test_a_paused_gate_without_a_reason_carries_none(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A paused gate with no reason is valid, and the result carries none."""
    gate = _gate_file(tmp_path, reason=_ABSENT)

    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="paused-no-reason",
        config=_config(gate_document=gate),
        extra=["--local"],
    )

    assert result.exit_code == 75
    assert payload["error"] == "lane-paused"
    assert payload["reason"] is None
    assert payload["lane_gate"]["state"] == "paused"


# ── the proceeding rows ─────────────────────────────────────────────────────


def test_an_open_gate_proceeds(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _gate_file(tmp_path, paused=False)
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="open",
        config=_config(gate_document=gate),
        extra=["--local"],
        dry_run=True,
    )
    assert result.exit_code == 0
    assert payload["lane_gate"]["state"] == "open"


def test_a_gate_with_no_paused_key_proceeds(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _gate_file(tmp_path, paused=_ABSENT, reason=_ABSENT)
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="no-key",
        config=_config(gate_document=gate),
        extra=["--local"],
        dry_run=True,
    )
    assert result.exit_code == 0
    assert payload["lane_gate"]["state"] == "open"


def test_no_gate_declared_proceeds(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="not-declared",
        config=_config(),
        extra=["--local"],
        dry_run=True,
    )
    assert result.exit_code == 0
    assert payload["lane_gate"]["state"] == "not-declared"


def test_a_declared_but_missing_gate_proceeds_and_names_itself(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-gate.json"
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="missing-gate",
        config=_config(gate_document=missing),
        extra=["--local"],
        dry_run=True,
    )
    assert result.exit_code == 0
    assert payload["lane_gate"]["state"] == "declared-but-missing"
    assert "gate path declared but missing" in payload["lane_gate"]["detail"]


def test_a_metered_backend_without_a_gate_proceeds_while_the_local_gate_is_paused(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A backend declaring no gate never reads it, whatever the local gate says."""
    gate = _gate_file(tmp_path)
    config = _config(gate_document=gate)
    # The gate sits on the local backend; the metered one declares none.
    config["backends"]["beta"].pop("gate_document", None)
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="metered-open",
        config=config,
        extra=["--backend", "beta"],
        dry_run=True,
    )
    assert result.exit_code == 0
    assert payload["lane_gate"]["state"] == "not-declared"


# ── the unreadable rows ─────────────────────────────────────────────────────


def _waits(payload: dict, result) -> None:
    assert result.exit_code == 75
    assert payload["ok"] is False
    assert payload["error"] == "lane-paused"
    assert payload["lane_gate"]["state"] == "unreadable"


def test_a_non_boolean_paused_waits(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _gate_file(tmp_path, paused="true")
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="string-paused",
        config=_config(gate_document=gate),
        extra=["--local"],
    )
    _waits(payload, result)
    assert "non-boolean" in payload["lane_gate"]["detail"]


def test_a_file_that_is_not_json_waits(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = tmp_path / "router-gate.json"
    gate.write_text("not json at all", encoding="utf-8")
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="not-json",
        config=_config(gate_document=gate),
        extra=["--local"],
    )
    _waits(payload, result)
    assert "not valid JSON" in payload["lane_gate"]["detail"]


def test_valid_json_that_is_not_an_object_waits(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = tmp_path / "router-gate.json"
    gate.write_text("[1, 2, 3]", encoding="utf-8")
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="not-object",
        config=_config(gate_document=gate),
        extra=["--local"],
    )
    _waits(payload, result)
    assert "not a JSON object" in payload["lane_gate"]["detail"]


def test_a_permission_error_waits(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _gate_file(tmp_path)

    def refuse(path: Path) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(dispatch_admission_module, "_gate_text_reader", refuse)
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="permission",
        config=_config(gate_document=gate),
        extra=["--local"],
    )
    _waits(payload, result)
    assert "cannot be read" in payload["lane_gate"]["detail"]


def test_a_read_past_its_deadline_waits(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _gate_file(tmp_path)
    monkeypatch.setattr(dispatch_admission_module, "LANE_GATE_READ_DEADLINE_SECONDS", 0.05)

    def stalled(path: Path) -> str:
        time.sleep(1.0)
        return "{}"

    monkeypatch.setattr(dispatch_admission_module, "_gate_text_reader", stalled)
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="deadline",
        config=_config(gate_document=gate),
        extra=["--local"],
    )
    _waits(payload, result)
    assert "exceeded" in payload["lane_gate"]["detail"]


def test_a_declared_path_that_differs_from_the_published_one_waits(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _gate_file(tmp_path)
    published = tmp_path / "somewhere-else.json"
    lane = _lane_document(tmp_path, config_path=published)
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="mismatch",
        config=_config(gate_document=gate, lane_document=lane),
        extra=["--local"],
    )
    _waits(payload, result)
    detail = payload["lane_gate"]["detail"]
    assert str(gate) in detail and str(published) in detail
    assert payload["lane_gate"]["path_check"] == "mismatch"


# ── the skipped path comparison ─────────────────────────────────────────────


def test_a_lane_document_without_a_config_path_skips_the_comparison(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _gate_file(tmp_path, paused=False)
    lane = _lane_document(tmp_path)  # gate block, but no config_path
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="no-config-path",
        config=_config(gate_document=gate, lane_document=lane),
        extra=["--local"],
        dry_run=True,
    )
    assert result.exit_code == 0
    assert payload["lane_gate"]["state"] == "open"
    assert payload["lane_gate"]["path_check"] == "skipped"
    assert payload["lane_gate"]["path_check_detail"]


def test_a_lane_read_past_its_deadline_skips_the_comparison(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _gate_file(tmp_path, paused=False)
    lane = _lane_document(tmp_path, config_path=gate)
    monkeypatch.setattr(dispatch_admission_module, "LANE_GATE_READ_DEADLINE_SECONDS", 0.05)
    real_reader = dispatch_module._gate_text_reader

    def stalled_for_lane(path: Path) -> str:
        if Path(path) == lane:
            time.sleep(1.0)
        return real_reader(path)

    monkeypatch.setattr(dispatch_admission_module, "_gate_text_reader", stalled_for_lane)
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="lane-deadline",
        config=_config(gate_document=gate, lane_document=lane),
        extra=["--local"],
        dry_run=True,
    )
    assert result.exit_code == 0
    assert payload["lane_gate"]["state"] == "open"
    assert payload["lane_gate"]["path_check"] == "skipped"
    assert "exceeded" in payload["lane_gate"]["path_check_detail"]


def test_a_matching_published_path_records_the_comparison(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _gate_file(tmp_path, paused=False)
    lane = _lane_document(tmp_path, config_path=gate)
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="matched",
        config=_config(gate_document=gate, lane_document=lane),
        extra=["--local"],
        dry_run=True,
    )
    assert result.exit_code == 0
    assert payload["lane_gate"]["state"] == "open"
    assert payload["lane_gate"]["path_check"] == "matched"


# ── the follower's review reflex ────────────────────────────────────────────


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A dispatch holds while the lane is paused</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )

    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    worktrees: list[str] = []

    def prepare_worktree(_repo, session, node, base):
        worktrees.append(node)
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repo, worktrees


def _scoring_pointer(config_home: Path, repo: Path, run_id: str) -> dict:
    manifest = config_home / "manifests" / (run_id + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + run_id + "\nstatus: complete\ncommits: " + run_id + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo),
        "node": {"id": run_id, "plan": "fixture", "section": "s2"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _review_runs() -> list[dict]:
    return [
        row
        for row in runs.list_live(project="sample")
        if row["node"].get("id", "").startswith(recovery.REVIEW_NODE_PREFIX)
    ]


def _review_config(gate: Path | None) -> dict:
    alpha = {
        "launch": "cli",
        "command": "codex",
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "time_budget": "25m",
    }
    if gate is not None:
        alpha["gate_document"] = str(gate)
    return {
        "default_backend": "alpha",
        "local_backend": "alpha",
        "backends": {
            "alpha": alpha,
            "beta": {
                "launch": "cli",
                "command": "claude",
                "model": "other-model",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            },
        },
        "roles": {"implement": {}, "review": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


def test_the_review_reflex_launches_nothing_while_the_gate_is_paused(
    tmp_path: Path, isolated_project
) -> None:
    """The sweep's report row carries error lane-paused and the reason."""
    _config_home, repo, worktrees = isolated_project
    gate = _gate_file(tmp_path)
    record = _scoring_pointer(tmp_path / "config", repo, "r-paused-review")

    report = recovery.dispatch_review_for_run(
        record,
        config=_review_config(gate),
        launcher=lambda *a, **k: os.getpid(),
    )

    assert report["dispatched"] is False
    assert report["error"] == "lane-paused"
    assert report["reason"] == GATE_REASON
    assert report["lane_gate"]["state"] == "paused"
    assert _review_runs() == []
    assert worktrees == []
    stored = runs.read_pointer("r-paused-review")[recovery.REVIEW_DISPATCH_FIELD]
    assert stored["status"] == "lane-paused"
    assert GATE_REASON in stored["reason"]


def test_the_review_reflex_dispatches_through_an_open_gate(
    tmp_path: Path, isolated_project
) -> None:
    """The positive control: an open gate must not become a default hold."""
    _config_home, repo, _worktrees = isolated_project
    gate = _gate_file(tmp_path, paused=False)
    record = _scoring_pointer(tmp_path / "config", repo, "r-open-review")

    report = recovery.dispatch_review_for_run(
        record,
        config=_review_config(gate),
        launcher=lambda *a, **k: os.getpid(),
    )

    assert report.get("error") != "lane-paused"
    assert report["dispatched"] is True
    assert _review_runs() != []


def _boom():  # pragma: no cover - reached only if the gate check is bypassed
    raise AssertionError("a held dispatch must create no worktree")
