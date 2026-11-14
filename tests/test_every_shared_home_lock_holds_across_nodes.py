"""Every lock-taking ``flock`` under ``reckon/`` is judged as safe on one node.

``flock`` is granted by the local kernel, so on a shared home a holder on one
node does not exclude a holder on another. A lock guarding shared state takes
``reckon.node_exclusive_lock.exclusive_lock``, whose record lock the file
server itself holds across nodes.

The bare ``flock`` calls that remain are each listed below by file and
qualified enclosing function: either their lock file and what it guards exist
only on one host, or a named lease decides the foreign-host case. The pending
table names the shared-home sites that have not moved yet; the plan finishes
when it is empty.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

from reckon import interface_counts

ROOT = Path(
    os.environ.get("RECKON_LOCK_GUARD_ROOT", Path(__file__).resolve().parents[1])
)
MODULE_SCOPE = "<module>"

NODE_LOCAL = "the lock file and what it guards exist only on this host"
HOST_LEASE = "the producer host lease decides the foreign-host case"
FOLLOWER_LEASE = "the renewed follower lease decides the foreign-host case"
LOCK_HALF = "the same-node half of the cross-node lock; lockf follows it"

# Judged: a bare flock that is safe on the shared home, with the mechanism that
# makes it safe. Every entry is a lock-taking call, so an entry left behind by
# a removed call fails the guard rather than lingering as documentation.
JUDGED: dict[str, str] = {
    "reckon/checkout_trust.py::_take_import_probe_lock": NODE_LOCAL,
    "reckon/checkout_trust.py::materialize_committed_tree": NODE_LOCAL,
    "reckon/checkout_trust.py::pin_materialized_tree": NODE_LOCAL,
    "reckon/crew/dispatch_watch.py::_ensure_watch_producer": HOST_LEASE,
    "reckon/crew/fleet_supervisor.py::DeclaredServices._pass_allows_restart": NODE_LOCAL,
    "reckon/crew/fleet_supervisor.py::lock_held": NODE_LOCAL,
    "reckon/crew/fleet_supervisor.py::run_declared_service": NODE_LOCAL,
    "reckon/crew/fleet_supervisor.py::service_pass": NODE_LOCAL,
    "reckon/crew/follower_registration.py::_FollowerRegistration.acquire": FOLLOWER_LEASE,
    "reckon/crew/follower_registration.py::_follower_liveness": FOLLOWER_LEASE,
    "reckon/crew/follower_registration.py::sweep_released_followers": FOLLOWER_LEASE,
    "reckon/crew/recovery_watch.py::_recreate_unlinked_registration": HOST_LEASE,
    "reckon/crew/recovery_watch.py::unwatch": HOST_LEASE,
    "reckon/crew/runs.py::_probe_seat_read_only": HOST_LEASE,
    "reckon/crew/runs.py::_project_watch_claim": HOST_LEASE,
    "reckon/crew/runs.py::_reconcile_watch_record": HOST_LEASE,
    "reckon/crew/session_host.py::SessionHost.acquire_owner_lock": NODE_LOCAL,
    "reckon/crew/watch_unit.py::_register_watch_unit": HOST_LEASE,
    "reckon/crew/watch_unit.py::_seat_is_held": HOST_LEASE,
    "reckon/crew/watch_unit.py::watch_state": HOST_LEASE,
    "reckon/node_exclusive_lock.py::exclusive_lock": LOCK_HALF,
}

# Pending: shared-home locks not yet moved onto exclusive_lock. Each migration
# removes its rows from here and adds judged rows where a site stays bare.
PENDING: frozenset[str] = frozenset(
    {
        "reckon/_store.py::_serialized_path_lock",
        "reckon/compliance.py::_locked",
        "reckon/crew/admission/service.py::_admit_lock",
        "reckon/crew/budget_reset.py::_locked",
        "reckon/crew/dispatch_peer.py::_update_peer_index",
        "reckon/crew/model_catalogue.py::_refresh_lock",
        "reckon/crew/obligations.py::_record_index_lag",
        "reckon/crew/paid_lanes.py::publish_document",
        "reckon/crew/paid_lanes.py::refresh_reserved_window",
        "reckon/crew/picker/outcomes.py::record_picker_hold",
        "reckon/crew/placement.py::_reservation_hold_lock",
        "reckon/crew/queue_landing.py::land_once",
        "reckon/crew/recovery_review_dispatch.py::_dispatch_text_review_for_run",
        "reckon/crew/review_records.py::_index_lock",
        "reckon/crew/runs.py::_pointer_lock",
        "reckon/crew/runs.py::queue_dispatch",
        "reckon/crew/runs.py::update_watch_registration",
        "reckon/ledger.py::_ledger_write_lock",
    }
)

REMEDY = (
    "take reckon.node_exclusive_lock.exclusive_lock, whose record lock the file "
    "server holds across nodes, or judge the site above with its reason"
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


def _takes_a_lock(call: ast.Call) -> bool:
    """Whether this ``flock`` call asks for a lock rather than releasing one."""
    callee = call.func
    name = (
        callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
    )
    if name != "flock":
        return False
    operation = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "operation":
            operation = keyword.value
    if operation is None:
        return True
    return ast.unparse(operation).strip().rsplit(".", 1)[-1] != "LOCK_UN"


def flock_sites(trees: dict[str, ast.Module]) -> dict[str, int]:
    """Map ``file::qualified.function`` to its count of lock-taking flocks.

    The qualified name comes from the census's own definition walker, so a
    method keys as ``Class.method`` and a nested function as
    ``outer.inner`` exactly as the interface census names it.
    """
    sites: dict[str, int] = {}
    for path, tree in trees.items():
        for owner, count in _file_sites(tree).items():
            sites[f"{path}::{owner}"] = sites.get(f"{path}::{owner}", 0) + count
    return sites


def _file_sites(tree: ast.Module) -> dict[str, int]:
    """Map each flock-taking call in one module to its enclosing definition."""
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
            if _takes_a_lock(node):
                innermost = enclosing[-1] if enclosing else None
                owner = qualified.get(id(innermost), MODULE_SCOPE)
                sites[owner] = sites.get(owner, 0) + 1
            self.generic_visit(node)

    Visitor().visit(tree)
    return sites


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
