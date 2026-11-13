"""A coordinator can hold a delivered run, and a resume never vanishes.

The automatic lander promotes a delivered run as soon as its evidence reads
clean, and a promotion deletes the live pointer. A coordinator that had planned
to steer the delivery found its resume printing nothing and raising nothing,
because the run it named had already been merged; the repair had to be carried
as a new plan section.

These cases drive the two automatic doors and the resume door against one
temporary repository and crew home:

* a delivered, cleanly reviewed run its coordinator has held is neither
  promoted by the acceptance sweep nor landed by a landing pass, and lands once
  the hold is released;
* a resume of a run a promotion already owns exits non-zero and names the
  revision the promotion asserted;
* a resume of a delivered, unheld run records the hold before the worker is
  relaunched, so a landing pass running between the two leaves it unlanded;
* the hold and its release both stay on the run with the reasons given.

The negative control removes the hold from the landing pass and shows the held
run landing, so the first case is the guard's doing rather than something else
in the pass.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon.crew import (
    landing_measure,
    queue_landing,
    recovery_review_acceptance,
    resume,
    runs,
)
from reckon.crew import (
    review as review_module,
)

PROJECT = "hold-fixture"
PLAN = "delivery-plan"
SESSION = "owner"
RUN = "r-held-delivery"
SUITE = "python -m pytest -q tests/test_alpha.py"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_plan(repo: Path) -> None:
    path = repo / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    authored = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<title>Delivery plan</title></head><body><main class="plan-doc">'
        '<h2 id="s1">One section</h2><p>The section body.</p>'
        "</main></body></html>"
    )
    path.write_text(
        _plan_html.write_state(
            authored,
            {
                "project": PROJECT,
                "type": "plan",
                "slug": PLAN,
                "title": "Delivery plan",
                "status": "active",
                "modified": "2026-11-13",
                "version": 0,
                "section_declarations": {"s1": "implementable"},
                "sections": [
                    {
                        "id": "s1",
                        "effort_hours": 1.0,
                        "capability": {
                            "version": "1.0",
                            "class": "general",
                            "requirements": {
                                "reasoning": "standard",
                                "verification": "strict",
                                "risk": "low",
                            },
                        },
                        "attempts": 0,
                        "status": "implementable",
                        "links": [],
                    }
                ],
            },
        ),
        encoding="utf-8",
    )


class _Yield:
    def __init__(self, tree: Path) -> None:
        self._tree = tree

    def __enter__(self) -> Path:
        return self._tree

    def __exit__(self, *_exc) -> None:
        return None


def _completed(argv, code: int = 0, output: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, code, output, "")


def _green_runner(calls: list[dict]):
    def run(argv, *, cwd, capture_output=True, text=True, **_ignored):
        calls.append({"argv": list(argv), "cwd": str(cwd)})
        return _completed(argv, 0, "3 passed\n")

    return run


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch_root))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _write_plan(repo)
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}), encoding="utf-8"
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text(
        "def test_real():\n    pass\n", encoding="utf-8"
    )
    module = repo / "reckon" / "crew"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "queue_landing.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "docs", "tests", "reckon")
    _git(repo, "commit", "-q", "-m", "chore: seed fixture")
    base = _git(repo, "rev-parse", "HEAD")
    (module / "queue_landing.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "reckon")
    _git(repo, "commit", "-q", "-m", "feat: change the module")
    head = _git(repo, "rev-parse", "HEAD")
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "\n".join(
            (
                "node: implementation",
                "status: complete",
                f"commits: [{head}]",
                "baseline_suite: "
                + json.dumps(
                    {
                        "revision": base,
                        "command": SUITE,
                        "exit_status": 0,
                        "completed": True,
                        "failure_ids": [],
                    }
                ),
                "after_suite: "
                + json.dumps(
                    {
                        "revision": head,
                        "command": SUITE,
                        "exit_status": 0,
                        "completed": True,
                        "failure_ids": [],
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(RUN),
        {
            "run_id": RUN,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repo),
            "worktree": str(repo),
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "phase": "complete",
            "manifest_path": str(manifest),
            "node": {
                "id": "implementation",
                "plan": PLAN,
                "section": "s1",
                "role": "implement",
                "write_paths": ["reckon/crew/queue_landing.py"],
            },
            "dispatched_at": datetime.now(UTC).isoformat(),
            "base_sha": base,
            "suite_command": SUITE,
        },
    )
    base_tree = tmp_path / "base-tree"
    base_tree.mkdir()
    monkeypatch.setattr(
        landing_measure,
        "base_checkout",
        lambda repo, sha, timeout=0.0: _Yield(base_tree),
    )
    monkeypatch.setattr(landing_measure, "_RUN", _green_runner([]))

    landed: list[str] = []

    def record_landing(run_id: str) -> tuple[bool, str]:
        landed.append(run_id)
        return True, ""

    monkeypatch.setattr(queue_landing, "_land", record_landing)
    _store_review(base=base, head=head)
    return {"repo": repo, "base": base, "head": head, "landed": landed}


def _store_review(*, base: str, head: str) -> dict:
    text = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    parsed = review_module.parse_review(text)
    parsed.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN,
            "review_run_id": "review-of-delivery",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(parsed)
    return parsed


# ── the four cases ─────────────────────────────────────────────────────────


def test_a_held_run_is_neither_accepted_nor_landed(delivery) -> None:
    """The hold defers both automatic doors, and releasing it frees them."""
    resume.hold_delivery(RUN, "the lead rejected this result; the coordinator decides")

    accepted = recovery_review_acceptance.accept_clean_review(runs.read_pointer(RUN))
    assert accepted is not None, "a cleanly reviewed run is acceptance-eligible"
    assert accepted["accepted"] is False, accepted
    assert accepted["held"] is True, accepted
    assert "the lead rejected this result" in accepted["reason"], accepted

    pass_result = queue_landing.land_once()
    assert pass_result["landed"] == [], pass_result
    assert delivery["landed"] == [], "a held run was promoted by the lander"
    held_rows = [row for row in pass_result["skipped"] if row["run_id"] == RUN]
    assert held_rows and held_rows[0]["status"] == "held", pass_result

    resume.release_hold(RUN, "the coordinator judged the result and it stands")

    after = queue_landing.land_once()
    assert after["landed"] == [RUN], after
    assert delivery["landed"] == [RUN], delivery["landed"]


def test_the_hold_and_its_release_are_recorded_with_their_reasons(delivery) -> None:
    resume.hold_delivery(RUN, "waiting for the lead's verdict")
    resume.release_hold(RUN, "the verdict arrived and the run stands")

    recorded = runs.read_pointer(RUN)[resume.HOLD_FIELD]
    assert recorded["reason"] == "waiting for the lead's verdict", recorded
    assert recorded["by"] == resume.DEFAULT_HOLDER, recorded
    assert recorded["release_reason"] == "the verdict arrived and the run stands", (
        recorded
    )
    assert recorded["released_at"], recorded
    assert resume.delivery_hold(runs.read_pointer(RUN)) is None, recorded


def test_a_resume_of_a_promoted_run_exits_non_zero_naming_its_commit(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's pointer is gone, so the resume must say where the run went."""
    head = str(delivery["head"])
    (delivery["repo"] / "docs" / "state" / PROJECT / "runs").mkdir(
        parents=True, exist_ok=True
    )
    ledger.write_run_file(
        PROJECT,
        {
            "run_id": RUN,
            "project": PROJECT,
            "promoted_revision": head,
            "promoted_by": "review-acceptance",
        },
        root=delivery["repo"],
    )
    runs.pointer_path(RUN).unlink()
    monkeypatch.setattr(
        "reckon.crew.resumption._resume",
        lambda *a, **k: pytest.fail("a promoted run was resumed"),
    )

    from reckon.crew_dispatch_commands import crew

    result = CliRunner().invoke(crew, ["resume", "--run", RUN, "--advice", "repair"])

    assert result.exit_code != 0, result.output
    assert head[:12] in result.output, result.output
    assert "already been promoted" in result.output, result.output


def test_a_promotion_in_flight_refuses_the_resume_before_it_holds(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promotion owns the run while it holds its lease, pointer or not.

    A promotion claims its lease before it does any work and removes the run's
    pointer only at the very end, so most of the promotion window runs with the
    pointer still present. A resume reading the pointer first would take a hold
    and launch a worker beside the promotion the section exists to stay out of.
    """
    from reckon.crew import promotion as promotion_module
    from reckon.crew.host_lease import HostLease, local_host

    lease = HostLease(
        runs.crew_home(),
        promotion_module._promotion_lease_name(RUN),
        local_host(),
        os.getpid(),
        "",
    )
    assert lease.claim(), "the fixture could not take the promotion lease"
    try:
        assert runs.pointer_path(RUN).is_file(), "the pointer must still exist"
        monkeypatch.setattr(
            "reckon.crew.resumption._resume",
            lambda *a, **k: pytest.fail("a promotion in flight was resumed"),
        )

        from reckon.crew_dispatch_commands import crew

        result = CliRunner().invoke(
            crew, ["resume", "--run", RUN, "--advice", "the result needs repair"]
        )

        assert result.exit_code != 0, result.output
        assert "is being promoted right now" in result.output, result.output
        assert resume.delivery_hold(runs.read_pointer(RUN)) is None, (
            "the refused resume took a hold anyway"
        )
    finally:
        lease.release()


def test_a_resume_holds_before_it_relaunches(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running between the hold and the launch, the lander leaves it unlanded."""
    seen: list[dict] = []

    class _Plan:
        def as_dict(self) -> dict:
            return {"run_id": RUN, "node": {"id": "implementation"}}

    def fake_resume(run_id, record, **kwargs):
        seen.append(dict(runs.read_pointer(run_id)))
        return {"plan": _Plan(), "pid": 4242, "log_path": "", "turn": 2}

    monkeypatch.setattr("reckon.crew.resumption._resume", fake_resume)

    from reckon.crew_dispatch_commands import crew

    result = CliRunner().invoke(
        crew, ["resume", "--run", RUN, "--advice", "the result needs repair"]
    )

    assert result.exit_code == 0, result.output
    assert seen, "the resume never reached the launcher"
    held = resume.delivery_hold(seen[0])
    assert held is not None, seen[0]
    assert "resuming this delivered run" in held["reason"], held

    between = queue_landing.land_once()
    assert between["landed"] == [], between
    assert delivery["landed"] == [], delivery["landed"]


def test_a_resume_hold_stops_covering_the_next_delivery(delivery) -> None:
    """A hold a resume took defers the head it named, not the repair after it.

    The resumed attempt delivers a new head, so the deferral has nothing left
    to protect: the lander must judge the repair rather than skip the run until
    somebody remembers to release the hold.
    """
    before = str(delivery["head"])
    after = "0" * 40
    resume.hold_delivery(
        RUN, "a coordinator is resuming this delivered run", head=before
    )
    pointer = runs.read_pointer(RUN)

    assert resume.hold_reason(pointer, before), "the named head is not deferred"
    assert resume.hold_reason(pointer, after) == "", "a later head is deferred"
    assert resume.hold_reason(pointer), "the run is not reported as held at all"


def test_a_hold_covers_a_manifest_that_abbreviates_its_commit(delivery) -> None:
    """A manifest may spell a commit short; the hold still covers that delivery.

    A worker writes its commits however its manifest reads to it, and this
    run's own manifest names them abbreviated. A hold taken against the full
    head must defer that delivery anyway: comparing the two spellings as
    strings would let the lander promote a held run whose manifest happens to
    be short, which is the deferral silently not happening.
    """
    head = str(delivery["head"])
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace(f"commits: [{head}]", f"commits: [{head[:10]}]"), encoding="utf-8"
    )
    assert head[:10] in manifest.read_text(encoding="utf-8"), (
        "the fixture did not shorten"
    )
    resume.hold_delivery(RUN, "the coordinator is steering this delivery", head=head)

    pass_result = queue_landing.land_once()

    assert pass_result["landed"] == [], pass_result
    assert delivery["landed"] == [], "a held run whose manifest is abbreviated landed"
    held_rows = [row for row in pass_result["skipped"] if row["run_id"] == RUN]
    assert held_rows and held_rows[0]["status"] == "held", pass_result


# ── negative control ───────────────────────────────────────────────────────


def test_the_lander_lands_a_held_run_without_the_hold_guard(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignoring the hold in the lander makes the first case land."""
    monkeypatch.setattr(
        queue_landing, "delivery_hold_reason", lambda pointer, revision=None: ""
    )
    resume.hold_delivery(RUN, "the lead rejected this result; the coordinator decides")

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert delivery["landed"] == [RUN], delivery["landed"]
