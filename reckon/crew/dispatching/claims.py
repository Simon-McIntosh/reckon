"""Restore a waiting request when its launch refuses."""

from __future__ import annotations

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
    """Return an admitted request to its waiting phase without an absent pointer."""
    try:
        yield
    except Exception:
        from reckon.crew.promotion import discard_recorded

        if discard_recorded(run_id):
            _release_launch_claim(run_id)
        else:
            # The starting claim published the attempt record beside the claim.
            # The request is going back to the queue rather than being launched,
            # so the record it published describes an attempt that never
            # happened: kept, the restored request would read afterwards as a
            # launch that stopped after its claim. The claim itself stays,
            # exactly as it did before the record was published with it.
            _attempt_record_for(run_id).unlink(missing_ok=True)
            _write_json(pointer_path(run_id), request)
        raise


def _attempt_record_for(run_id: str) -> Path:
    return run_dir(run_id) / ATTEMPT_RECORD_NAME
