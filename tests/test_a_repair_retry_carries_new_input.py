"""A repair retry only when it carries input the ended turn did not have.

The reflex resumes a reviewed run once, retries once more if the resumed turn
ends without answering, and then exhausts the round. Two things the retry must
respect, each measured on the same shape — a round already resumed once, its
worker gone, the reviewed head unmoved, so ``_reviewed_run_is_busy`` reads the
run free and the retry gate is reached:

* a turn that ended by *refusing* the round's advice — its own manifest terminal
  and quoting that advice — is a dead end, and re-sending byte-identical advice
  resumes nothing, so the round records exhausted rather than spending a second
  worker on the same refusal;
* a turn that died mid-work leaves the run's manifest untouched, so a retry
  carries into a dead process the same way it always did.

A third case is about the resumed attempt's own clock: the fence a resume
composes is restated for the attempt it launches, and the prompt the worker
actually reads is the one carrying that fence rather than the bare advice, so no
prompt text states the first attempt's deadline.

The declared negative control restores the unconditional same-round retry, and
the refused-advice case must then turn red.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import crew
from reckon.crew import recovery, recovery_repair_dispatch, resumption, runs
from tests import test_resume_and_lane_change_follow_their_own_attempt as lane
from tests.test_resume_and_lane_change_follow_their_own_attempt import (  # noqa: F401
    crew_home,
    operator_home,
    repo,
)
from tests.test_the_reflex_resumes_an_unpromoted_run import (
    CONFIG,
    FINDING,
    RUN_ID,
    _completed_pointer,
    _store_review,
    _stub_dispatch,
    _stub_resume,
    _sweep,
    isolated_project,  # noqa: F401 - registered as a fixture, requested by name
)

pytestmark = pytest.mark.arms_watch_producer

# The declared mutation, printed verbatim as the red log's first line.
DECLARED_MUTATION = (
    "restore the unconditional same-round retry; the refused-advice case must fail"
)

MUTATION_ENV = "RECKON_REPAIR_RETRY_NEGATIVE_CONTROL"

# The lane-change mutation: drop the restated fence, so the written prompt is the
# bare advice and the lane-change case reads the first attempt's clock.
LANE_MUTATION_ENV = "RECKON_LANE_CHANGE_FENCE_NEGATIVE_CONTROL"
LANE_DECLARED_MUTATION = (
    "write the lane-change prompt without restating the fence; "
    "the lane-change case must fail"
)

# The round-scope mutation: match the scope line alone, so any round's refusal
# reads as this round's and the earlier-round case stops resuming.
ROUND_MUTATION_ENV = "RECKON_REPAIR_ROUND_SCOPE_NEGATIVE_CONTROL"
ROUND_DECLARED_MUTATION = (
    "match the scope line instead of the round token; the earlier-round case "
    "must fail"
)


def _unscoped_refusal(record: dict, round_id: str) -> bool:
    """The reading the round-scope mutation restores: scope line alone."""
    path = str(record.get("manifest_path") or "")
    if not path:
        return False
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return recovery.REPAIR_ADVICE_SCOPE_LINE in text


@pytest.fixture(autouse=True)
def _declared_negative_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply a declared mutation when its environment variable is set."""
    if os.environ.get(LANE_MUTATION_ENV) == "1":
        monkeypatch.setattr(
            dispatch_sessions_module,
            "_restate_time_fence",
            lambda prompt, record, *, attempt_started_at: prompt,
        )
    if os.environ.get(ROUND_MUTATION_ENV) == "1":
        monkeypatch.setattr(
            recovery_repair_dispatch, "_reviewed_run_refused_the_round", _unscoped_refusal
        )

# The refusal manifest an ended turn writes: terminal status, and the round
# token the advice opens with quoted as the blocker. The token names the round,
# so the retry reads back which round was refused.
REFUSAL_MANIFEST = (
    "node: {node}\n"
    "status: blocked\n"
    "blockers: the round's advice cannot be carried out — {token}"
    "Write scope for this round: reckon/crew/thing.py\n"
)


def _round_token(advice: str) -> str:
    """The round token line from the advice the sweep composed."""
    for line in advice.splitlines():
        if line.startswith(recovery.REPAIR_ROUND_TOKEN_LINE):
            return line
    raise AssertionError(f"advice carries no round token line: {advice!r}")


def _write_refusal_manifest(record: dict, advice: str) -> None:
    Path(record["manifest_path"]).write_text(
        REFUSAL_MANIFEST.format(node=record["node"]["id"], token=_round_token(advice)),
        encoding="utf-8",
    )


def test_a_same_round_retry_after_a_refusal_is_exhausted_not_resumed(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ended turn refused the advice: a retry re-sends the same dead end.

    The first sweep resumes the round. The resumed turn then ends by refusing
    it — its manifest is terminal and quotes the round's advice — and the worker
    is gone with the reviewed head unmoved. The second sweep must record the
    round exhausted rather than resume a second worker into the same refusal.
    """
    config_home, project_repo, head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, project_repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)
    if os.environ.get(MUTATION_ENV) == "1":
        monkeypatch.setattr(
            recovery_repair_dispatch,
            "_reviewed_run_refused_the_round",
            lambda *_args, **_kwargs: False,
            raising=False,
        )

    _sweep()
    first = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert first["status"] == "resumed"
    assert first["attempt"] == 1

    _write_refusal_manifest(record, resumed[0]["advice"])
    _sweep()

    assert len(resumed) == 1, "a refused advice was re-sent into the same dead end"
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "exhausted", recorded


def test_a_same_round_retry_after_a_mid_work_death_still_resumes(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ended turn died mid-work, its manifest untouched: the retry stands.

    The refusal guard reads the reviewed run's own manifest. A turn that died
    before writing one leaves that manifest where it was, so the guard must not
    fire and the retry must reach a second resume exactly as before.
    """
    config_home, project_repo, head_sha = request.getfixturevalue("isolated_project")
    _completed_pointer(config_home, project_repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()
    _sweep()

    assert len(resumed) == 2
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "resumed"
    assert recorded["attempt"] == 2


def test_a_resumed_attempt_prompt_carries_its_own_deadline_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt a resumed worker reads states the resumed attempt's clock.

    ``_resume`` launches the plan ``resume_plan`` built, whose prompt carries the
    fence restated for the attempt now starting. The worker reads the file at the
    launcher's ``prompt_path``, so that file must be the plan's own prompt — the
    fence naming this attempt's launch and deadline — and must not state the
    first attempt's clock. Here the plan is stubbed with the restated prompt a
    real resume would build; the assertion is that it is what reaches disk.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    run_id = "r-resumed-attempt"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "manifest.md"
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": "",
        "backend": "alpha",
        "launch": "cli",
        "worktree": str(tmp_path / "tree"),
        "manifest_path": str(manifest),
        "session": "session-orchestrating",
        "attempt": 1,
    }
    crew._write_json(runs.pointer_path(run_id), record)

    first_launch = "2031-01-01T00:00:00Z"
    resumed_launch = "2031-01-01T01:00:00Z"
    own_deadline = "2031-01-01T02:00:00Z"
    aged_prompt = (
        "An independent review of this run found 1 blocking finding.\n\n"
        "FENCE — TIME (resumed attempt)\n"
        f"  Launched {resumed_launch}; deadline {own_deadline} — 60m from launch\n"
    )
    plan = SimpleNamespace(stdin_text=aged_prompt, argv=[], cwd=str(tmp_path))
    monkeypatch.setattr(resumption, "resume_plan", lambda *a, **k: plan)
    captured: dict[str, str] = {}

    def launcher(_plan, *, log_path, stderr_path, prompt_path):
        captured["prompt_path"] = str(prompt_path)
        return 4242

    resumption._resume(
        run_id, record, config=CONFIG, launcher=launcher, advice="the bare advice"
    )

    text = Path(captured["prompt_path"]).read_text(encoding="utf-8")
    assert "FENCE — TIME (resumed attempt)" in text
    assert own_deadline in text
    assert resumed_launch in text
    assert first_launch not in text


def test_a_manifest_quoting_an_earlier_round_does_not_suppress_the_retry(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal marker is round-scoped: another round's advice does not count.

    The scope line is identical in every round's advice, so a terminal manifest
    quoting an earlier round's advice must not read as this round's refusal — the
    retry still runs, carrying advice the ended turn had not seen.
    """
    config_home, project_repo, head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, project_repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()
    Path(record["manifest_path"]).write_text(
        REFUSAL_MANIFEST.format(
            node=record["node"]["id"],
            token=recovery.REPAIR_ROUND_TOKEN_LINE + "an-earlier-round",
        ),
        encoding="utf-8",
    )
    _sweep()

    assert len(resumed) == 2, "an earlier round's refusal suppressed this round's retry"
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "resumed"


def test_a_lane_change_prompt_carries_the_attempt_its_own_fence(
    operator_home: Path,  # noqa: F811 - imported fixture, requested by name
    crew_home: Path,  # noqa: F811 - imported fixture, requested by name
    repo: Path,  # noqa: F811 - imported fixture, requested by name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane change restates the fence for the attempt it launches.

    The lane-change prompt is composed for the attempt that ended — the bare
    advice for a continued session — and without restating the fence the worker
    reads the first attempt's clock, exactly the three nova-49 refusals. The
    prompt written for the lane change must carry a resumed-attempt fence.
    """
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    run_id = "r-lane-fence"
    record = lane._stopped_pointer(tmp_path, repo, run_id, backend="alpha")
    monkeypatch.setattr(
        dispatch_sessions_module, "plan_dispatch", lambda **kwargs: lane._cli_resolution("beta")
    )
    lane._stub_move_gates(monkeypatch)

    lane.dispatch_module.change_lane(
        run_id,
        "beta",
        "the lane is spent",
        config=lane.CONFIG,
        advice="answer the finding in your own worktree",
        launch=True,
        launcher=lambda *args, **kwargs: 4242,
    )

    directory = crew.run_dir(run_id)
    prompt_files = sorted(directory.glob("lane-change-*-prompt.txt"))
    assert prompt_files, "the lane change wrote no prompt file"
    text = prompt_files[-1].read_text(encoding="utf-8")
    assert "FENCE — TIME (resumed attempt)" in text, text
    assert record["node"]["time_budget"] in text


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    for mutation in (
        DECLARED_MUTATION,
        LANE_DECLARED_MUTATION,
        ROUND_DECLARED_MUTATION,
    ):
        print(mutation)
    os.environ[MUTATION_ENV] = "1"
    raise SystemExit(pytest.main(["-p", "no:cacheprovider", "-q", str(Path(__file__))]))
