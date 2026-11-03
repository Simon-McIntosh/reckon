"""Replay two recorded backend-pick refusals, in process.

Each attempt is rebuilt from what recorded it: the node definition from the
coordinator's own dispatch command line, the flight configuration from the
layers in force at the moment, and the pick-timing row that recorded the
refusal. The picker is then asked the same question, and its answer is
compared with the record candidate by candidate, in the picker's own order.

No routing or competence verdict is stubbed. The candidate scan runs
``reckon.crew.picker.snapshot.candidates`` unchanged, so every reason the
report shows is the picker's own.

The configuration below is a reconstruction of a config *input*, not of a
verdict. It is the resolved flight configuration at the two attempts' moment,
produced by ``flight.resolve`` over the four layers committed at the time:

    shipped   <repo>@9bdaea5ab6  reckon/schema/flight-defaults.yaml
    host      dotfiles@2892816   reckon/flight.yaml
    project   <repo>@9bdaea5ab6  docs/state/reckon/flight.yaml
    catalogue <repo>@9bdaea5ab6  docs/state/reckon/model-catalogue.yaml

It is embedded so the replay reproduces without the host layer's git history,
which lives outside this repository. ``--check-config`` re-resolves it from
those layers when they are reachable and exits 2 if the two disagree.

Usage::

    python replay_oversized_route.py [--repo PATH] [--check-config]

The report goes to stdout. Redirect it and append the exit status as a bare
``EXIT=<code>`` line to turn it into a gate receipt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: The node both recorded attempts dispatched.
NODE_ID = "an-admission-pass-reads-each-project-once"
#: The plan section the node was scoped to, and the plan it lives in.
PLAN = "a-read-costs-what-changed"
SECTION = "s6"
#: The coordinator session that dispatched both attempts.
SESSION = "404513fc-8e68-4a0f-8060-29ac9ff3d5fd"
#: The context estimate both pick-timing rows carry.
ESTIMATED_CONTEXT = 234021

#: The three layers inside this repository, and the revision each was read at.
CONFIG_LAYERS = {
    "shipped": ("9bdaea5ab6", "reckon/schema/flight-defaults.yaml"),
    "project": ("9bdaea5ab6", "docs/state/reckon/flight.yaml"),
    "catalogue": ("9bdaea5ab6", "docs/state/reckon/model-catalogue.yaml"),
}
#: The host layer, which lives in another checkout.
HOST_LAYER = ("2892816", "reckon/flight.yaml")

#: The node definition, transcribed from the dispatch command line each attempt
#: ran. The three attempts before the last passed no lane; the last named
#: ``--backend codex``, which is what narrowed its offering to that lane alone.
NODE_FIELDS: dict[str, Any] = {
    "goal": (
        "An admission pass, landing pass and dispatch read each project and each "
        "stored snapshot once"
    ),
    "role": "implement",
    "spec_level": "guided",
    "done_when": (
        "The six cases and four negative controls in the plan's s6 done-when hold "
        "in tests/test_an_admission_pass_reads_each_project_once.py, run on the "
        "fleet node with -p no:cacheprovider; the test files importing "
        "reckon.crew.queue_admission, reckon.crew.queue_landing, "
        "reckon.crew.dispatch and reckon.crew.plan_review add no failure against "
        "the base revision; ruff check and ruff format --check add no findings on "
        "the staged paths"
    ),
    "write_paths": [
        "reckon/crew/queue_admission.py",
        "reckon/crew/queue_landing.py",
        "reckon/crew/dispatch.py",
        "reckon/crew/plan_review.py",
        "tests/test_an_admission_pass_reads_each_project_once.py",
    ],
    "time_budget": "60m",
}

#: The two attempts replayed, and the exclusion rows each recorded. A row is one
#: reason on one candidate, in the order the picker emitted them, read from the
#: ``pick-timings.jsonl`` row that carries the attempt's ``started_at``.
ATTEMPTS: list[dict[str, Any]] = [
    {
        "label": "attempt-1",
        "started_at": "2026-11-03T02:29:07.892892+00:00",
        "named_lane": None,
        "recorded": [
            ("claude", "reserved-lane"),
            ("claude", "reserved-lane"),
            ("claude", "reserved-lane"),
            ("claude", "reserved-lane"),
            ("clive", "context-fit"),
            ("clive", "context-fit"),
            ("clive", "competence"),
            ("codex", "reserved-lane"),
            ("codex", "reserved-lane"),
            ("codex", "reserved-lane"),
            ("native", "in-harness-backend"),
        ],
    },
    {
        "label": "attempt-4",
        "started_at": "2026-11-03T02:34:43.640120+00:00",
        "named_lane": "codex",
        "recorded": [
            ("codex", "reserved-lane"),
            ("codex", "reserved-lane"),
            ("codex", "reserved-lane"),
        ],
    },
]

CONFIG_JSON = r"""{
 "backends": {
  "claude": {
   "alias": "sonnet 5.5",
   "budget_check": true,
   "budget_group": "claude-sub",
   "catalog_id": "anthropic/claude-sonnet-5.5",
   "command": "claude",
   "effort": "medium",
   "lane": "claude",
   "launch": "cli",
   "model": "claude-sonnet-5-5",
   "model_key": "sonnet",
   "placement": {
    "options": [
     "--ntasks=1"
    ],
    "reason_query": [
     "squeue",
     "-h",
     "-j",
     "{job}",
     "-o",
     "%R"
    ],
    "requirements": [
     {
      "name": "code-checkouts",
      "path": "/home/ITER/mcintos/Code"
     },
     {
      "name": "crew-state",
      "path": "/home/ITER/mcintos/.config/reckon"
     },
     {
      "name": "claude-home",
      "path": "/home/ITER/mcintos/.claude"
     },
     {
      "name": "codex-home",
      "path": "/home/ITER/mcintos/.codex"
     }
    ],
    "scheduler": "srun",
    "state_query": [
     "squeue",
     "-h",
     "-j",
     "{job}",
     "-o",
     "%T"
    ]
   },
   "probe_new_models": true,
   "sandbox": "worktree-full",
   "session_reuse": false,
   "suits": "The lane default, an everyday coding model at about half of Opus's token price. Suits guided implementation and full reviews.",
   "time_budget": "25m"
  },
  "clive": {
   "alias": "dsv4.1-flash",
   "command": "clive",
   "effective_input_window": 230144,
   "effort": "xhigh",
   "endpoints_document": "/home/ITER/mcintos/public/imas-ambix/endpoints.json",
   "gate_document": "/work/projects/imas_gpu/agents/control/router-gate.json",
   "input_modalities": [
    "text",
    "image"
   ],
   "lane_document": "~/public/imas-ambix/lane.json",
   "launch": "cli",
   "max_concurrent_runs": 64,
   "model": "deepseek-v4.1-flash",
   "placement": {
    "options": [
     "--ntasks=1"
    ],
    "reason_query": [
     "squeue",
     "-h",
     "-j",
     "{job}",
     "-o",
     "%R"
    ],
    "requirements": [
     {
      "name": "code-checkouts",
      "path": "/home/ITER/mcintos/Code"
     },
     {
      "name": "crew-state",
      "path": "/home/ITER/mcintos/.config/reckon"
     },
     {
      "name": "claude-home",
      "path": "/home/ITER/mcintos/.claude"
     },
     {
      "name": "codex-home",
      "path": "/home/ITER/mcintos/.codex"
     },
     {
      "name": "clive-publication",
      "path": "/home/ITER/mcintos/public/imas-ambix"
     },
     {
      "endpoint": "98dci4-gpu-0003:18802",
      "name": "clive-router"
     }
    ],
    "scheduler": "srun",
    "state_query": [
     "squeue",
     "-h",
     "-j",
     "{job}",
     "-o",
     "%T"
    ]
   },
   "sandbox": "worktree-full",
   "session_reuse": false,
   "time_budget": "90m",
   "usable_input_window": 230144
  },
  "codex": {
   "alias": "sol6",
   "budget_check": true,
   "budget_group": "codex-sub",
   "catalog_id": "openai/gpt-6-sol",
   "command": "codex",
   "effort": "high",
   "input_modalities": [
    "text",
    "image"
   ],
   "lane": "codex",
   "launch": "cli",
   "model": "gpt-6-sol",
   "model_key": "sol",
   "placement": {
    "options": [
     "--ntasks=1"
    ],
    "reason_query": [
     "squeue",
     "-h",
     "-j",
     "{job}",
     "-o",
     "%R"
    ],
    "requirements": [
     {
      "name": "code-checkouts",
      "path": "/home/ITER/mcintos/Code"
     },
     {
      "name": "crew-state",
      "path": "/home/ITER/mcintos/.config/reckon"
     },
     {
      "name": "claude-home",
      "path": "/home/ITER/mcintos/.claude"
     },
     {
      "name": "codex-home",
      "path": "/home/ITER/mcintos/.codex"
     }
    ],
    "scheduler": "srun",
    "state_query": [
     "squeue",
     "-h",
     "-j",
     "{job}",
     "-o",
     "%T"
    ]
   },
   "sandbox": "worktree-full",
   "served_models": "~/.codex/models_cache.json",
   "session_reuse": false,
   "suits": "The lane default, a previous-generation general model. Suits guided implementation, tests and full reviews at standard or deep reasoning and moderate risk. About twenty times luna's token price and a fifth of astra's.",
   "time_budget": "25m"
  },
  "native": {
   "alias": "harness",
   "launch": "in-harness",
   "sandbox": "worktree-full",
   "session_reuse": false,
   "time_budget": "25m"
  }
 },
 "budget": {
  "advisory_pace_groups": [
   "codex-sub"
  ],
  "coordinator_reserve_pct": 3,
  "drain_lead_hours": 12.0,
  "evidence_shelf_life_minutes": 5,
  "exhausted_statuses": [],
  "lift": {
   "max_hours": 168.0,
   "max_multiple": 3.0
  },
  "pace_multiple": 1.1,
  "resume_reserve_pct": 5,
  "utilisation_ceiling_pct": 100
 },
 "capability_raise": {
  "attempts_threshold": 3,
  "raised_class": "orchestrator",
  "raised_reasoning": "deep",
  "raised_verification": "strict"
 },
 "default_backend": "clive",
 "fences": {
  "budget_grace_multiple": 1.5,
  "enforce_budget_watchdog": false,
  "manifest_required": true,
  "needs_help_after_failures": 2,
  "time_budget": "60m",
  "unreconciled_run_grace": "15m"
 },
 "gates": {
  "dimension_floors": {
   "durability": 10,
   "evidence": 10,
   "fit": 10,
   "goal_fidelity": 10,
   "reuse": 10,
   "scope_discipline": 10
  },
  "enforce": "strict",
  "on_fail": "hold",
  "require_evidence": true,
  "suite_command": "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"
 },
 "harness_home_adjacent_files": {
  "claude": [
   {
    "keys": [
     "mcpServers"
    ],
    "path": ".claude.json"
   }
  ]
 },
 "harness_home_files": {
  "claude": [
   {
    "keys": [
     "hooks",
     "attribution",
     "includeCoAuthoredBy"
    ],
    "path": "settings.json"
   },
   {
    "path": "CLAUDE.md"
   }
  ],
  "codex": [
   {
    "path": "AGENTS.md"
   },
   {
    "path": "config.toml"
   },
   {
    "path": "rules"
   }
  ]
 },
 "lanes": {
  "claude": {
   "budget_check": true,
   "command": "claude",
   "default_model": "sonnet",
   "launch": "cli",
   "models": {
    "fable": {
     "alias": "fable 5.1",
     "budget_group": "claude-sub",
     "catalog_id": "anthropic/claude-fable-5.1",
     "effort": "high",
     "model": "claude-fable-5-1",
     "suits": "The most capable and most expensive Claude model, about two and a half times Opus's token price. Reserve it for the very hardest nodes.",
     "time_budget": "55m"
    },
    "haiku": {
     "alias": "haiku 5.5",
     "budget_group": "claude-sub",
     "catalog_id": "anthropic/claude-haiku-5.5",
     "effort": "high",
     "model": "claude-haiku-5-5",
     "suits": "The lane's fastest and cheapest model, about a twentieth of Sonnet's token price. Suits light, well-defined work."
    },
    "opus": {
     "alias": "opus 5.5",
     "budget_group": "claude-sub",
     "catalog_id": "anthropic/claude-opus-5.5",
     "effort": "high",
     "model": "claude-opus-5-5",
     "suits": "A strong general model for deep or open-ended work, about twice Sonnet's token price.",
     "time_budget": "40m"
    },
    "sonnet": {
     "alias": "sonnet 5.5",
     "budget_group": "claude-sub",
     "catalog_id": "anthropic/claude-sonnet-5.5",
     "effort": "medium",
     "model": "claude-sonnet-5-5",
     "suits": "The lane default, an everyday coding model at about half of Opus's token price. Suits guided implementation and full reviews."
    }
   },
   "placement": {
    "options": [
     "--ntasks=1"
    ],
    "reason_query": [
     "squeue",
     "-h",
     "-j",
     "{job}",
     "-o",
     "%R"
    ],
    "requirements": [
     {
      "name": "code-checkouts",
      "path": "/home/ITER/mcintos/Code"
     },
     {
      "name": "crew-state",
      "path": "/home/ITER/mcintos/.config/reckon"
     },
     {
      "name": "claude-home",
      "path": "/home/ITER/mcintos/.claude"
     },
     {
      "name": "codex-home",
      "path": "/home/ITER/mcintos/.codex"
     }
    ],
    "scheduler": "srun",
    "state_query": [
     "squeue",
     "-h",
     "-j",
     "{job}",
     "-o",
     "%T"
    ]
   },
   "probe_new_models": true,
   "sandbox": "worktree-full",
   "session_reuse": false,
   "time_budget": "25m"
  },
  "codex": {
   "budget_check": true,
   "command": "codex",
   "default_model": "sol",
   "launch": "cli",
   "models": {
    "astra": {
     "alias": "astra6",
     "budget_group": "codex-sub",
     "catalog_id": "openai/gpt-6-astra",
     "effort": "xhigh",
     "model": "gpt-6-astra",
     "suits": "The lane's frontier model, strongest and slowest, about five times sol's token price. Suits deep or open-ended design, elevated or critical risk, and nodes that already failed on another model.",
     "time_budget": "55m"
    },
    "luna": {
     "alias": "luna6",
     "budget_group": "codex-sub",
     "catalog_id": "openai/gpt-6-luna",
     "effort": "xhigh",
     "model": "gpt-6-luna",
     "suits": "The lane's fast, affordable model, about a twentieth of sol's token price. Suits well-defined, less difficult work: reviews, exact-spec implementation, tests and documentation."
    },
    "sol": {
     "alias": "sol6",
     "budget_group": "codex-sub",
     "catalog_id": "openai/gpt-6-sol",
     "effort": "high",
     "model": "gpt-6-sol",
     "suits": "The lane default, a previous-generation general model. Suits guided implementation, tests and full reviews at standard or deep reasoning and moderate risk. About twenty times luna's token price and a fifth of astra's."
    }
   },
   "placement": {
    "options": [
     "--ntasks=1"
    ],
    "reason_query": [
     "squeue",
     "-h",
     "-j",
     "{job}",
     "-o",
     "%R"
    ],
    "requirements": [
     {
      "name": "code-checkouts",
      "path": "/home/ITER/mcintos/Code"
     },
     {
      "name": "crew-state",
      "path": "/home/ITER/mcintos/.config/reckon"
     },
     {
      "name": "claude-home",
      "path": "/home/ITER/mcintos/.claude"
     },
     {
      "name": "codex-home",
      "path": "/home/ITER/mcintos/.codex"
     }
    ],
    "scheduler": "srun",
    "state_query": [
     "squeue",
     "-h",
     "-j",
     "{job}",
     "-o",
     "%T"
    ]
   },
   "sandbox": "worktree-full",
   "served_models": "~/.codex/models_cache.json",
   "session_reuse": false,
   "time_budget": "25m"
  }
 },
 "local_backend": "clive",
 "model_catalogue": {
  "list_url": "https://openrouter.ai/api/v1/models",
  "refresh_age": 86400,
  "watch": {
   "anthropic/claude-": "claude",
   "openai/gpt-": "codex"
  }
 },
 "picker_reserved_lanes": {
  "claude": {},
  "codex": {
   "floor": {
    "reasoning": [
     "deep"
    ]
   }
  }
 },
 "plan_review_gate": "enforce",
 "review": {
  "plan_change_threshold": 0.3,
  "review_need_threshold": 0.5,
  "suite": {
   "budget": "20m",
   "command": [
    ".venv/bin/python",
    "-m",
    "pytest",
    "-n",
    "8",
    "-p",
    "no:cacheprovider",
    "-q",
    "-m",
    "not browser",
    "tests/"
   ]
  },
  "tiers": {
   "light_changed_lines": 50,
   "light_time_budget": "10m"
  }
 },
 "review_excluded_backends": [
  "claude",
  "native"
 ],
 "roles": {
  "cleanup": {
   "execution_capable": true,
   "sandbox": "worktree-full",
   "time_budget": "25m"
  },
  "documentation": {
   "execution_capable": true,
   "sandbox": "worktree-full"
  },
  "implement": {
   "by_spec_level": {
    "exact": {
     "effort": "high"
    },
    "guided": {
     "effort": "high"
    },
    "open": {
     "effort": "high"
    }
   },
   "execution_capable": true,
   "sandbox": "worktree-full",
   "time_budget": "8m"
  },
  "investigate": {
   "execution_capable": false,
   "sandbox": "read-only",
   "time_budget": "4m"
  },
  "review": {
   "by_spec_level": {
    "exact": {
     "effort": "high"
    },
    "guided": {
     "effort": "high"
    },
    "open": {
     "effort": "high"
    }
   },
   "execution_capable": false,
   "sandbox": "read-only",
   "time_budget": "4m"
  },
  "test": {
   "execution_capable": true,
   "sandbox": "worktree-full",
   "time_budget": "30m",
   "write_paths": [
    "."
   ]
  }
 },
 "routing": {
  "picker": "route"
 },
 "summary": {
  "at": [
   "dispatch",
   "completion",
   "micro-plan",
   "hold"
  ],
  "reflex": "what-why-how-when"
 },
 "ticker": {
  "history_rows": 100,
  "history_window": "4h"
 },
 "version": 1,
 "worktree": {
  "cleanup": "conservative",
  "scratch_min_free_bytes": 5368709120,
  "scratch_min_free_pct": 10
 }
}"""
#: The outcome every recorded attempt carries.
RECORDED_OUTCOME = {
    "action": "refuse",
    "decision_source": "refused",
    "fallback_reason": "no-eligible-candidates; default-backend-ineligible",
}

RECONSTRUCTED_CONFIG: dict[str, Any] = json.loads(CONFIG_JSON)


def repo_root(explicit: str | None = None) -> Path:
    """The reckon checkout this script replays against."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "reckon" / "crew" / "picker").is_dir():
            return parent
    raise SystemExit("cannot locate the reckon checkout above this script")


def _git_show(repo: Path, rev: str, path: str) -> bytes:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
        check=True,
    ).stdout


def config_from_layers(repo: Path, host_repo: Path) -> dict[str, Any] | None:
    """Re-resolve the recorded layers, or None when they are not reachable."""
    import tempfile

    from reckon import flight

    try:
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)

            def layer(where: Path, spec: tuple[str, str]) -> Path:
                rev, path = spec
                dest = work / path.replace("/", "_")
                dest.write_bytes(_git_show(where, rev, path))
                return dest

            return dict(
                flight.resolve(
                    "reckon",
                    host_path=layer(host_repo, HOST_LAYER),
                    project_path=layer(repo, CONFIG_LAYERS["project"]),
                    shipped_path=layer(repo, CONFIG_LAYERS["shipped"]),
                    catalogue_path=layer(repo, CONFIG_LAYERS["catalogue"]),
                ).config
            )
    except Exception:  # noqa: BLE001 - an unreachable layer is a fact to report
        return None


def build_request(repo: Path):
    """The pick request both attempts carried."""
    from reckon.crew.node import TaskNode
    from reckon.crew.picker import PickRequest

    node = TaskNode(id=NODE_ID, plan=PLAN, section=SECTION, **NODE_FIELDS)
    return PickRequest(
        "reckon", node, estimated_context=ESTIMATED_CONTEXT, session=SESSION
    )


def budget_view(config: dict[str, Any], *, utilisation: float | None) -> dict[str, Any]:
    """The lane-window reading, reconstructed.

    A pick-timing row keeps each excluded candidate's reason code and the
    request's context estimate, and nothing of the budget verdict the reason was
    judged on. So the reading is rebuilt here rather than loaded, and every
    attribution that rests on it is reported as conditional on it. ``None`` is
    an absent reading, which is what an unrefreshed window looks like; a number
    is a fresh reading at that percentage.
    """
    names = sorted(config.get("backends", {}))
    rows = []
    for name in names:
        if utilisation is None:
            state = {"backend": name, "headroom": "unknown", "expired": True}
        else:
            state = {
                "backend": name,
                "headroom": "known",
                "utilisation_pct": utilisation,
                "expired": False,
                "observed_at": "2026-11-03T02:28:00+00:00",
                "resets_at": "2026-11-03T05:00:00+00:00",
            }
        rows.append({"backend": name, "state": state})
    return {"backends": rows, "groups": []}


def _exclusion_rows(excluded: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """One (backend, reason code) row per reason, in the picker's own order."""
    from reckon.crew.picker import _reason_code

    return [
        (str(candidate.get("backend")), _reason_code(str(reason)))
        for candidate in excluded
        for reason in (candidate.get("reasons") or ())
    ]


def _outcome(selection: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": selection.get("action"),
        "decision_source": selection.get("decision_source"),
        "fallback_reason": selection.get("fallback_reason"),
    }


def replay(attempt: dict[str, Any], config: dict[str, Any], repo: Path, view: dict):
    """Ask the picker the question that recorded this attempt."""
    import reckon.crew.picker as picker_pkg
    from reckon.crew.picker import pick

    # A replay never appends to the fleet's pick-timings log: a second row for
    # this node would read as a further attempt to every later reader.
    picker_pkg._record_pick_timings = lambda *a, **k: None

    request = build_request(repo)
    lane = attempt.get("named_lane")
    if lane:
        from reckon.crew import dispatch_picker

        _narrowed, selection = dispatch_picker.lane_model_selection(
            node=request.node,
            config=config,
            backend=str(lane),
            project=request.project,
            repo=repo,
            session=request.session,
            records=[],
            verdict_inputs=None,
            budget_snapshot=view,
            authority=None,
        )
        return dict(selection or {})

    # The pick reads the project's promotion ledger for its attempt counts. The
    # replay supplies an empty one rather than loading the ambient ledger: a
    # ledger read makes the answer depend on state the record does not name, and
    # the reserved-lane tier term is met on the declared capability here, so the
    # attempt count never enters the reason either recorded attempt carries.
    return pick(
        request,
        config,
        repo=repo,
        records=[],
        cached_only=True,
        budget_snapshot=view,
    ).as_dict()


def report(attempt, selection) -> tuple[list[str], bool]:
    """One attempt's replay beside the row that recorded it."""
    lines = [f"=== {attempt['label']}  {attempt['started_at']}"]
    lane = attempt.get("named_lane")
    lines.append(
        "offer surface: "
        + (
            f"the {lane!r} lane alone, because the dispatch named --backend {lane}"
            if lane
            else "every configured backend, because the dispatch named no lane"
        )
    )
    lines.extend(
        f"  excluded {candidate.get('backend')}/{candidate.get('model')}: "
        + "; ".join(str(r) for r in candidate.get("reasons") or ())
        for candidate in selection.get("excluded") or ()
    )
    lines.extend(
        f"  offered {candidate.get('backend')}/{candidate.get('model')}"
        for candidate in selection.get("offered") or ()
    )
    lines.append(
        "  answer: action={} decision_source={} fallback_reason={}".format(
            selection.get("action"),
            selection.get("decision_source"),
            selection.get("fallback_reason"),
        )
    )
    got = _exclusion_rows(selection.get("excluded") or [])
    want = [tuple(row) for row in attempt["recorded"]]
    lines.append(f"  recorded rows: {len(want)}   replayed rows: {len(got)}")
    matched = got == want
    if matched:
        lines.append("  exclusion set: reproduced exactly, in the picker's order")
    else:
        lines.append("  exclusion set: DIFFERS")
        lines.extend(f"    recorded only: {row}" for row in want if row not in got)
        lines.extend(f"    replayed only: {row}" for row in got if row not in want)
    reproduced = _outcome(selection) == RECORDED_OUTCOME
    lines.append(f"  outcome: {'reproduced' if reproduced else 'DIFFERS'}")
    return lines, matched and reproduced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay two recorded pick refusals.")
    parser.add_argument("--repo", default=None, help="the checkout to replay against")
    parser.add_argument(
        "--host-repo",
        default="~/Code/dotfiles",
        help="the checkout holding the host flight layer's history",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="re-resolve the recorded layers and exit 2 on a disagreement",
    )
    args = parser.parse_args(argv)

    repo = repo_root(args.repo)
    sys.path.insert(0, str(repo))
    config = RECONSTRUCTED_CONFIG

    lines: list[str] = [
        f"repo: {repo}",
        f"node: {NODE_ID}",
        f"plan section: {PLAN} {SECTION}",
        f"estimated_context: {ESTIMATED_CONTEXT}",
        "",
        "--- runtime inputs",
        "flight-config: reconstructed",
        "  layers: "
        + "; ".join(f"{k}@{v[0]} {v[1]}" for k, v in CONFIG_LAYERS.items()),
        f"  host: {HOST_LAYER[1]} at dotfiles@{HOST_LAYER[0]}",
        "  the resolved configuration is embedded in this script: a config input",
        "  reconstructed from those layers, never a routing or competence verdict,",
        "  and the scan below is the picker's own",
        "budget-window-reading: unrecorded, reconstructed",
        "  a pick-timing row keeps each reason code and the context estimate and",
        "  nothing of the budget verdict behind it; both of that reading's branches",
        "  are replayed below, because the record does not say which one it was",
        "lane-document: unrecorded (a live published file), not decisive",
        "  it feeds worker slots and congestion only, and no exclusion reads it",
        "promotion-ledger: not consulted",
        "  the pick's attempt count reads it, and both lanes' tier floors are met on",
        "  the declared capability, so the count never enters either reason; an empty",
        "  ledger is supplied so the replay answers the record and not the ambient",
        "  state of whichever checkout runs it",
        "serving-probe: not reached",
        "  every candidate carried a reason before the probe, so availability reads",
        "  not-probed and no probe result entered either refusal",
        f"plan-section: recorded (docs/plans/{PLAN}.html {SECTION})",
        "node-definition: recorded (the coordinator's dispatch command line)",
        "",
    ]

    if args.check_config:
        from_layers = config_from_layers(repo, Path(args.host_repo).expanduser())
        if from_layers is None:
            lines.append("config-check: the recorded layers are not reachable here")
        elif from_layers == config:
            lines.append("config-check: the embedded configuration matches the layers")
        else:
            lines.append(
                "config-check: THE EMBEDDED CONFIGURATION DIFFERS FROM THE LAYERS"
            )
            print("\n".join(lines))
            return 2
        lines.append("")

    failures = 0
    for attempt in ATTEMPTS:
        selection = replay(attempt, config, repo, budget_view(config, utilisation=None))
        block, ok = report(attempt, selection)
        lines.extend(block)
        lines.append("")
        failures += 0 if ok else 1

    lines.append(
        "=== attempt-1, a fresh window reading at 72% instead of an absent one"
    )
    selection = replay(ATTEMPTS[0], config, repo, budget_view(config, utilisation=72.0))
    rows = _exclusion_rows(selection.get("excluded") or [])
    same = rows == [tuple(row) for row in ATTEMPTS[0]["recorded"]]
    lines.append(f"  exclusion set: {'identical to the record' if same else 'DIFFERS'}")
    lines.append(
        "  the record does not discriminate between an absent reading and one at or"
        " past the ceiling: both withhold the reserved lanes on the same term"
    )
    failures += 0 if same else 1
    lines.append("")

    lines.append(
        "inputs that decided each refusal: the node's declared capability against"
        " each reserved lane's tier floor, which is met, so the tier term withheld"
        " neither lane; the lane-window term of the reserved-lane policy, which"
        " withheld claude and codex; the oversize waiver, which was skipped because"
        " an unreserved candidate carries a reason other than context fit; and"
        " clive's own context-fit and competence verdicts from the unchanged scan"
    )
    lines.append(
        "replayed: 2 attempts, refusals "
        + ("reproduced" if not failures else "NOT reproduced")
    )
    print("\n".join(lines))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
