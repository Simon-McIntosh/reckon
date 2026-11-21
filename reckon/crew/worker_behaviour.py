"""One statement of a worker's treatment, keyed on its lane and its role.

A worker's treatment — whether a message reaches it, which servers it may
reach, which paths it may write, whether its delivery owes a review, and how
its work is landed — is the same for every backend of one lane. Only four
things differ between the harnesses serving a lane, because the harness's own
interface forces them: the launch argv, the layout of the harness home, the
format of the stream a run writes, and the model identifier.

This module states that treatment once and returns it as comparable facts. A
statement keyed on a backend's name is the defect the facts exist to catch:
two names in front of one model server are one lane, and moving a lane from one
name to the other must move none of these facts. The harness's interface is
carried separately, as a route name, so reading it can never be mistaken for
deciding a worker's treatment by it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# The two roles a launch composes for. A role decides whether delivery owes a
# review, never which backend runs it.
IMPLEMENT_ROLE = "implement"
REVIEW_ROLE = "review"

# How a queued message reaches the running worker. The message itself is always
# written into the run directory's inbox; the trigger is the harness's own
# interface, which is why it is recorded rather than compared.
INBOX_RUN_HOME_HOOK = "run-home-hook"
INBOX_ARGV_OVERRIDE = "argv-override"
INBOX_FILE_ONLY = "file-only"


@dataclass(frozen=True)
class WorkerBehaviour:
    """The treatment a launch gives one worker, stated as facts to compare."""

    lane: str
    role: str
    inbox_directory: str
    inbox_trigger: str
    mcp_server_allowlist: bool
    fence_protections: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    review_duty: bool = False
    landing_treatment: str = "promoted-under-its-lane"
    unmergeable_seals: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def dimensions(self) -> dict[str, Any]:
        """Return the facts that must be equal across the backends of a lane.

        The harness interface is deliberately absent: ``inbox_trigger`` is the
        route a message takes and the launch argv and home layout are where it
        is spelled, all of which the four permitted differences cover.
        """
        return {
            "lane": self.lane,
            # The role is a fact of the launch rather than of the lane: it is
            # equal across a lane's backends for one node, and a reader of the
            # recorded treatment needs it to know what the launch owes. Left
            # out, a review launch records no role and reads as an implement
            # one from the duty and landing facts alone.
            "role": self.role,
            "inbox_directory": self.inbox_directory,
            "mcp_server_allowlist": self.mcp_server_allowlist,
            "fence_protections": self.fence_protections,
            "write_roots": self.write_roots,
            "review_duty": self.review_duty,
            "landing_treatment": self.landing_treatment,
            "unmergeable_seals": self.unmergeable_seals,
        }


def lane_identity(
    backend_name: str,
    backend: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Return the lane a backend serves, not the name it happens to carry.

    A backend that declares a ``lane_document`` is serving the lane that
    document describes, so every backend publishing the same document resolves
    to one identity however it is named. A backend declaring none is its own
    lane, named after itself, because nothing declares it shares one.
    """
    declared = ""
    if isinstance(backend, Mapping):
        declared = str(backend.get("lane_document") or "").strip()
    if not declared and isinstance(config, Mapping):
        backends = config.get("backends")
        settings = backends.get(backend_name) if isinstance(backends, Mapping) else None
        if isinstance(settings, Mapping):
            declared = str(settings.get("lane_document") or "").strip()
    if not declared:
        return backend_name
    return os.path.realpath(os.path.expanduser(declared))


def inbox_trigger_for(dialect_name: str, settings_file: str | None = None) -> str:
    """Return the route a queued message takes into a running worker.

    A harness whose interface reads a settings group takes the hook that reads
    the inbox on its next tool call; codex carries the same hook as a pair of
    argv overrides; a harness that declares neither is reached by the message
    written into the run directory, read on its next resume. All three routes
    carry the same message to the same directory: the route is the harness's
    interface, the delivery is the inbox.
    """
    if settings_file:
        return INBOX_RUN_HOME_HOOK
    if dialect_name == "codex":
        return INBOX_ARGV_OVERRIDE
    return INBOX_FILE_ONLY


def compose_worker_behaviour(
    *,
    backend_name: str,
    backend: Mapping[str, Any] | None,
    run_directory: str | os.PathLike[str] | None,
    role: str = IMPLEMENT_ROLE,
    fence: bool = True,
    write_roots: Iterable[str | os.PathLike[str]] = (),
    unmergeable_seals: Mapping[str, str] | None = None,
    dialect_name: str = "",
    settings_file: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> WorkerBehaviour:
    """State once the treatment every backend of a lane gives its worker.

    Nothing here is decided from ``backend_name``: the lane comes from the lane
    document the backend declares, the write roots from the node's own scope,
    and the duty from the role. A caller that wanted a name-keyed difference
    has to add it outside this function, which is what makes the difference
    visible when the facts across two backends are compared.
    """
    directory = "" if run_directory is None else str(run_directory)
    roots = tuple(sorted({os.fspath(root) for root in write_roots})) if fence else ()
    protections = roots if fence else ()
    seals = tuple(sorted((unmergeable_seals or {}).items()))
    return WorkerBehaviour(
        lane=lane_identity(backend_name, backend, config),
        role=role,
        # The inbox lives in the run directory every backend is always granted,
        # so the same node on two harnesses queues its message in one place.
        inbox_directory="" if not directory else os.path.join(directory, "inbox"),
        inbox_trigger=inbox_trigger_for(dialect_name, settings_file),
        # Every backend restricts the harness to the flight-declared servers:
        # the claude-shaped homes by their JSON configuration and codex on its
        # argv. The interface differs; the allowlist does not.
        mcp_server_allowlist=True,
        fence_protections=protections,
        write_roots=roots,
        review_duty=role == IMPLEMENT_ROLE,
        landing_treatment=(
            "reviewed-under-its-lane"
            if role == REVIEW_ROLE
            else "promoted-under-its-lane"
        ),
        unmergeable_seals=seals,
    )


def behave_identically(first: WorkerBehaviour, second: WorkerBehaviour) -> list[str]:
    """Return the dimension names on which two backends' treatments differ.

    An empty list is the claim the lane makes: the same node on two harnesses
    of one lane is given the same treatment.
    """
    left, right = first.dimensions(), second.dimensions()
    return sorted(name for name in left if left[name] != right.get(name))


__all__ = [
    "IMPLEMENT_ROLE",
    "INBOX_ARGV_OVERRIDE",
    "INBOX_FILE_ONLY",
    "INBOX_RUN_HOME_HOOK",
    "REVIEW_ROLE",
    "WorkerBehaviour",
    "behave_identically",
    "compose_worker_behaviour",
    "inbox_trigger_for",
    "lane_identity",
]
