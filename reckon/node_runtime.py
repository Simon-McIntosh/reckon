"""Node-local runtime paths, stated once for every reader that resolves them.

The prompt hook loads :mod:`reckon.crew.obligation_snapshot` by file path and
must not import the crew package, whose facade loads every derivation module,
so the candidate order the station and the session host resolve cannot be
reached through ``reckon.crew`` from there. Stating it here — standard library
only, no package import — is what lets the station's store, the dispatch that
asks a session host, and the hook's reader all resolve one order instead of a
copy in each. The file name the station's database is stored under is restated
beside it for the same reason: a reader that repeats a constant drifts from the
writer silently, and nothing fails when it does.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

#: The file name the station's store keeps its database under.
STATION_DATABASE_NAME = "station.sqlite3"


def runtime_root_candidate(
    *,
    environ: Mapping[str, str] | None = None,
    run_user_root: str | Path | None = None,
) -> tuple[Path | None, str]:
    """The node-local runtime root and which candidate produced it.

    Three candidates, in the order that keeps a FIFO and a database off shared
    storage: the session's own runtime directory, then the per-user directory a
    login node provides, then the scratch root. The source is returned beside
    the root because only the first two are shared by every process on the node
    — ``TMPDIR`` differs per session.

    ``environ`` and ``run_user_root`` are named so a caller can feed fixture
    values, exactly as ``reckon.host.host_facts`` does; the defaults are the
    process's own environment and the node's per-user runtime directory.
    """
    env = os.environ if environ is None else environ
    runtime = str(env.get("XDG_RUNTIME_DIR") or "").strip()
    if runtime:
        return Path(runtime), "runtime-directory"
    run_user = (
        Path(run_user_root)
        if run_user_root is not None
        else Path(f"/run/user/{os.getuid()}")
    )
    if run_user.is_dir():
        return run_user, "per-user-runtime"
    scratch = str(env.get("TMPDIR") or "").strip()
    if scratch:
        return Path(scratch), "scratch"
    return None, "none"
