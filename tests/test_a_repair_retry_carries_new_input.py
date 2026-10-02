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

from reckon import crew
from reckon.crew import recovery, resumption, runs
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

# The refusal manifest an ended turn writes: terminal status, and the round's
# own advice quoted as the blocker.
REFUSAL_MANIFEST = (
    "node: {node}\n"
    "status: blocked\n"
    "blockers: the round's advice cannot be carried out — "
    "Write scope for this round: reckon/crew/thing.py\n"
)


def _write_refusal_manifest(record: dict) -> None:
    Path(record["manifest_path"]).write_text(
        REFUSAL_MANIFEST.format(node=record["node"]["id"]), encoding="utf-8"
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
    config_home, repo, head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)
    if os.environ.get(MUTATION_ENV) == "1":
        monkeypatch.setattr(
            recovery,
            "_reviewed_run_refused_the_round",
            lambda *_args, **_kwargs: False,
            raising=False,
        )

    _sweep()
    first = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert first["status"] == "resumed"
    assert first["attempt"] == 1

    _write_refusal_manifest(record)
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
    config_home, repo, head_sha = request.getfixturevalue("isolated_project")
    _completed_pointer(config_home, repo)
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


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(DECLARED_MUTATION)
    os.environ[MUTATION_ENV] = "1"
    raise SystemExit(pytest.main(["-p", "no:cacheprovider", "-q", str(Path(__file__))]))
