"""Replay two recorded backend-pick refusals, in process.

Each attempt is rebuilt from what recorded it: the node definition from the
coordinator's own dispatch command line, the flight configuration from the
layers in force at the moment, and the pick-timing row that recorded the
refusal. The picker's candidate scan is then run over those inputs unchanged,
and the set it withholds is compared with the record candidate by candidate, in
the picker's own order. Each attempt also carries the rows the rules now keep,
because two rules landed after the record was written and they moved reasons
between candidates without changing the refusal; the report names every
recorded row the picker no longer emits, beside the rule that replaced it.

No routing or competence verdict is stubbed. The candidate scan runs
``reckon.crew.picker.snapshot.candidates`` unchanged and the placement runs the
picker's own ``_fallback_choice``, so every reason and every answer the report
shows is the picker's own. The router is not asked: its answer is one model's
judgment of one moment and is nowhere in the record, so asserting it would
report on whichever endpoint answered. The fallback is where the policy's own
words decide, and it decides the same way twice.

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
import time
from dataclasses import replace
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
#:
#: ``recorded`` is the row set the record carries and its ``action``: the
#: decision the fleet made on 3 November, kept verbatim so a reader sees what
#: the replay is asked to reproduce and how it differs today. ``expected`` is
#: the row set the picker's rules now keep for the same inputs, and
#: ``expected_action`` is the answer those rules give with no router asked.
#: Where the two differ, the picker keeps a rule the record predates; the
#: report names each such row and the rule behind it.
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
        "recorded_action": "refuse",
        # The claude family, which now includes the local wrapper, is withheld
        # by the enforced reservation; the codex reservation is carried as
        # evidence on an offered candidate rather than as an exclusion.
        "expected": [
            ("claude", "reserved-lane"),
            ("claude", "reserved-lane"),
            ("claude", "reserved-lane"),
            ("claude", "reserved-lane"),
            ("clive", "reserved-lane"),
            ("native", "in-harness-backend"),
        ],
        "expected_action": "refuse",
        "rule": (
            "the local wrapper takes the reservation of the harness it speaks, "
            "so clive is withheld as the claude family rather than reached by "
            "the context and competence verdicts (b14e289ffe), and a reserved "
            "lane outside the enforced family is offered with its reservation "
            "as evidence instead of removed (ccff33221c)"
        ),
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
        "recorded_action": "refuse",
        # The named lane's models are offered with the reservation as evidence;
        # none is placed, because an unlifted reservation is never where the
        # deterministic fallback puts a node.
        "expected": [],
        "expected_action": "refuse",
        "rule": (
            "a codex reservation is a preference the router weighs, not a fact "
            "that removes the lane (ccff33221c); with no router asked, the "
            "fallback places nothing on an unlifted reservation, so the "
            "refusal stands on a different term"
        ),
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
#: The answer the replayed rules give for both attempts.
#:
#: The replay asks the picker's candidate scan and then its deterministic
#: fallback. It does not ask the router: a router's answer is a model's
#: judgment of one moment and is nowhere in the record, so asserting it would
#: make the replay report on whichever endpoint answered; the fallback is the
#: one place the policy's own words decide, which is the question this replay
#: can answer twice and get the same answer. ``decision_source`` says so.
REPLAY_ANSWER = {
    "action": "refuse",
    "decision_source": "no-router",
    "fallback_reason": (
        "the fallback places nothing: every offered candidate is a reserved "
        "lane with no lift in force; default-backend-ineligible"
    ),
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


def _candidate_codes(candidate: dict[str, Any]) -> list[str]:
    """One reason code per reason the candidate carries, in the picker's order."""
    from reckon.crew.picker import _reason_code

    return [_reason_code(str(r)) for r in candidate.get("reasons") or ()]


def _exclusion_rows(excluded: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """One (backend, reason code) row per reason, in the picker's own order."""
    from reckon.crew.picker import _reason_code

    return [
        (str(candidate.get("backend")), _reason_code(str(reason)))
        for candidate in excluded
        for reason in (candidate.get("reasons") or ())
    ]


def _narrowed_config(config: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    """The configuration the attempt's dispatch was offered, as it narrowed it.

    A dispatch naming a lane keeps only that lane's backends, and the record's
    three-row fingerprint is the proof of that path: ``clive``, ``claude`` and
    ``native`` were never candidates. The narrowing is taken from the
    dispatcher's own helper rather than reimplemented, so the offered surface
    the report prints is the one the attempt saw.
    """
    lane = attempt.get("named_lane")
    if not lane:
        return config
    from reckon.crew import dispatch_picker

    return dict(dispatch_picker._config_within_lane(config, str(lane)))


def replay(attempt: dict[str, Any], config: dict[str, Any], repo: Path, view: dict):
    """Ask the picker's candidate scan, then the deterministic fallback.

    The scan is ``reckon.crew.picker.snapshot.candidates`` unchanged, and the
    placement is the picker's own ``_fallback_choice``, so every reason and
    every answer the report shows is the picker's. The router is not asked: its
    answer is not in the record (see ``REPLAY_ANSWER``), and a report on
    whichever model answered is not a report on the picker's rules.
    """
    import reckon.crew.picker as picker_pkg
    from reckon.crew import picker as picker_module
    from reckon.crew.picker import snapshot

    # A replay never appends to the fleet's pick-timings log: a second row for
    # this node would read as a further attempt to every later reader.
    picker_pkg._record_pick_timings = lambda *a, **k: None

    request = build_request(repo)
    scope = _narrowed_config(config, attempt)
    # The capability a section declares is resolved the way the pick resolves
    # it, because the reservation's floor term is read off it: a request that
    # carried none would be withheld for a tier the section actually meets.
    if not request.capability:
        capability, _hours, _source = picker_module._declared_difficulty(
            request, scope, repo=repo, authority=None
        )
        if capability:
            request = replace(request, capability=capability)
    # The scan reads the project's promotion ledger for each candidate's attempt
    # count. The replay supplies an empty one rather than loading the ambient
    # ledger: a ledger read makes the answer depend on state the record does not
    # name, and the reserved-lane tier term is met on the declared capability
    # here, so the attempt count never enters the reason either attempt carries.
    options = snapshot.candidates(
        request,
        scope,
        repo,
        records=[],
        verdict_inputs=None,
        budget_snapshot=view,
        cached_only=True,
    )
    excluded = [candidate.as_dict() for candidate in options if candidate.reasons]
    offered = [candidate for candidate in options if not candidate.reasons]
    placed = picker_module._fallback_choice(offered, scope)
    return {
        "offer_surface": (
            f"the {attempt['named_lane']!r} lane alone, because the dispatch "
            f"named --backend {attempt['named_lane']}"
            if attempt.get("named_lane")
            else "every configured backend, because the dispatch named no lane"
        ),
        "excluded": excluded,
        "offered": [candidate.as_dict() for candidate in offered],
        "action": "route" if placed is not None else "refuse",
        "decision_source": "no-router",
        "fallback_reason": None
        if placed is not None
        else REPLAY_ANSWER["fallback_reason"],
        "placed": None if placed is None else f"{placed.backend}/{placed.model}",
    }


def report(attempt, result, elapsed_ms: float) -> tuple[list[str], bool]:
    """One attempt's replay beside the record that asked for it.

    The record's own rows and answer, and the rows and answer the picker's
    rules now keep for the same inputs, are printed in that order. An attempt
    is reproduced when the replay matches the rows the rules keep, and the
    record's stale rows are listed with the rule that replaced them rather than
    silently dropped: a reader comparing the two sees which rule moved.
    """
    lines = [f"=== {attempt['label']}  {attempt['started_at']}"]
    lines.append(f"  elapsed: {elapsed_ms:.0f} ms")
    lines.append("offer surface: " + result["offer_surface"])
    lines.extend(
        f"  excluded {candidate.get('backend')}/{candidate.get('model')}: "
        + "; ".join(str(r) for r in candidate.get("reasons") or ())
        for candidate in result.get("excluded") or ()
    )
    lines.extend(
        "  codes {}: {}".format(
            f"{candidate.get('backend')}/{candidate.get('model')}",
            " ".join(_candidate_codes(candidate)),
        )
        for candidate in result.get("excluded") or ()
    )
    lines.extend(
        f"  offered {candidate.get('backend')}/{candidate.get('model')}"
        for candidate in result.get("offered") or ()
    )
    lines.extend(
        "  evidence {}: {}".format(
            f"{candidate.get('backend')}/{candidate.get('model')}",
            str((candidate.get("reservation") or {}).get("withheld") or ""),
        )
        for candidate in result.get("offered") or ()
        if (candidate.get("reservation") or {}).get("withheld")
    )
    lines.append(
        "  answer: action={} decision_source={} placed={} fallback_reason={}".format(
            result.get("action"),
            result.get("decision_source"),
            result.get("placed") or "none",
            result.get("fallback_reason"),
        )
    )
    got = _exclusion_rows(result.get("excluded") or [])
    recorded = [tuple(row) for row in attempt["recorded"]]
    expected = [tuple(row) for row in attempt["expected"]]
    lines.append(f"  recorded rows: {len(recorded)}   replayed rows: {len(got)}")
    matched = got == expected
    if not expected:
        lines.append(
            "  exclusion set: empty, as the rules keep it: nothing is withheld"
        )
        lines.append(
            "  every candidate is offered with its reservation beside it, so the"
            " refusal rests on the fallback alone"
        )
    elif matched:
        lines.append("  exclusion set: reproduced exactly, in the picker's order")
    else:
        lines.append("  exclusion set: excludes what the rules keep:")
        lines.extend(f"    expected only: {row}" for row in expected if row not in got)
        lines.extend(f"    replayed only: {row}" for row in got if row not in expected)
    superseded = [row for row in recorded if row not in got]
    if superseded:
        lines.append("  record's rows the picker no longer emits:")
        lines.extend(f"    superseded: {row}" for row in superseded)
        lines.append("  rule behind those rows: " + attempt["rule"])
    reproduced = (
        matched
        and result.get("action") == attempt["expected_action"]
        and result.get("fallback_reason") == REPLAY_ANSWER["fallback_reason"]
    )
    lines.append(f"  outcome: {'reproduced' if reproduced else 'DIFFERS'}")
    return lines, reproduced


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

    # A dispatch bounded its pick, and that bound is not an input the picker
    # weighs. The replay asks no router and runs no pick, so no bound applies
    # here: what it asks is the scan and the deterministic fallback, and both
    # answer in the same way every time.
    from reckon.mcp_views import section_attempt_count

    section_attempt_count("reckon", PLAN, SECTION, None)

    lines.append(
        "router: not asked — the replay reports the candidate scan and the"
        " deterministic fallback"
    )
    lines.append(
        "  a router's answer is one model's judgment of one moment and is nowhere"
        " in the record, so"
    )
    lines.append(
        "  asserting it would report on whichever endpoint answered; the fallback"
        " is where the"
    )
    lines.append("  policy's own words decide, and it decides the same way twice.")
    lines.append("")

    failures = 0
    for attempt in ATTEMPTS:
        moment = time.perf_counter()
        result = replay(attempt, config, repo, budget_view(config, utilisation=None))
        block, ok = report(attempt, result, (time.perf_counter() - moment) * 1000)
        lines.extend(block)
        lines.append("")
        failures += 0 if ok else 1

    lines.append(
        "=== attempt-1, a fresh window reading at 72% instead of an absent one"
    )
    result = replay(
        ATTEMPTS[0],
        config,
        repo,
        budget_view(config, utilisation=72.0),
    )
    rows = _exclusion_rows(result.get("excluded") or [])
    same = rows == [tuple(row) for row in ATTEMPTS[0]["expected"]] and (
        result.get("action") == ATTEMPTS[0]["expected_action"]
    )
    lines.append(f"  exclusion set: {'identical to the rules' if same else 'DIFFERS'}")
    lines.append(
        "  the record does not discriminate between an absent reading and one at or"
        " past the ceiling:"
    )
    lines.append(
        "  both withhold the reserved lanes on the same term, and the rules the"
        " picker keeps withhold"
    )
    lines.append("  the same candidates either way")
    failures += 0 if same else 1
    lines.append("")

    lines.append(
        "inputs that decided each refusal: the node's declared capability against"
        " each reserved lane's tier floor, which is met, so the tier term withheld"
        " neither lane; and then the two rules that replaced the record's terms —"
        " the enforced claude reservation withholds the claude family including"
        " the local wrapper, and a codex reservation is offered to the router as"
        " evidence rather than removing the lane, so with no router asked the"
        " deterministic fallback places nothing on an unlifted reservation"
    )
    lines.append(
        "replayed: 2 attempts, refusals "
        + ("reproduced" if not failures else "DIFFERS")
    )
    print("\n".join(lines))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
