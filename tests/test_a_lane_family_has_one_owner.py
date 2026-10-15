"""One owner for a backend's lane family, and the zero-slot re-ask's recorded pair.

A behaviour-preserving consolidation: the rule mapping a configured backend to
its lane family was stated three times -- in the picker state, in the dispatch
exclusion and in the review reflex -- so a change to it had to be made in each
place. :func:`reckon.crew.dispatch_picker.lane_family` is now the one owner and
the reflex and the state call it. The reflex's retry and the dispatch re-ask are
the same second ask with the refusing lane excluded, so they share
:func:`reckon.crew.dispatch_picker._ask_excluding_lane` and each keeps its own
trigger.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew.dispatch_picker import lane_family

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OWNER = _REPO_ROOT / "reckon" / "crew" / "dispatch_picker.py"
_CALLERS = (
    _REPO_ROOT / "reckon" / "crew" / "recovery_review_dispatch.py",
    _REPO_ROOT / "reckon" / "crew" / "picker" / "snapshot.py",
)

# A backend named as the configuration's local backend, whatever its command
# would otherwise translate to.
_LOCAL_CONFIG = {
    "local_backend": "local",
    "backends": {"local": {"launch": "cli", "command": "clive"}},
}
# A backend whose command resolves to a known dialect.
_DIALECT_CONFIG = {
    "local_backend": "local",
    "backends": {"paid": {"command": "claude"}},
}
# A backend whose command names no known dialect, so its launch kind is the family.
_NO_DIALECT_CONFIG = {
    "local_backend": "local",
    "backends": {"odd": {"command": "mystery-agent", "launch": "cli"}},
}


def test_the_local_backend_maps_to_the_local_marker():
    """The configured local backend is the local family, not its command's dialect."""
    assert lane_family(_LOCAL_CONFIG, "local") == "local"


def test_a_dialect_backend_maps_to_its_dialect():
    """A backend whose command resolves to a dialect takes that dialect's name."""
    assert lane_family(_DIALECT_CONFIG, "paid") == "claude"


def test_a_backend_without_a_dialect_maps_to_its_launch_kind():
    """A command that names no dialect falls back to the backend's launch kind."""
    assert lane_family(_NO_DIALECT_CONFIG, "odd") == "cli"


def test_an_unknown_backend_names_itself():
    """A name with no configured backend is its own family rather than an error."""
    assert lane_family(_DIALECT_CONFIG, "absent") == "absent"


def test_the_owner_is_the_only_lane_family_derivation_among_the_consolidated_modules():
    """Neither former duplicator still derives a lane family from ``dialect_for``.

    The owner is pinned present, so a scan that finds nothing cannot read as a
    pass: the same derivation must exist once, in the owner, and nowhere in the
    modules the owner replaced.
    """
    assert "dialect_for(" in _OWNER.read_text(encoding="utf-8")
    for path in _CALLERS:
        assert "dialect_for(" not in path.read_text(encoding="utf-8"), (
            f"{path} derives a lane family instead of calling the owner"
        )


def test_the_reflex_and_the_state_call_the_owner():
    """Both former duplicators call the one owner by name."""
    for path in _CALLERS:
        assert "lane_family(" in path.read_text(encoding="utf-8"), (
            f"{path} does not call the lane-family owner"
        )
