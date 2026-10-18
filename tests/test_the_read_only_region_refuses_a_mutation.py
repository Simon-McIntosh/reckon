"""A document the read-only region hands out refuses mutation.

:func:`reckon.file_memo.read_only_region` shares the memo's stored value with
its callers instead of copying it, because the copy is what a reader pays per
hand-out and a reader that changes nothing gains nothing from it. The read-only
contract that buys the saving was then a convention no test checked: a caller
that mutated a document it read inside the region would corrupt the
process-wide memo for every later reader, and no guard stopped it.

The guard is the region handing out values on which a mutation raises, rather
than a test running the derivation over frozen values. The region is where a
future caller meets the document, so the refusal protects every caller rather
than only the ones a test reaches, and it protects the memo rather than
reporting its corruption after the fact. A read-only view is the one added cost
and it is built once per document per region, so a caller that mutates must
copy first: ``copy.copy`` and ``copy.deepcopy`` of a view return plain, fully
mutable containers, so copying stays the way to a mutable value.

The cases below show the guard both fires and does not: a caller that mutates a
handed-out document is refused, at the top level and inside a container nested
in a dict or a tuple; a caller that copies one changes the copy without
touching the memo; and one ``obligation_snapshot.fleet_state`` derivation run
inside the region completes without a detection, so no production caller on
that path mutates what it is handed.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon import file_memo, ledger
from reckon.crew import obligation_snapshot
from tests.helpers.git import git as _git

PROJECT = "read-only-region-fixture"
SESSION = "read-only-region"
NODE = "node-00"
COMPLETED = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
REFUSAL = "read-only"


def test_a_mutation_of_a_document_the_region_hands_out_is_refused(
    tmp_path: Path,
) -> None:
    """A caller that mutates a document inside the region is stopped.

    The document is read through :func:`reckon.file_memo.memoized` inside a
    region, so what the caller holds is the view the region shares. A change at
    the top level and a change to a container nested inside it both raise, and
    the reads around them are unchanged — so the document is a reading of the
    memo rather than a window onto it.
    """
    path = tmp_path / "document.json"
    payload = {"name": "note", "top": {"leaf": [1, 2]}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    file_memo.clear()
    with file_memo.read_only_region():
        document = file_memo.memoized(
            "read-only-document",
            path,
            lambda: json.loads(path.read_text(encoding="utf-8")),
        )
        assert document["top"]["leaf"] == [1, 2]
        with pytest.raises(TypeError, match=REFUSAL):
            document["top"] = {}
        with pytest.raises(TypeError, match=REFUSAL):
            document["name"] = "changed"
        with pytest.raises(TypeError, match=REFUSAL):
            document["top"]["leaf"].append(3)
        with pytest.raises(TypeError, match=REFUSAL):
            document["top"]["leaf"][0] = 9
        with pytest.raises(TypeError, match=REFUSAL):
            document["top"]["leaf"].sort()
        # Every refusal left the document readable and unchanged.
        assert document["top"]["leaf"] == [1, 2]
    # The memo the region shares from is not poisoned by the refusals: a reader
    # outside the region still gets the document it always did.
    outside = file_memo.memoized(
        "read-only-document",
        path,
        lambda: json.loads(path.read_text(encoding="utf-8")),
    )
    assert outside == payload


def test_outside_a_region_the_memo_still_copies(tmp_path: Path) -> None:
    """The guard is the region's, not the memo's.

    A caller outside a region gets an ordinary mutable value, so no reader that
    must change what it reads is affected by a region it is not in.
    """
    path = tmp_path / "document.json"
    path.write_text(json.dumps({"leaf": [1]}), encoding="utf-8")
    file_memo.clear()
    value = file_memo.memoized(
        "outside-document",
        path,
        lambda: json.loads(path.read_text(encoding="utf-8")),
    )
    assert type(value) is dict
    value["leaf"].append(2)
    assert value == {"leaf": [1, 2]}


def test_a_copy_of_a_document_the_region_hands_out_is_mutable(
    tmp_path: Path,
) -> None:
    """Copying a handed-out document yields a mutable value, and the memo holds.

    A caller that lets what it reads escape the region must be able to change it
    afterwards, so the views copy to plain, fully mutable containers rather than
    propagating the refusal. The copy is changed at the top level and inside a
    nested container, and the document the region still hands out — and the memo
    outside it — are unchanged.
    """
    path = tmp_path / "document.json"
    payload = {"name": "note", "top": {"leaf": [1, 2]}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    file_memo.clear()
    with file_memo.read_only_region():
        document = file_memo.memoized(
            "copied-document",
            path,
            lambda: json.loads(path.read_text(encoding="utf-8")),
        )
        shallow = copy.copy(document)
        assert type(shallow) is dict
        deep = copy.deepcopy(document)
        assert type(deep) is dict
        assert type(deep["top"]) is dict
        assert type(deep["top"]["leaf"]) is list
        deep["name"] = "changed"
        deep["top"]["leaf"].append(3)
        assert deep == {"name": "changed", "top": {"leaf": [1, 2, 3]}}
    memo = file_memo.memoized(
        "copied-document",
        path,
        lambda: json.loads(path.read_text(encoding="utf-8")),
    )
    assert memo == payload


def test_a_dict_inside_a_handed_out_tuple_refuses_mutation(
    tmp_path: Path,
) -> None:
    """A container a handed-out tuple holds refuses a change too.

    A tuple is immutable in itself, so wrapping one would add nothing unless the
    mutable members it holds are wrapped as well; a plan read memoises a mapping
    beside a string inside a tuple, so a tuple whose members went unwrapped would
    hand that mapping out mutable.
    """
    path = tmp_path / "document.json"
    path.write_text('{"held": [{"leaf": 1}]}', encoding="utf-8")
    file_memo.clear()
    with file_memo.read_only_region():
        document = file_memo.memoized(
            "tuple-document",
            path,
            lambda: (json.loads(path.read_text(encoding="utf-8")), "text"),
            variant="tuple",
        )
        held, text = document
        assert text == "text"
        assert isinstance(held, dict)
        with pytest.raises(TypeError, match=REFUSAL):
            held["leaf"] = 2
        with pytest.raises(TypeError, match=REFUSAL):
            held["held"].append({"leaf": 2})
        assert held == {"held": [{"leaf": 1}]}
    assert file_memo.memoized(
        "tuple-document",
        path,
        lambda: (json.loads(path.read_text(encoding="utf-8")), "text"),
        variant="tuple",
    ) == ({"held": [{"leaf": 1}]}, "text")


def _fleet_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A synthesised home holding one run that retains one inspected tree.

    The held-worktree derivation reads a run document only when a registered,
    unoccupied tree is inspected, so this registers one tree and writes one run
    record that retains it — the least that makes the region hand a document
    out. The environment is named for the call and left in place, because the
    fixtures that use it read through those variables.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config_home / "mounts.json"))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the read-only-region fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    tree = root.parent / "managed-worktrees" / SESSION / NODE
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    run_id = f"r-20261016T100000-{NODE}"
    record = ledger.build_record(
        run_id=run_id,
        plan="fixture-plan",
        gate="passed",
        node=NODE,
        session=SESSION,
        completed_at=COMPLETED.isoformat(),
    )
    record["worktree_retention"] = {
        "worktree": str(tree),
        "retained_at": COMPLETED.isoformat(),
    }
    ledger.append_run(PROJECT, record, root=root, allow_create=True)
    return {"root": root, "tree": tree, "run_id": run_id}


def test_a_fleet_state_derivation_inside_the_region_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One fleet_state derivation reads a shared document without a detection.

    The derivation runs inside a read-only region under the same guard the
    region always applies, and completes. The documents the region handed out
    are counted, so a pass that never reached the guard cannot stand in for one
    that did: the derivation names the inspected tree, which it cannot do
    without reading the run document through the region.
    """
    fleet = _fleet_home(tmp_path, monkeypatch)
    handed_out: list[Any] = []
    real_read_only = file_memo._read_only

    def counting(value: Any) -> Any:
        handed_out.append(value)
        return real_read_only(value)

    monkeypatch.setattr(file_memo, "_read_only", counting)
    with file_memo.read_only_region():
        state = obligation_snapshot.fleet_state(PROJECT)

    assert handed_out, "the region handed out no document; the guard was unexercised"
    duties = {
        str(duty.get("run_id") or "") for duty in state.held_worktrees.get(SESSION, [])
    }
    assert duties == {fleet["run_id"]}
