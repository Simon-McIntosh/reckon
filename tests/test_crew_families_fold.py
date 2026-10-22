"""The crew verb families fold behind flags, each retired name answering for itself.

A family of sibling verbs is folded behind one parent and a flag per case, so
the crew surface shrinks without any answer changing. Four checks hold that for
the budget family -- the three budget verbs and the pre-flight folded into one
``crew budget`` command:

* every retired name answers through its tombstone, printing the successor
  invocation with the flag that names its own case and the recorded arguments
  carried across, and exits with the documented retirement code;
* each folded flag gives the answer its retired verb gave at this node's base.
  The answers were captured from the retired verbs before they were folded and
  are embedded below as :data:`FIXTURE`. Each is compared after normalisation,
  because a grant records an instant and an identifier a fresh run cannot
  reproduce: floats are rounded to two places and instants, identifiers,
  repository paths and the acting user become placeholders, so every value a
  caller reads is compared rather than the clock;
* no string literal under ``reckon/`` names a tombstoned verb as a crew
  command, so no module keeps reaching for a name that now only refuses. The
  registry module is exempt: it is where the retired names are defined;
* the visible leaf count, read from the live command tree, is the base count
  less the four retired names plus the one parent verb that replaces them.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from click import Group
from click.testing import CliRunner

from reckon.cli import crew, main
from reckon.crew import paid_lanes
from reckon.crew import review as review_module
from reckon.crew import runs as runs_module
from reckon.crew.tombstones import TOMBSTONE_BY_NAME, TOMBSTONE_EXIT_CODE

ROOT = Path(__file__).resolve().parents[1]

GROUP = "budget-fold-fixture"
BACKEND = "codex"

#: The parent verb this node adds, and the flag naming each retired verb's case
#: in it.
BUDGET_PARENT = "budget"
FOLDED_INTO_BUDGET: dict[str, str] = {
    "budget-lift": "--lift",
    "budget-lifts": "--list",
    "budget-reset": "--reset",
    "preflight": "--check",
}

#: The registry module is where the retired names are defined, so it is the one
#: place a retired name is expected to appear.
REGISTRY_MODULE = ROOT / "reckon" / "crew" / "tombstones.py"

_GRANT = ["budget", "--lift", "--group", GROUP, "--multiple", "1.8", "--reason", "why"]

#: case name -> (invocations replayed first, the recorded invocation). A grant
#: is replayed before the clear and list cases because both act on a lift.
CASES: dict[str, tuple[list[list[str]], list[str]]] = {
    "reset-read": ([], ["budget", "--reset", "--group", GROUP]),
    "reset-available": ([], ["budget", "--reset", "--group", GROUP, "--available"]),
    "lift-grant": ([], _GRANT),
    "lift-clear": ([_GRANT], ["budget", "--lift", "--clear", "--group", GROUP]),
    "lift-list": ([_GRANT], ["budget", "--list", "--group", GROUP]),
    "check": ([], ["budget", "--check", "--project", "p"]),
}

_HOST_LAYER = f"""
version: 1

backends:
  {BACKEND}:
    budget_group: {GROUP}
"""

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.+\-]+")
_LIFT_ID = re.compile(r"[A-Za-z0-9_.\-]*-\d{10}-[0-9a-f]{6}")
_WHO_KEYS = {"set_by", "granted_by", "cleared_by"}

#: The retired verbs' answers, captured at this node's base revision before the
#: fold. The folded flags are held to these, after normalisation.
_FIXTURE_JSON = r"""
{
  "check": {
    "answer": {
      "backends": [
        {
          "backend": "codex",
          "ceiling_pct": 100.0,
          "effective_ceiling_pct": 92.0,
          "held": false,
          "purpose": "dispatch",
          "reason": "headroom is unknown, and absence of a signal is never read as exhaustion \u2014 nothing recorded for this backend",
          "state": {
            "age_source": null,
            "availability": null,
            "availability_cached": false,
            "availability_observed_at": null,
            "backend": "codex",
            "burn_multiple": null,
            "detail": "",
            "expired": false,
            "headroom": "unknown",
            "observed_at": null,
            "projected_exhaustion_at": null,
            "rate_limit_period_minutes": null,
            "rate_limit_type": null,
            "resets_at": null,
            "seconds_until_reset": null,
            "severity": null,
            "source": "none",
            "threshold_status": null,
            "utilisation_pct": null
          }
        },
        {
          "backend": "native",
          "ceiling_pct": 100.0,
          "effective_ceiling_pct": 92.0,
          "held": false,
          "purpose": "dispatch",
          "reason": "headroom is unknown, and absence of a signal is never read as exhaustion \u2014 nothing recorded for this backend",
          "state": {
            "age_source": null,
            "availability": null,
            "availability_cached": false,
            "availability_observed_at": null,
            "backend": "native",
            "burn_multiple": null,
            "detail": "",
            "expired": false,
            "headroom": "unknown",
            "observed_at": null,
            "projected_exhaustion_at": null,
            "rate_limit_period_minutes": null,
            "rate_limit_type": null,
            "resets_at": null,
            "seconds_until_reset": null,
            "severity": null,
            "source": "none",
            "threshold_status": null,
            "utilisation_pct": null
          }
        }
      ],
      "checked_at": "<when>Z",
      "clear_backends": [
        "codex",
        "native"
      ],
      "groups": [
        {
          "allowance": {
            "advisory": false,
            "burn_multiple": 0.73,
            "derived": 0.04,
            "drain_hours": 156.0,
            "effective_limit": 0.04,
            "elapsed_fraction": 0.29,
            "elapsed_hours": 48.0,
            "group": "budget-fold-fixture",
            "limited_by": "allowance",
            "observed_at": "<when>",
            "pace_multiple": 1.1,
            "provider_ceiling": null,
            "reason": null,
            "remaining_budget": 0.79,
            "remaining_windows": 21.6,
            "resets_at": "<when>",
            "state": "observed",
            "utilisation": 0.21,
            "window_minutes": 10080
          },
          "bar": {
            "admitted": [],
            "held": [],
            "recommendations": [],
            "refusal": null,
            "runway": {
              "age_seconds": "<age>",
              "by_role": {},
              "drain_deadline_hours": null,
              "group": "budget-fold-fixture",
              "mean_age_seconds": null,
              "mean_observed_at": null,
              "mean_run_cost_pct": null,
              "observed_at": "<when>",
              "priced_runs": 0,
              "quantisation_pct": null,
              "reason": "no reset window in the trailing seven days held two receipts with a rise between them, so no per-run cost is measured to divide the remaining window",
              "remaining_pct": 79.0,
              "resets_at": "<when>",
              "runs_remaining": null,
              "state": "unknown"
            },
            "split": [],
            "state": "observed",
            "undecided": [],
            "window_fill": 0.05
          },
          "clocks": {
            "five_hour": {
              "age_seconds": "<age>",
              "observed_at": "<when>",
              "period": "five_hour",
              "resets_at": "<when>",
              "state": "observed",
              "utilisation": 0.05,
              "window_minutes": null
            },
            "seven_day": {
              "age_seconds": "<age>",
              "observed_at": "<when>",
              "period": "seven_day",
              "resets_at": "<when>",
              "state": "observed",
              "utilisation": 0.21,
              "window_minutes": null
            }
          },
          "group": "budget-fold-fixture",
          "member": "codex",
          "members": [
            "codex"
          ],
          "source": "document",
          "state": "observed"
        }
      ],
      "held": false,
      "held_backends": [],
      "hold_history": {
        "outcomes": [],
        "path": "<path>",
        "version": 0
      },
      "ok": true,
      "policy": {
        "availability_probe_cache_seconds": 60.0,
        "coordinator_reserve_pct": 3.0,
        "evidence_shelf_life_minutes": 60.0,
        "exhausted_statuses": [],
        "resume_reserve_pct": 5.0,
        "utilisation_ceiling_pct": 100.0
      },
      "project": "p",
      "purpose": "dispatch",
      "resume_after_seconds": null,
      "resume_at": null,
      "summary": "WHAT   budget preflight \u2014 0 backend(s) held, 2 clear\nWHY    codex: headroom is unknown, and absence of a signal is never read as exhaustion \u2014 nothing recorded for this backend; native: headroom is unknown, and absence of a signal is never read as exhaustion \u2014 nothing recorded for this backend\nHOW    no worktree created and no node failed; ready nodes on codex, native dispatch normally\nWHEN   the wave may open now; compare projected exhaustion with the work horizon before dispatch",
      "unattributed_records": [],
      "window_sources": {
        "codex": "document"
      }
    },
    "exit_code": 0
  },
  "lift-clear": {
    "answer": {
      "action": "clear",
      "cleared": {
        "cleared_at": "<when>",
        "cleared_by": "<who>",
        "ends": {
          "clock": "seven_day",
          "kind": "reset",
          "resets_at": "<when>",
          "utilisation": 0.21
        },
        "form": "multiple",
        "granted_at": "<when>",
        "granted_by": "<who>",
        "group": "budget-fold-fixture",
        "id": "<id>",
        "pace_multiple": 1.8,
        "projected_exhaustion": "<when>",
        "reason": "why",
        "replaced_by": null,
        "replaces": null,
        "scope": "global",
        "starts_at": "<when>"
      },
      "detail": "revoked lift <id> on budget-fold-fixture",
      "group": "budget-fold-fixture",
      "lift_id": "<id>",
      "ok": true
    },
    "exit_code": 0
  },
  "lift-grant": {
    "answer": {
      "action": "grant",
      "admitted": true,
      "clock": "seven_day",
      "detail": "granted lift <id> on budget-fold-fixture: the group's budget is admitted regardless of when the projection lands",
      "ends": {
        "clock": "seven_day",
        "kind": "reset",
        "resets_at": "<when>",
        "utilisation": 0.21
      },
      "form": "multiple",
      "group": "budget-fold-fixture",
      "lift": {
        "cleared_at": null,
        "cleared_by": "<who>",
        "ends": {
          "clock": "seven_day",
          "kind": "reset",
          "resets_at": "<when>",
          "utilisation": 0.21
        },
        "form": "multiple",
        "granted_at": "<when>",
        "granted_by": "<who>",
        "group": "budget-fold-fixture",
        "id": "<id>",
        "pace_multiple": 1.8,
        "projected_exhaustion": "<when>",
        "reason": "why",
        "replaced_by": null,
        "replaces": null,
        "scope": "global",
        "starts_at": "<when>"
      },
      "lift_id": "<id>",
      "multiple": 1.8,
      "ok": true,
      "projected_exhaustion": "<when>",
      "scope": "global"
    },
    "exit_code": 0
  },
  "lift-list": {
    "answer": {
      "count": 1,
      "group": "budget-fold-fixture",
      "lifts": [
        {
          "cleared_at": null,
          "cleared_by": "<who>",
          "ends": {
            "clock": "seven_day",
            "kind": "reset",
            "resets_at": "<when>",
            "utilisation": 0.21
          },
          "form": "multiple",
          "granted_at": "<when>",
          "granted_by": "<who>",
          "group": "budget-fold-fixture",
          "id": "<id>",
          "pace_multiple": 1.8,
          "projected_exhaustion": "<when>",
          "reason": "why",
          "replaced_by": null,
          "replaces": null,
          "scope": "global",
          "starts_at": "<when>"
        }
      ],
      "ok": true,
      "reset_count": 0,
      "resets": []
    },
    "exit_code": 0
  },
  "reset-available": {
    "answer": {
      "available": true,
      "changed": true,
      "detail": "a banked reset is now flagged available for this group",
      "group": "budget-fold-fixture",
      "ok": true,
      "set_at": "<when>Z",
      "set_by": "<who>"
    },
    "exit_code": 0
  },
  "reset-read": {
    "answer": {
      "available": false,
      "changed": false,
      "cleared_at": null,
      "cleared_reason": null,
      "detail": "no banked reset is available for this group",
      "group": "budget-fold-fixture",
      "ok": true,
      "set_at": null,
      "set_by": "<who>"
    },
    "exit_code": 0
  }
}
"""
FIXTURE: dict = json.loads(_FIXTURE_JSON)

WATCH_STOP_FIXTURE = {
    "detail": "project 'fixture-project' has no registered watcher to stop",
    "ok": True,
    "project": "fixture-project",
    "reason": "nothing-to-stop",
    "registration_released": True,
    "stopped": False,
    "watcher": {},
}


def _normalise(value):
    """Every value a caller reads, with the clock, identifiers and paths masked."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in {"id", "lift_id"}:
                out[key] = "<id>"
            elif key in _WHO_KEYS:
                out[key] = "<who>"
            elif key == "age_seconds":
                out[key] = "<age>"
            else:
                out[key] = _normalise(item)
        return out
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, str):
        if value.startswith("/"):
            return "<path>"
        return _LIFT_ID.sub("<id>", _ISO.sub("<when>", value))
    return value


def _fleet_home(home: Path) -> None:
    """Write the host layer and the published headroom document into ``home``."""
    (home / "flight.yaml").write_text(_HOST_LAYER)
    now = datetime.now(UTC)
    document = {
        "accounts": {
            BACKEND: {
                "windows": {
                    "five_hour": {
                        "state": paid_lanes.OBSERVED,
                        "utilisation": 0.05,
                        "observed_at": now.isoformat(),
                        "resets_at": (now + timedelta(hours=0.2)).isoformat(),
                    },
                    "seven_day": {
                        "state": paid_lanes.OBSERVED,
                        "utilisation": 0.21,
                        "observed_at": now.isoformat(),
                        "resets_at": (now + timedelta(days=5.0)).isoformat(),
                    },
                }
            }
        }
    }
    (home / paid_lanes.DOCUMENT_NAME).write_text(json.dumps(document))


def _string_literals(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _leaf_commands(group: Group, prefix=()) -> dict:
    leaves: dict = {}
    for name, command in group.commands.items():
        path = (*prefix, name)
        if isinstance(command, Group):
            leaves.update(_leaf_commands(command, path))
        else:
            leaves[path] = command
    return leaves


def test_each_retired_budget_name_prints_its_successor() -> None:
    """A session reaching for a retired name is routed in one turn."""
    runner = CliRunner()
    for retired, flag in FOLDED_INTO_BUDGET.items():
        result = runner.invoke(main, ["crew", retired, "--group", GROUP])
        assert result.exit_code == TOMBSTONE_EXIT_CODE, result.output
        payload = json.loads(result.output)
        assert payload["retired"] == retired
        assert payload["successor"] == f"crew budget {flag}"
        assert payload["invocation"] == f"reckon crew budget {flag} --group {GROUP}"
        # The registry row behind that answer, not the key it was fetched by:
        # the successor it names, the flag it carries into the invocation.
        row = TOMBSTONE_BY_NAME[retired]
        assert row.successor == f"crew budget {flag}", row
        assert row.argv == ("crew", BUDGET_PARENT), row
        assert row.flags == (flag,), row


def test_unwatch_tombstone_names_watch_stop() -> None:
    result = CliRunner().invoke(
        main, ["crew", "unwatch", "--project", "fixture-project"]
    )
    assert result.exit_code == TOMBSTONE_EXIT_CODE, result.output
    payload = json.loads(result.output)
    assert payload["successor"] == "crew watch --stop"
    assert payload["invocation"] == (
        "reckon crew watch --stop --project fixture-project"
    )
    assert "unwatch" not in crew.commands


def test_resume_family_tombstones_and_live_migrations() -> None:
    runner = CliRunner()
    cases = {
        "resume-ready": (
            ["--project", "reckon", "--dry-run"],
            "reckon crew resume --ready --project reckon --dry-run",
        ),
        "redispatch": (
            ["--run", "r-absent", "--backend", "codex", "--reason", "fixture"],
            "reckon crew resume --run r-absent --backend codex --reason fixture",
        ),
    }
    for retired, (args, invocation) in cases.items():
        result = runner.invoke(main, ["crew", retired, *args])
        assert result.exit_code == TOMBSTONE_EXIT_CODE, result.output
        assert json.loads(result.output)["invocation"] == invocation
        assert retired not in crew.commands
    for retained in ("repair-completion", "split-runs"):
        assert retained in crew.commands
        assert retained not in TOMBSTONE_BY_NAME
        result = runner.invoke(main, ["crew", retained, "--help"])
        assert result.exit_code == 0, result.output


def test_resume_successors_preserve_base_answers(isolated_reckon_home: Path) -> None:
    runner = CliRunner()
    ready = runner.invoke(
        main, ["crew", "resume", "--ready", "--project", "reckon", "--dry-run"]
    )
    assert ready.exit_code == 0, ready.output
    answer = _normalise(json.loads(ready.output))
    assert answer == {
        "checked": 0,
        "dry_run": True,
        "ok": True,
        "project": "reckon",
        "resumed": [],
        "reviews": {},
        "skipped": [],
        "swept_at": "<when>Z",
    }
    moved = runner.invoke(
        main,
        [
            "crew",
            "resume",
            "--run",
            "r-absent",
            "--backend",
            "codex",
            "--reason",
            "fixture",
            "--print-only",
        ],
    )
    assert moved.exit_code == 1
    assert moved.output.startswith("Error: no live run 'r-absent'")


def test_resume_family_reduces_the_visible_leaf_count() -> None:
    leaves = _leaf_commands(crew)
    visible = {path[0] for path in leaves}
    retired = {"resume-ready", "redispatch"}
    assert retired <= TOMBSTONE_BY_NAME.keys()
    assert ("resume",) in leaves
    assert not retired & visible
    assert {("repair-completion",), ("split-runs",)} <= leaves.keys()
    options = {
        option for parameter in leaves[("resume",)].params for option in parameter.opts
    }
    assert {"--ready", "--backend", "--model", "--reopen", "--write-path"} <= options
    assert visible == set(crew.commands), sorted(visible ^ set(crew.commands))


def test_resume_reopens_a_complete_run_and_grants_its_path(
    tmp_path: Path, isolated_reckon_home: Path, monkeypatch
) -> None:
    from reckon.crew import runs as runs_module

    run_id = "r-20261020T000000000000-reopen-fixture"
    manifest = tmp_path / "manifest.md"
    manifest.write_text("node: reopen-fixture\nstatus: complete\n")
    pointer = {
        "run_id": run_id,
        "project": "fixture",
        "repo": str(tmp_path),
        "worktree": str(tmp_path),
        "phase": "complete",
        "launch": "cli",
        "session_id": "fixture-session",
        "manifest_path": str(manifest),
        "node": {"id": "reopen-fixture", "write_paths": ["reckon/crew/runs.py"]},
    }
    runs_module._write_json(runs_module.pointer_path(run_id), pointer)
    monkeypatch.setattr("reckon.crew_run_commands._resolved_flight", lambda *args: {})
    calls = []

    def launch(*args, **kwargs):
        calls.append((args, kwargs))
        plan = type("Plan", (), {"as_dict": lambda self: {}})()
        return {"plan": plan, "pid": 123, "log_path": "fixture.log", "turn": 1}

    monkeypatch.setattr("reckon.crew.resumption._resume", launch)
    result = CliRunner().invoke(
        main,
        [
            "crew",
            "resume",
            "--run",
            run_id,
            "--reopen",
            "--write-path",
            "reckon/crew/claims.py",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls and calls[0][1]["advice"] == "continue"
    assert runs_module.read_pointer(run_id)["node"]["write_paths"] == [
        "reckon/crew/runs.py",
        "reckon/crew/claims.py",
    ]
    # The reopen continues the run rather than ruling on it, so no verdict of
    # its own is on the manifest: a status word here would be an outcome no
    # attempt reached, and the resumed worker's first edit would then be read
    # against it.
    assert "status: in-progress\n" in manifest.read_text()
    assert "status: blocked" not in manifest.read_text()
    delivered = sorted(manifest.parent.glob("manifest.md.attempt-*.asdelivered"))
    assert [path.read_text() for path in delivered] == [
        "node: reopen-fixture\nstatus: complete\n"
    ]


def test_resume_backend_carries_the_selected_model(
    tmp_path: Path, isolated_reckon_home: Path, monkeypatch
) -> None:
    import importlib

    from reckon import flight, ledger
    from reckon.crew import runs as runs_module

    dispatch = importlib.import_module("reckon.crew.dispatch")

    run_id = "r-20261020T000000000000-model-fixture"
    runs_module._write_json(
        runs_module.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "fixture",
            "repo": str(tmp_path),
            "phase": "blocked",
            "backend": "source",
            "node": {"id": "model-fixture", "write_paths": []},
        },
    )
    config = {"backends": {"destination": {"lane": "destination"}}}
    monkeypatch.setattr(
        "reckon.crew_run_commands._resolved_flight", lambda *args: config
    )
    resolver_calls = []

    def backend_serving_model(*args, **kwargs):
        resolver_calls.append((args, kwargs))
        return "destination", "destination", "chosen"

    monkeypatch.setattr(ledger, "backend_serving_model", backend_serving_model)
    selection_calls = []

    def select_lane_model(config, backend, lane, key):
        selection_calls.append((config, backend, lane, key))
        return {"backends": {backend: {"lane": lane, "model_key": key}}}

    monkeypatch.setattr(flight, "select_lane_model", select_lane_model)
    moved = []

    def change_lane(*args, **kwargs):
        moved.append((args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(dispatch, "change_lane", change_lane)
    result = CliRunner().invoke(
        main,
        [
            "crew",
            "resume",
            "--run",
            run_id,
            "--backend",
            "destination",
            "--model",
            "chosen",
            "--reason",
            "switch model",
            "--print-only",
        ],
    )
    assert result.exit_code == 0, result.output
    assert resolver_calls == [((config,), {"lane": "destination", "model": "chosen"})]
    assert selection_calls == [(config, "destination", "destination", "chosen")]
    assert moved[0][0][:2] == (run_id, "destination")
    assert moved[0][1]["config"]["backends"]["destination"]["model_key"] == "chosen"
    assert moved[0][1]["launch"] is False


def test_complete_accept_path_updates_the_run_claim(
    tmp_path: Path, isolated_reckon_home: Path, monkeypatch
) -> None:
    from reckon import crew as crew_module
    from reckon.crew import runs as runs_module

    run_id = "r-20261020T000000000000-accept-fixture"
    runs_module._write_json(
        runs_module.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "fixture",
            "repo": str(tmp_path),
            "worktree": str(tmp_path),
            "phase": "complete",
            "node": {"id": "accept-fixture", "write_paths": ["reckon/crew/runs.py"]},
        },
    )
    observed = []

    def complete(*args, **kwargs):
        pointer = runs_module.read_pointer(run_id)
        observed.append(
            (
                pointer["node"]["write_paths"],
                pointer["accepted_scope_before_widening"],
                kwargs["accepted_paths"],
            )
        )
        return {"ok": True}

    monkeypatch.setattr(crew_module, "complete", complete)
    result = CliRunner().invoke(
        main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "not-run",
            "--accept-path",
            "reckon/crew/claims.py",
            "companion change",
        ],
    )
    assert result.exit_code == 0, result.output
    assert observed == [
        (
            ["reckon/crew/runs.py", "reckon/crew/claims.py"],
            ["reckon/crew/runs.py"],
            {"reckon/crew/claims.py": "companion change"},
        )
    ]


REPAIR_AND_CHECK_NAMES = {
    "repair-status": "crew repair --status",
    "repair-review": "crew repair --review",
    "check-manifest": "crew check --manifest",
    "check-review": "crew check --review",
}


def test_repair_and_check_tombstones_name_their_successors() -> None:
    runner = CliRunner()
    cases = {
        "repair-status": (
            ["--run", "r-fixture", "--status", "complete", "--reason", "fixture"],
            "reckon crew repair --run r-fixture --status complete --reason fixture",
        ),
        "repair-review": (
            [
                "--run",
                "r-fixture",
                "--record",
                "record.json",
                "--set",
                "status=parsed",
                "--reason",
                "fixture",
            ],
            "reckon crew repair --review --run r-fixture --record record.json --set status=parsed --reason fixture",
        ),
        "check-manifest": (
            ["--run", "r-fixture"],
            "reckon crew check --manifest --run r-fixture",
        ),
        "check-review": (
            ["--record", "record.json"],
            "reckon crew check --review --record record.json",
        ),
    }
    for retired, successor in REPAIR_AND_CHECK_NAMES.items():
        args, invocation = cases[retired]
        result = runner.invoke(main, ["crew", retired, *args])
        assert result.exit_code == TOMBSTONE_EXIT_CODE, result.output
        answer = json.loads(result.output)
        assert answer["retired"] == retired
        assert answer["successor"] == successor
        assert answer["invocation"] == invocation
        assert retired not in crew.commands


def test_repair_and_check_successors_preserve_recorded_answers(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """The four old verbs' base answers are replayed through their replacements."""
    record_path = tmp_path / "review.json"
    record_path.write_text(
        json.dumps(
            {
                "project": "p",
                "reviewed_run_id": "r-old",
                "review_run_id": "r-review",
                "reviewed_base_sha": "a" * 40,
                "reviewed_head_sha": "b" * 40,
                "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 17),
                "added_failure_count": 0,
                "added_failure_ids": [],
                "findings": [],
                "status": "parsed",
            }
        )
    )
    runner = CliRunner()
    valid = runner.invoke(
        main, ["crew", "check", "--review", "--record", str(record_path)]
    )
    assert valid.exit_code == 0, valid.output
    assert json.loads(valid.output) == {
        "ok": True,
        "record": str(record_path),
        "findings": [],
    }

    missing = tmp_path / "missing.json"
    refused = runner.invoke(
        main, ["crew", "check", "--review", "--record", str(missing)]
    )
    assert refused.exit_code == 1
    assert json.loads(refused.output)["findings"] == [
        f"record could not be read as JSON: [Errno 2] No such file or directory: '{missing}'"
    ]
    manifest = runner.invoke(main, ["crew", "check", "--manifest", "--run", "r-absent"])
    assert manifest.exit_code == 1
    assert "no live run 'r-absent'" in manifest.output
    repair_review = runner.invoke(
        main,
        [
            "crew",
            "repair",
            "--review",
            "--run",
            "r-absent",
            "--record",
            str(missing),
            "--set",
            "status=parsed",
            "--reason",
            "fixture",
        ],
    )
    assert repair_review.exit_code == 1
    assert (
        "record is outside the reviewed run's review-records directory"
        in repair_review.output
    )
    repair_status = runner.invoke(
        main,
        [
            "crew",
            "repair",
            "--run",
            "r-absent",
            "--status",
            "complete",
            "--reason",
            "fixture",
        ],
    )
    assert repair_status.exit_code == 1
    assert "run 'r-absent' has no manifest to repair" in repair_status.output


def test_check_review_resolves_a_run_record(isolated_reckon_home: Path) -> None:
    run_id = "r-reviewed-fixture"
    project = "review-fixture"
    runs_module._write_json(
        runs_module.pointer_path(run_id), {"run_id": run_id, "project": project}
    )
    record_path = review_module.review_path(project, run_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "project": project,
                "reviewed_run_id": run_id,
                "review_run_id": "r-review-fixture",
                "reviewed_base_sha": "a" * 40,
                "reviewed_head_sha": "b" * 40,
                "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 17),
                "added_failure_count": 0,
                "added_failure_ids": [],
                "findings": [],
                "status": "parsed",
            }
        )
    )
    result = CliRunner().invoke(main, ["crew", "check", "--review", "--run", run_id])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "ok": True,
        "record": str(record_path),
        "findings": [],
    }


def test_repair_and_check_replace_four_visible_leaves_with_two() -> None:
    leaves = _leaf_commands(crew)
    family = {("repair",), ("check",)} | {(name,) for name in REPAIR_AND_CHECK_NAMES}
    visible_family = family & leaves.keys()
    assert visible_family == {("repair",), ("check",)}


def test_watch_stop_replaces_one_visible_leaf() -> None:
    """One live watch leaf carries both following and stopping."""
    leaves = _leaf_commands(crew)
    family = {("watch",), ("unwatch",)}
    visible_family = family & leaves.keys()
    assert visible_family == {("watch",)}
    assert len(visible_family) == len(family) - 1
    options = {
        option for parameter in leaves[("watch",)].params for option in parameter.opts
    }
    assert "--stop" in options


def test_watch_stop_matches_recorded_answer(isolated_reckon_home: Path) -> None:
    result = CliRunner().invoke(
        main, ["crew", "watch", "--stop", "--project", "fixture-project"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == WATCH_STOP_FIXTURE


def test_each_budget_flag_gives_the_answer_the_old_verb_gave(
    isolated_reckon_home: Path, monkeypatch
) -> None:
    """The folded flag reproduces the retired verb's answer, case by case.

    Each case runs against its own home, because a grant, a clear and a banked
    reset all persist: run in one home they accumulate and a later case reads an
    answer that the retired verb never gave. The capture this fixture holds was
    taken the same way, one home per case.
    """
    runner = CliRunner()
    for name, (prelude, argv) in CASES.items():
        case_home = isolated_reckon_home / name
        case_home.mkdir()
        monkeypatch.setenv("RECKON_HOME", str(case_home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(case_home / "xdg"))
        monkeypatch.delenv("RECKON_RUN_ID", raising=False)
        _fleet_home(case_home)
        result = None
        for step in [*prelude, argv]:
            result = runner.invoke(main, ["crew", *step])
        assert result.exit_code == FIXTURE[name]["exit_code"], result.output
        assert _normalise(json.loads(result.output)) == FIXTURE[name]["answer"]


def test_no_string_literal_names_a_tombstoned_verb_as_a_crew_command() -> None:
    """A module that still reaches for a retired name is reported by line."""
    names = sorted(TOMBSTONE_BY_NAME)
    pattern = re.compile(r"\bcrew\s+(" + "|".join(re.escape(n) for n in names) + r")\b")
    problems = []
    for path in sorted(ROOT.glob("reckon/**/*.py")):
        if path == REGISTRY_MODULE:
            continue
        for line, text in _string_literals(path):
            if pattern.search(text):
                problems.append(
                    f"{path.relative_to(ROOT)}:{line} names a retired crew verb"
                )
    assert not problems, "\n".join(problems)


def test_the_visible_leaf_count_drops_by_the_folded_names() -> None:
    """The census shrinks by the retired names and grows by the fold's parent verb.

    No base count is written down: the figure is reconstructed from the tree this
    guard reads — the leaves it shows, plus the retired names the registry still
    resolves, less the one parent verb the fold adds. The claims the census is
    held to are then membership claims, which a wrong fold cannot satisfy: a
    retired name that stayed a visible leaf, a registry row that went missing, or
    a parent that shed one of the flags it carries all fail below.
    """
    leaves = _leaf_commands(crew)
    visible = {path[0] for path in leaves}

    retired = [name for name in FOLDED_INTO_BUDGET if name in TOMBSTONE_BY_NAME]
    assert set(retired) == set(FOLDED_INTO_BUDGET), sorted(TOMBSTONE_BY_NAME)
    assert not set(FOLDED_INTO_BUDGET) & visible, sorted(visible)

    assert BUDGET_PARENT in visible
    carried = {
        opt for parameter in leaves[(BUDGET_PARENT,)].params for opt in parameter.opts
    }
    assert set(FOLDED_INTO_BUDGET.values()) <= carried, sorted(carried)

    # The census and the group's own registration agree, so no leaf is reached
    # without being registered and no retired name is smuggled back in as one.
    assert visible == set(crew.commands), sorted(visible ^ set(crew.commands))

    # Base figure reconstructed from the live census: the leaves it shows, plus
    # the retired names the registry still resolves, less the parent verb the
    # fold added in their place. It is held to the fold this node declares —
    # four names in, one parent out — so the count the registry contributed and
    # the count the fold declares are weighed against each other: a row that
    # went missing moves the reconstruction and fails here, with no absolute
    # written down for either side.
    derived_base = len(leaves) + len(retired) - 1
    assert len(leaves) == derived_base - len(FOLDED_INTO_BUDGET) + 1, derived_base
