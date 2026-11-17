"""Every lock-taking ``flock`` under ``reckon/`` is judged as safe on one node.

``flock`` is granted by the local kernel, so on a shared home a holder on one
node does not exclude a holder on another. A lock guarding shared state takes
``reckon.node_exclusive_lock.exclusive_lock``, whose record lock the file
server itself holds across nodes.

Two tables account for every lock-taking ``flock`` call under ``reckon/``:

- ``JUDGED`` records why a site that keeps a bare ``flock`` is safe on one node:
  its lock file and what it guards exist only on one host, or a named lease
  decides the foreign-host case. Each entry also records how many lock-taking
  calls that site makes, so a second bare ``flock`` added inside an already
  judged function is a new unjudged lock rather than more of the same.
- ``MIGRATED`` names the sites that left the pending table. Each is proved to
  take ``exclusive_lock``, so the pending table cannot empty by a site dropping
  its lock altogether.

The pending table is empty and stays empty: a shared-home site either moves onto
the cross-node lock or is judged, never parked.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Callable
from pathlib import Path

from reckon import interface_counts

ROOT = Path(
    os.environ.get("RECKON_LOCK_GUARD_ROOT", "") or Path(__file__).resolve().parents[1]
)
MODULE_SCOPE = "<module>"

NODE_LOCAL = "the lock file and what it guards exist only on this host"
HOST_LEASE = "the producer host lease decides the foreign-host case"
FOLLOWER_LEASE = "the renewed follower lease decides the foreign-host case"
LOCK_HALF = "the same-node half of the cross-node lock; the record lock follows it"

# Judged: a bare flock that is safe on the shared home, with the mechanism that
# makes it safe and the number of lock-taking calls that accounts for. Every
# entry is a lock-taking call, so an entry left behind by a removed call fails
# the guard rather than lingering as documentation.
JUDGED: dict[str, tuple[str, int]] = {
    "reckon/checkout_trust.py::_take_import_probe_lock": (NODE_LOCAL, 1),
    "reckon/checkout_trust.py::materialize_committed_tree": (NODE_LOCAL, 1),
    "reckon/checkout_trust.py::pin_materialized_tree": (NODE_LOCAL, 1),
    "reckon/crew/dispatch_watch.py::_ensure_watch_producer": (HOST_LEASE, 1),
    "reckon/crew/fleet_supervisor.py::DeclaredServices._pass_allows_restart": (
        NODE_LOCAL,
        2,
    ),
    "reckon/crew/fleet_supervisor.py::lock_held": (NODE_LOCAL, 1),
    "reckon/crew/fleet_supervisor.py::run_declared_service": (NODE_LOCAL, 1),
    "reckon/crew/fleet_supervisor.py::service_pass": (NODE_LOCAL, 2),
    "reckon/crew/follower_registration.py::_FollowerRegistration.acquire": (
        FOLLOWER_LEASE,
        1,
    ),
    "reckon/crew/follower_registration.py::_follower_liveness": (FOLLOWER_LEASE, 1),
    "reckon/crew/follower_registration.py::sweep_released_followers": (
        FOLLOWER_LEASE,
        1,
    ),
    "reckon/crew/recovery_watch.py::_recreate_unlinked_registration": (HOST_LEASE, 1),
    "reckon/crew/recovery_watch.py::unwatch": (HOST_LEASE, 2),
    "reckon/crew/runs.py::_probe_seat_read_only": (HOST_LEASE, 1),
    "reckon/crew/runs.py::_project_watch_claim": (HOST_LEASE, 1),
    "reckon/crew/runs.py::_reconcile_watch_record": (HOST_LEASE, 1),
    "reckon/crew/session_host.py::SessionHost.acquire_owner_lock": (NODE_LOCAL, 1),
    "reckon/crew/watch_unit.py::_register_watch_unit": (HOST_LEASE, 1),
    "reckon/crew/watch_unit.py::_seat_is_held": (HOST_LEASE, 1),
    "reckon/crew/watch_unit.py::watch_state": (HOST_LEASE, 1),
    "reckon/node_exclusive_lock.py::_hold_lock": (LOCK_HALF, 1),
}

# Migrated: sites that left the pending table for ``exclusive_lock``. A site
# listed here that stops taking the cross-node lock fails, so an emptied pending
# table cannot hide a lock that was dropped rather than migrated.
MIGRATED: dict[str, str] = {
    "reckon/crew/runs.py::_pointer_lock": "run pointer lock",
    "reckon/crew/runs.py::queue_dispatch": "queue dispatch lock",
    "reckon/crew/runs.py::update_watch_registration": "watch registration lock",
    "reckon/ledger.py::_ledger_write_lock": "ledger write lock",
    "reckon/crew/queue_landing.py::land_once": "landing-pass lock",
    "reckon/crew/admission/service.py::_admit_lock": "admit lock",
    "reckon/crew/placement.py::_reservation_hold_lock": "placement reservation",
    "reckon/_store.py::_serialized_path_lock": "store path lock",
    "reckon/compliance.py::_write_entries": "compliance store lock",
    "reckon/crew/review_records.py::_index_lock": "review index lock",
    "reckon/crew/recovery_review_dispatch.py::_dispatch_text_review_for_run": (
        "plan-review dispatch lock"
    ),
    "reckon/crew/recovery_watch.py::_update_git_answer_store": "git answer store lock",
    "reckon/crew/obligations.py::_record_index_lag": "index-lag record lock",
    "reckon/crew/resumption.py::probe_lane_availability": "lane probe cache lock",
    "reckon/crew/resumption.py::_write_sweep_status": "sweep status lock",
    "reckon/crew/dispatch_peer.py::_update_peer_index": "peer index lock",
    "reckon/crew/budget_reset.py::_locked": "budget reset lock",
    "reckon/crew/model_catalogue.py::_refresh_lock": "catalogue refresh lock",
    "reckon/crew/paid_lanes.py::refresh_reserved_window": "reserved window lock",
    "reckon/crew/paid_lanes.py::publish_document": "paid lane document lock",
    "reckon/crew/picker/outcomes.py::record_picker_hold": "picker holds lock",
}

# Pending: shared-home locks not yet moved onto exclusive_lock. Empty now that
# the migration is complete; a new bare flock is judged or migrated, not parked.
PENDING: frozenset[str] = frozenset()

REMEDY = (
    "take reckon.node_exclusive_lock.exclusive_lock, whose record lock the file "
    "server holds across nodes, or judge the shared-home site above with its "
    "reason"
)


def _python_trees(root: Path) -> dict[str, ast.Module]:
    """Parse every Python module under ``root/reckon``, keyed by repo path.

    A checkout's module set is the union of the census reader's revision
    listing and the working tree, so a file deleted from the working tree is
    not read and a new untracked one still is. The bytes are always the working
    tree's, so an uncommitted lock is caught by the same assertion as a
    committed one. Any other root is a plain directory and is walked directly.
    """
    if (root / ".git").exists():
        paths = set(interface_counts.read_trees(root, "HEAD"))
    else:
        paths = set()
    paths.update(
        path.relative_to(root).as_posix() for path in (root / "reckon").rglob("*.py")
    )
    trees: dict[str, ast.Module] = {}
    for path in paths:
        source = root / path
        if source.is_file():
            trees[path] = ast.parse(
                source.read_text(encoding="utf-8-sig"), filename=path
            )
    return trees


def _callee_name(call: ast.Call) -> str:
    callee = call.func
    return (
        callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
    )


def _takes_a_lock(call: ast.Call) -> bool:
    """Whether this ``flock`` call asks for a lock rather than releasing one."""
    if _callee_name(call) != "flock":
        return False
    operation = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "operation":
            operation = keyword.value
    if operation is None:
        return True
    return ast.unparse(operation).strip().rsplit(".", 1)[-1] != "LOCK_UN"


def _takes_the_cross_node_lock(call: ast.Call) -> bool:
    return _callee_name(call) == "exclusive_lock"


def _file_sites(tree: ast.Module, takes: Callable[[ast.Call], bool]) -> dict[str, int]:
    """Map each call matching ``takes`` to its enclosing definition's name."""
    qualified = {
        id(node): name
        for node, name, _public, _nested in interface_counts.definitions(tree)
    }
    enclosing: list[ast.AST] = []
    sites: dict[str, int] = {}

    class Visitor(ast.NodeVisitor):
        def _enter(self, node: ast.AST) -> None:
            enclosing.append(node)
            self.generic_visit(node)
            enclosing.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._enter(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._enter(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._enter(node)

        def visit_Call(self, node: ast.Call) -> None:
            if takes(node):
                innermost = enclosing[-1] if enclosing else None
                owner = qualified.get(id(innermost), MODULE_SCOPE)
                sites[owner] = sites.get(owner, 0) + 1
            self.generic_visit(node)

    Visitor().visit(tree)
    return sites


def _sites(
    trees: dict[str, ast.Module], takes: Callable[[ast.Call], bool]
) -> dict[str, int]:
    """Map ``file::qualified.function`` to its ``takes`` call count."""
    sites: dict[str, int] = {}
    for path, tree in trees.items():
        for owner, count in _file_sites(tree, takes).items():
            key = f"{path}::{owner}"
            sites[key] = sites.get(key, 0) + count
    return sites


def flock_sites(trees: dict[str, ast.Module]) -> dict[str, int]:
    """Map ``file::qualified.function`` to its count of lock-taking flocks.

    The qualified name comes from the census's own definition walker, so a
    method keys as ``Class.method`` and a nested function as ``outer.inner``
    exactly as the interface census names it.
    """
    return _sites(trees, _takes_a_lock)


def cross_node_lock_sites(trees: dict[str, ast.Module]) -> set[str]:
    """The ``file::qualified.function`` keys that call ``exclusive_lock``."""
    return set(_sites(trees, _takes_the_cross_node_lock))


def test_every_lock_taking_flock_is_judged_or_pending() -> None:
    found = set(flock_sites(_python_trees(ROOT)))
    unjudged = sorted(found - set(JUDGED) - PENDING)
    assert not unjudged, (
        f"an unjudged bare flock excludes nothing across nodes: {unjudged}; {REMEDY}"
    )


def test_every_listed_site_still_holds_its_call() -> None:
    found = set(flock_sites(_python_trees(ROOT)))
    stale = sorted((set(JUDGED) | PENDING) - found)
    assert not stale, f"listed sites with no lock-taking flock left: {stale}"


def test_every_judged_site_holds_its_recorded_call_count() -> None:
    found = flock_sites(_python_trees(ROOT))
    changed = {
        key: {"recorded": JUDGED[key][1], "found": found[key]}
        for key in JUDGED
        if key in found and found[key] != JUDGED[key][1]
    }
    assert not changed, (
        "a judged site's lock-taking flock count changed, so it carries a lock "
        f"the entry does not account for: {sorted(changed)}; {REMEDY}"
    )


def test_every_migrated_site_takes_the_cross_node_lock() -> None:
    holding = cross_node_lock_sites(_python_trees(ROOT))
    missing = sorted(set(MIGRATED) - holding)
    assert not missing, (
        "a site that left the pending table takes no cross-node lock, so it "
        f"holds only the node-local flock: {missing}; {REMEDY}"
    )


def test_judged_and_pending_do_not_overlap() -> None:
    both = sorted(set(JUDGED) & PENDING)
    assert not both, f"sites claimed as both safe and unmigrated: {both}"


def test_a_bare_flock_in_a_fresh_module_is_reported() -> None:
    tree = ast.parse(
        "import fcntl\n"
        "def write_shared(handle):\n"
        "    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
        "    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)\n"
    )
    assert flock_sites({"reckon/fresh.py": tree}) == {
        "reckon/fresh.py::write_shared": 1
    }


def test_a_second_flock_in_an_already_judged_module_is_counted() -> None:
    tree = ast.parse(
        "import fcntl\n"
        "def judged(handle):\n"
        "    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
        "    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    )
    found = flock_sites({"reckon/judged.py": tree})["reckon/judged.py::judged"]
    assert found == 2, (
        "a second flock inside an already judged function must count 2 against "
        f"the recorded 1, not {found}"
    )
