"""A refused landing names the evidence that is missing, not only the rule.

The lander's guard reports which field a passing gate's check lacks. That is
the rule, not the repair: an operator who reads "missing exit_status" checks
the promotion command, while the usual defect is a gate log whose last line is
a quoted status rather than a terminal ``EXIT=<n>`` record. These cases show
the refusal carries the remedy, and that the reader is not widened to accept
the quoted form.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import (
    obligations,
    promotion_checks,
    promotion_evidence,
    queue_landing,
    runs,
)
from reckon.crew import review as review_module

PROJECT = "landing-fixture"
SESSION = "owner"
RUN = "r-delivered-implementation"


def _gate_guard_from_promotion(gate_check: dict[str, object]) -> str:
    """The guard the promotion itself raises for this gate check.

    The test must not assert against a string it also handed to the lander: a
    literal proves only that the test agreed with itself. The guard is taken
    from the production precondition the promotion runs, in the shape ``_land``
    receives it — the failing step's name and its error text.
    """
    with pytest.raises(ledger.LedgerError) as refused:
        promotion_checks._require_gate_check_precondition(
            gate_check, gate="passed", require_gate_check=True
        )
    return f"promote: {refused.value}"


#: A gate log that states its status in prose rather than as a capture record.
QUOTED_EXIT_STATUS = "# exit status 0 (the gate printed EXIT=0)\n"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path | str]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    plan = repo / "docs" / "plans" / "delivery-plan.html"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        '<!doctype html><meta name="docs-project" content="landing-fixture">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="delivery-plan">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-section-declarations" '
        'content="{&quot;s2&quot;: &quot;implementable&quot;}">'
        '<h2 id="s2">Downstream</h2>\n',
        encoding="utf-8",
    )
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}),
        encoding="utf-8",
    )
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    (repo / "marker.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "marker.txt", "docs", "skills")
    _git(repo, "commit", "-q", "-m", "chore: seed fixture")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "marker.txt").write_text("delivered\n", encoding="utf-8")
    _git(repo, "add", "marker.txt")
    _git(repo, "commit", "-q", "-m", "feat: deliver marker")
    head = _git(repo, "rev-parse", "HEAD")
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    gate_log = manifest.parent / "gate-repair.log"
    gate_log.write_text(QUOTED_EXIT_STATUS, encoding="utf-8")
    manifest.write_text(
        "\n".join(
            (
                "node: implementation",
                "status: complete",
                f"commits: [{head}]",
                "tests: pytest -q tests/test_marker.py",
                "test_logs: [gate-repair.log]",
                "",
            )
        ),
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
            "manifest_path": str(manifest),
            "node": {"id": "implementation", "write_paths": ["marker.txt"]},
        },
    )
    return {
        "repo": repo,
        "base": base,
        "head": head,
        "gate_log": gate_log,
    }


def _review(*, base: str, head: str, score: int = 20) -> None:
    parsed = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: {score}"
            for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
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


def test_a_gate_log_without_an_exit_record_names_the_log_and_the_record(
    delivery: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _review(base=str(delivery["base"]), head=str(delivery["head"]))
    gate_check = {
        "command": "pytest -q tests/test_marker.py",
        "log_path": str(delivery["gate_log"]),
        "exit_status": promotion_evidence._recorded_exit_status(QUOTED_EXIT_STATUS),
    }
    guard = _gate_guard_from_promotion(gate_check)
    monkeypatch.setattr(queue_landing, "_land", lambda run_id: (False, guard))

    result = queue_landing.land_once()

    assert result["landed"] == []
    refused = runs.read_pointer(RUN)["admission_landing"]
    assert refused["status"] == "refused"
    assert refused["guard"] == guard
    gate_log = str(delivery["gate_log"])
    assert gate_log in refused["remedy"]
    assert "EXIT=<n>" in refused["remedy"]

    duties = obligations.obligations(PROJECT, SESSION)["obligations"]
    landing_rows = [item for item in duties if item["kind"] == "landing-refused"]
    assert landing_rows, duties
    text = str(landing_rows[0]["reason"])
    assert gate_log in text
    assert "EXIT=<n>" in text


def test_a_stale_manifest_names_the_rewrite_the_landing_owes(
    delivery: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest older than the attempt is not repaired by editing its log.

    Promotion reads gate evidence through ``_fresh_manifest``, so for a
    manifest no newer than this attempt's ``manifest_baseline_mtime_ns`` it
    reads nothing: whichever field the guard names, no log record can supply
    it. The remedy must send the coordinator to the manifest write, not to the
    gate log.

    The run is a resumed attempt carrying the previous attempt's manifest: the
    classifier reads that acceptance across the attempt chain, so the lander
    reaches the refusal, while the promotion's own rule ignores it.
    """
    _review(base=str(delivery["base"]), head=str(delivery["head"]))
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
    delivered_ns = manifest.stat().st_mtime_ns
    first_dispatch = datetime.fromtimestamp(delivered_ns / 1_000_000_000, tz=UTC)
    runs._mutate_pointer(
        RUN,
        lambda pointer: {
            **pointer,
            "attempt": 2,
            "created_at": (first_dispatch - timedelta(hours=1)).isoformat(),
            "manifest_baseline_mtime_ns": delivered_ns,
        },
    )
    guard = _gate_guard_from_promotion(
        {
            "command": "pytest -q tests/test_marker.py",
            "log_path": str(delivery["gate_log"]),
            "exit_status": promotion_evidence._recorded_exit_status(QUOTED_EXIT_STATUS),
        }
    )
    monkeypatch.setattr(queue_landing, "_land", lambda run_id: (False, guard))

    queue_landing.land_once()

    remedy = str(runs.read_pointer(RUN)["admission_landing"]["remedy"])
    assert str(manifest) in remedy
    assert "rewrite the manifest for this attempt" in remedy
    assert "EXIT=<n>" not in remedy


def test_a_quoted_exit_status_is_still_refused_not_read_as_a_pass(
    delivery: dict[str, Path | str],
) -> None:
    gate_log = Path(str(delivery["gate_log"]))
    text = gate_log.read_text(encoding="utf-8")

    assert promotion_evidence._recorded_exit_status(text) is None

    check = {
        "command": "pytest -q tests/test_marker.py",
        "log_path": str(gate_log),
        "exit_status": promotion_evidence._recorded_exit_status(text),
    }
    assert "exit_status" in ledger.gate_check_missing_fields(check)
    with pytest.raises(ledger.LedgerError, match="missing exit_status"):
        promotion_checks._require_gate_check_precondition(
            check, gate="passed", require_gate_check=True
        )
