"""Every Python module under ``reckon/`` and ``tests/`` fits a worker window.

The context-fit check counts each write-path file whole at 3.5 bytes per token
on top of a launch-context floor, so a module that fills most of a window makes
a node writing it unfittable. This ratchet holds every module at or under
50,000 estimated tokens -- a quarter of a 200k-token window -- using the
context-fit check's own estimate rather than a second copy of the ratio.

``ALLOWLIST`` names the modules the split plan has scheduled and is a ratchet:
an entry is removed in the same change that brings the module under the bound.
A file over the bound that is not allowlisted, and an allowlisted file that has
shrunk to or under the bound, both read red, so a new oversize module cannot
appear and a completed split cannot leave its entry behind.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew.routing import _tokens_for_bytes

ROOT = Path(__file__).resolve().parents[1]

# A quarter of a 200k-token window leaves room for multiple module inputs.
BOUND = 50_000

AREAS = ("reckon", "tests")

# Modules above the bound when the bound was set, each scheduled for a split.
ALLOWLIST = frozenset(
    {
        "reckon/crew/runs.py",
        "reckon/mcp.py",
    }
)


def _module_sizes(root: Path) -> dict[str, int]:
    """The estimated tokens of every ``*.py`` under ``root``'s two areas."""

    sizes: dict[str, int] = {}
    for area in AREAS:
        for path in sorted((root / area).rglob("*.py")):
            sizes[path.relative_to(root).as_posix()] = _tokens_for_bytes(
                path.stat().st_size
            )
    return sizes


def _violations(
    sizes: dict[str, int],
    allowlist: frozenset[str] = ALLOWLIST,
    bound: int = BOUND,
) -> list[str]:
    """One message per module that breaks the bound or the ratchet."""

    failures: list[str] = []
    for path, tokens in sorted(sizes.items()):
        if tokens > bound and path not in allowlist:
            failures.append(
                f"{path}: {tokens} estimated tokens exceeds the {bound} bound "
                "and is not allowlisted"
            )
        elif tokens <= bound and path in allowlist:
            failures.append(
                f"{path}: {tokens} estimated tokens is at or under the {bound} "
                "bound; remove it from the allowlist"
            )
    return failures


def test_every_module_fits_a_worker_window():
    failures = _violations(_module_sizes(ROOT))
    assert not failures, "\n".join(failures)


def _write_fixture(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("value = 1  # " + "a" * size + "\n", encoding="utf-8")


def test_a_fixture_tree_under_the_bound_passes(tmp_path: Path):
    _write_fixture(tmp_path / "reckon" / "small.py", 10)
    assert _violations(_module_sizes(tmp_path)) == []


def test_an_unlisted_module_over_the_bound_turns_the_guard_red(tmp_path: Path):
    _write_fixture(tmp_path / "reckon" / "grew.py", int(BOUND * 3.5) + 1)
    failures = _violations(_module_sizes(tmp_path))
    assert len(failures) == 1
    assert "reckon/grew.py" in failures[0]
    assert "not allowlisted" in failures[0]


def test_an_allowlisted_module_under_the_bound_turns_the_guard_red(tmp_path: Path):
    _write_fixture(tmp_path / "reckon" / "shrunk.py", 10)
    failures = _violations(
        _module_sizes(tmp_path),
        allowlist=frozenset({"reckon/shrunk.py"}),
    )
    assert len(failures) == 1
    assert "reckon/shrunk.py" in failures[0]
    assert "remove it from the allowlist" in failures[0]
