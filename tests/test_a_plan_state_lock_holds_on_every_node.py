"""A plan-state resource lock excludes a holder on another node.

``flock`` is granted per node, so a recovery pass on one node can rewrite a
sprint while a move on another holds it. These cases drive the production lock
and the move/recovery pair on a temporary docs tree, and ask a separate
process for the cross-node primitive while the lock is held: a POSIX record
lock, refused here, is the one GPFS holds across nodes.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from reckon import project_state

PROJECT = "sample"
SOURCE = "current"
TARGET = "earlier"


def _write_plan(docs: Path, slug: str) -> None:
    from reckon import _plan_html

    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": slug,
                "title": slug.title(),
                "status": "active",
                "impl": 0.0,
            },
        ),
        encoding="utf-8",
    )


def _docs_tree(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    _write_plan(docs, "alpha")
    project_state.create_project_state(docs, PROJECT)
    project_state.write_resource(
        docs,
        PROJECT,
        "sprint",
        SOURCE,
        {"theme": "Current", "items": [{"slug": "alpha"}]},
        0,
        create=True,
    )
    project_state.write_resource(
        docs,
        PROJECT,
        "sprint",
        TARGET,
        {"theme": "Earlier", "items": []},
        0,
        create=True,
    )
    return docs


def _lock_file(docs: Path, resource_id: str) -> Path:
    return docs / ".reckon" / "locks" / f"{PROJECT}-sprint-{resource_id}.lock"


def _posix_lock_attempt(lock_path: Path) -> int:
    """Ask a separate process for the cross-node lock, without waiting."""
    script = (
        "import fcntl,sys\n"
        "handle=open(sys.argv[1],'r+b')\n"
        "try:\n"
        "    fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    raise SystemExit(3)\n"
        "raise SystemExit(0)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script, str(lock_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).returncode


def _sprint_items(docs: Path, resource_id: str) -> list[str]:
    data, _ = project_state.read_resource(docs, PROJECT, "sprint", resource_id)
    return [item if isinstance(item, str) else item["slug"] for item in data["items"]]


def test_two_threads_exclude_each_other_and_the_lock_crosses_nodes(tmp_path):
    docs = _docs_tree(tmp_path)
    lock_path = _lock_file(docs, SOURCE)
    entered: list[str] = []
    overlap: list[bool] = []
    active = [0]
    cross_node: list[int] = []

    def body(label: str, delay: float) -> None:
        with project_state._resource_lock(docs, PROJECT, "sprint", SOURCE):
            active[0] += 1
            overlap.append(active[0] > 1)
            entered.append(label)
            if label == "first":
                # While the first holder is inside, another process must not be
                # able to take the cross-node primitive on the same file.
                assert lock_path.exists(), "the holder published no lock file"
                cross_node.append(_posix_lock_attempt(lock_path))
            active[0] -= 1
            time.sleep(delay)

    first = threading.Thread(target=body, args=("first", 0.4))
    first.start()
    time.sleep(0.1)
    second = threading.Thread(target=body, args=("second", 0.0))
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)

    assert not first.is_alive() and not second.is_alive()
    assert entered == ["first", "second"]
    assert overlap == [False, False]
    assert cross_node and cross_node[0] != 0, "another process took the lock"


def test_recovery_never_restores_bytes_a_move_holds(tmp_path):
    docs = _docs_tree(tmp_path)
    source_path = project_state.resource_path(docs, PROJECT, "sprint", SOURCE)
    target_path = project_state.resource_path(docs, PROJECT, "sprint", TARGET)
    source_before = source_path.read_bytes()
    target_before = target_path.read_bytes()
    _, from_version = project_state.read_resource(docs, PROJECT, "sprint", SOURCE)
    _, to_version = project_state.read_resource(docs, PROJECT, "sprint", TARGET)
    observed: dict[str, object] = {}
    recovered: list[Path] = []
    returned_at: list[float] = []

    def recovery() -> None:
        recovered.extend(
            project_state.recover_project_state_transactions(docs, PROJECT)
        )
        returned_at.append(time.monotonic())

    def while_held() -> None:
        # The move holds both sprint locks here, after the journal and the
        # first write. Another node asking for that lock must be refused.
        observed["source"] = source_path.read_bytes()
        observed["target"] = target_path.read_bytes()
        observed["cross_node_refused"] = (
            _posix_lock_attempt(_lock_file(docs, SOURCE)) != 0
        )
        worker = threading.Thread(target=recovery, daemon=True)
        worker.start()
        observed["worker"] = worker
        time.sleep(0.3)
        assert worker.is_alive(), "recovery did not wait for the held move"

    project_state.move_sprint_item(
        docs,
        PROJECT,
        "alpha",
        SOURCE,
        TARGET,
        from_version,
        to_version,
        after_first_write=while_held,
    )
    worker = observed["worker"]
    assert isinstance(worker, threading.Thread)
    worker.join(timeout=30)
    assert not worker.is_alive()

    assert observed["cross_node_refused"] is True
    assert returned_at and returned_at[0] > 0
    assert recovered == [], f"recovery restored a locked move: {recovered}"
    assert _sprint_items(docs, SOURCE) == []
    assert _sprint_items(docs, TARGET) == ["alpha"]
    assert source_path.read_bytes() != source_before
    assert target_path.read_bytes() != target_before
