"""Restore a waiting request when its launch refuses."""

from __future__ import annotations

import json

from ._imports import (
    ATTEMPT_RECORD_NAME,
    Path,
    _release_launch_claim,
    _write_json,
    contextlib,
    pointer_path,
    run_dir,
)

_FACADE_BINDINGS = ()


@contextlib.contextmanager
def _request_claim_restored_on_refusal(run_id, request):
    """Return an admitted request to its waiting phase without an absent pointer.

    A dispatch that held its own request stored it again before raising, under
    the same run id and with the reason the hold gave: that copy is newer than
    the waiting request saved here, and restoring over it would put back a hold
    the lane has already replaced, which is what a requeued hold must not do.
    The saved request is written back only where the pointer still carries this
    launch, so a refusal keeps the request and a hold keeps the hold.
    """
    try:
        yield
    except Exception:
        from reckon.crew.promotion import discard_recorded

        if discard_recorded(run_id):
            _release_launch_claim(run_id)
        elif not _pointer_already_waits(run_id):
            # The starting claim published the attempt record beside the claim.
            # The request is going back to the queue rather than being launched,
            # so the record it published describes an attempt that never
            # happened: kept, the restored request would read afterwards as a
            # launch that stopped after its claim. The claim itself stays,
            # exactly as it did before the record was published with it.
            _attempt_record_for(run_id).unlink(missing_ok=True)
            _write_json(pointer_path(run_id), request)
        raise


def _pointer_already_waits(run_id: str) -> bool:
    """Whether the run's pointer already carries a stored waiting request."""
    try:
        stored = json.loads(pointer_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return str(stored.get("phase") or "") == "queued"


def _attempt_record_for(run_id: str) -> Path:
    return run_dir(run_id) / ATTEMPT_RECORD_NAME
