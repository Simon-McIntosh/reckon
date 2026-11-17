"""Scratch sealing, write grants and tmp mounts share one membership verdict."""

import pytest

from reckon import _worker_fence as fence


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    node = tmp_path / "node"
    root = node / "scratch"
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(root))
    return node, root


@pytest.mark.parametrize(
    "relative, sealed, writable",
    [
        ("scratch", True, False),
        ("scratch/own", True, True),
        ("scratch/own/nested", True, True),
        ("scratch-neighbour/own", False, False),
        ("elsewhere", False, False),
        (".", False, False),
    ],
)
def test_membership_for_absent_string_and_path_roots(
    scratch, relative, sealed, writable
):
    node, root = scratch
    handed = node / relative
    assert not root.exists()
    for spelling in (handed, str(handed)):
        assert fence.worker_scratch_seal_root(iter([spelling])) == (
            root if sealed else None
        )
        assert fence.worker_scratch_write_roots(iter([spelling])) == (
            {handed} if writable else set()
        )
        assert fence.worker_tmp_mount_source(iter([spelling]), node_tmp=node) == (
            handed if writable else None
        )
    assert not root.exists(), "membership queries must not create scratch directories"


def test_symlink_ancestry_resolves_and_the_shallowest_root_is_mounted(
    scratch, tmp_path
):
    node, root = scratch
    root.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(node, target_is_directory=True)
    own = root / "own"
    nested = own / "nested"
    handed = [alias / "scratch" / "own" / "nested", str(own), own]
    assert fence.worker_scratch_seal_root(iter(handed)) == root
    assert fence.worker_scratch_write_roots(iter(handed)) == {own, nested}
    assert fence.worker_tmp_mount_source(iter(handed), node_tmp=alias) == own


def test_a_symlinked_scratch_root_keeps_its_seal_spelling(
    scratch, tmp_path, monkeypatch
):
    node, root = scratch
    root.mkdir(parents=True)
    alias = tmp_path / "scratch-alias"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(alias))
    own = root / "own"
    assert fence.worker_scratch_seal_root([alias / "own"]) == alias
    assert fence.worker_scratch_write_roots([alias / "own"]) == {own}
    assert fence.worker_tmp_mount_source([alias / "own"], node_tmp=node) == own


def test_empty_roots_and_scratch_outside_the_node_compose_no_mount(scratch, tmp_path):
    node, root = scratch
    assert fence.worker_scratch_seal_root([]) is None
    assert fence.worker_scratch_write_roots([]) == set()
    assert fence.worker_tmp_mount_source([], node_tmp=node) is None
    own = root / "own"
    for node_tmp in (None, root, tmp_path / "elsewhere"):
        assert fence.worker_tmp_mount_source([own], node_tmp=node_tmp) is None


def test_one_membership_verdict_reaches_every_caller(scratch, monkeypatch):
    node, root = scratch
    chosen, other = root / "chosen", root / "other"
    assert fence.worker_scratch_seal_root([chosen]) == root
    assert fence.worker_scratch_write_roots([chosen]) == {chosen}
    assert fence.worker_tmp_mount_source([chosen], node_tmp=node) == chosen
    membership = fence._worker_scratch_roots

    def reject_chosen(roots):
        scratch_root, members = membership(roots)
        return scratch_root, [path for path in members if path != chosen]

    monkeypatch.setattr(fence, "_worker_scratch_roots", reject_chosen)
    assert fence.worker_scratch_write_roots([chosen]) == set()
    assert fence.worker_scratch_seal_root([chosen]) is None
    assert fence.worker_tmp_mount_source([chosen], node_tmp=node) is None
    assert fence.worker_scratch_seal_root([other]) == root
    assert fence.worker_scratch_write_roots([other]) == {other}
    assert fence.worker_tmp_mount_source([other], node_tmp=node) == other


def test_equally_shallow_mount_candidates_keep_handed_order(scratch):
    node, root = scratch
    first, second = root / "first", root / "second"
    for roots in ([first, second], [second, first]):
        assert fence.worker_tmp_mount_source(iter(roots), node_tmp=node) == roots[0]
