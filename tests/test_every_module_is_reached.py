"""Every module under ``reckon/`` is approached from a production entry point.

A node's gate proves its code works when called; it does not prove anything
calls it. An unreached validator reports nothing, an unreached hold holds
nothing, and a plan that shipped one reads as delivered. This ratchet fails
when a module is unreached and is not listed with the plan that owns wiring it,
and equally when a listed module has become reached (or removed) and its entry
was left behind -- the second direction is what keeps the allowlist exactly the
modules unreached at the revision it was written against, so the list shrinks as
the unfunded modules are wired or retired.

The tree read is ``RECKON_REACH_ROOT`` when set (a throwaway repository carrying
an extra module is how the ratchet is shown to fire) and this checkout
otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path

from reckon.served_code import package_modules, reached

ROOT = Path(os.environ.get("RECKON_REACH_ROOT") or Path(__file__).resolve().parents[1])

#: Unreached module -> the plan that owns reaching it (or retiring it).
ALLOWLIST = {
    "reckon.crew.carryover_census": "the-ledger-is-one-file-per-run",
    "reckon.crew.context_budget": "nothing-lands-unreached",
    "reckon.crew.death_census": "the-ledger-is-one-file-per-run",
    "reckon.crew.hold": "a-window-paces-the-lane",
    "reckon.crew.pace_replay": "a-window-paces-the-lane",
    "reckon.crew.serving_origin": "a-window-paces-the-lane",
    "reckon.project_state_parity": "project-state-fleet-migration",
}


def _violations(
    all_modules: set[str],
    reached_modules: set[str],
    allowlist: dict[str, str],
) -> list[str]:
    """One message per module that grew the allowlist or left a stale entry."""

    unreached = all_modules - reached_modules
    grew = [
        f"{module}: unreached and not in the allowlist; wire it to a production "
        "entry point or add it with the plan that owns it"
        for module in sorted(unreached - set(allowlist))
    ]
    stale = [
        f"{module}: listed as unreached but is reached or absent; remove it from "
        "the allowlist"
        for module in sorted(set(allowlist) - unreached)
    ]
    return grew + stale


def test_every_module_is_reached():
    repo = str(ROOT)
    every = set(package_modules(repo, "HEAD"))
    reach = reached(repo, "HEAD")

    failures = _violations(every, set(reach.modules), ALLOWLIST)
    assert not failures, "\n".join(failures)
