"""Paste the terminal client's clipboard onto this host and onto the live fleet.

The clipboard lives on the machine the terminal runs on, not on the host the
shell reached. A bridge on the client answers HTTP on a fixed port, and the ssh
tunnel reverse-forwards that port to the login nodes, so ``localhost:<port>`` on
a login node reaches it: ``GET /health`` answers when the bridge is up,
``GET /paste`` returns the clipboard, and ``POST /copy`` puts text back on it. A
reverse forward binds only on the node its ssh session reached, so a shell on a
login node the tunnel did not reach has no bridge at all.

An image is written to ``/tmp`` on this host and its path printed, so it can be
handed to an agent by path. ``/tmp`` is node-local, and agent sessions run
inside the fleet allocation on a compute node, where the login node's ``/tmp``
does not exist. So when a fleet is live the same bytes are also written at the
same path on the fleet node, and one printed path is valid on both.

Where the fleet lives is never named here. The fleet supervisor publishes its
job id to a record (:mod:`reckon.crew.fleet_supervisor`); the scheduler says
whether that job is running and on which node; and the copy runs as an
``--overlap`` step inside the job, so it sees exactly the ``/tmp`` the fleet's
sessions see. A record whose job is not running is a fleet that is gone, and the
paste stays on this host.

Replication is best effort: the image on this host is the paste, and a failed
copy to the fleet node is reported without failing it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from reckon.crew.fleet_supervisor import RECORD_NAME, state_directory

PORT_ENV = "WSL_CLIP_PORT"
DEFAULT_PORT = 2490

HEALTH_TIMEOUT_SECONDS = 2.0
PASTE_TIMEOUT_SECONDS = 12.0
SCHEDULER_TIMEOUT_SECONDS = 10.0
COPY_TIMEOUT_SECONDS = 60.0

PASTE_DIRECTORY = Path("/tmp")  # noqa: S108 - the path an agent is handed
PASTE_PREFIX = "paste-"

# Written on the fleet node with noclobber, so a name that already exists there
# is refused rather than overwritten, and readable by the owner only, as
# ``mkstemp`` makes the local copy.
_REMOTE_WRITE = 'set -C; umask 077; cat > "$1"'

# Leading bytes of each image format the clipboard bridge hands back, in the
# order they are tested. WEBP is a RIFF container, so its tag sits at offset 8.
_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"\xff\xd8\xff", "jpeg"),
    (0, b"GIF87a", "gif"),
    (0, b"GIF89a", "gif"),
    (8, b"WEBP", "webp"),
    (0, b"BM", "bmp"),
)

Runner = Callable[..., subprocess.CompletedProcess]


def bridge_port(environ: Mapping[str, str] | None = None) -> int:
    """The port the clipboard bridge is forwarded to on this host."""
    environ = os.environ if environ is None else environ
    value = str(environ.get(PORT_ENV, "")).strip()
    return int(value) if value.isdigit() else DEFAULT_PORT


def _opener() -> urllib.request.OpenerDirector:
    # The bridge is on loopback. A site proxy in the environment must not be
    # asked for it, and a proxy that answers would read as a bridge that works.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _bridge_url(port: int, route: str) -> str:
    return f"http://localhost:{port}/{route}"


def bridge_healthy(port: int) -> bool:
    """Whether the bridge answers through the forward on this host."""
    try:
        with _opener().open(
            _bridge_url(port, "health"), timeout=HEALTH_TIMEOUT_SECONDS
        ) as reply:
            return 200 <= reply.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def fetch_clipboard(port: int) -> bytes:
    """The clipboard's current contents, image or text, as bytes."""
    with _opener().open(
        _bridge_url(port, "paste"), timeout=PASTE_TIMEOUT_SECONDS
    ) as reply:
        return reply.read()


def copy_to_clipboard(port: int, text: str) -> bool:
    """Put ``text`` on the client's clipboard; false when the bridge refuses."""
    request = urllib.request.Request(  # noqa: S310 - fixed http scheme on loopback
        _bridge_url(port, "copy"), data=text.encode("utf-8"), method="POST"
    )
    try:
        with _opener().open(request, timeout=HEALTH_TIMEOUT_SECONDS) as reply:
            return 200 <= reply.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def image_extension(data: bytes) -> str | None:
    """The file extension for an image payload, or None when it is not one."""
    for offset, signature, extension in _SIGNATURES:
        if data[offset : offset + len(signature)] == signature:
            return extension
    return None


def save_image(data: bytes, extension: str, directory: Path = PASTE_DIRECTORY) -> Path:
    """Write the image under a fresh name in ``directory`` and return its path."""
    handle, name = tempfile.mkstemp(
        prefix=PASTE_PREFIX, suffix=f".{extension}", dir=directory
    )
    with os.fdopen(handle, "wb") as stream:
        stream.write(data)
    return Path(name)


def _forward_bound_here(port: int, run: Runner) -> bool:
    try:
        listing = run(
            ["ss", "-tlnH", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=SCHEDULER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(listing.stdout.strip())


def bridge_diagnosis(port: int, run: Runner = subprocess.run) -> list[str]:
    """Say why the bridge is unreachable here, and what restores it."""
    node = _short_hostname()
    lines = [f"clipboard bridge unreachable on {node}:{port}"]
    if _forward_bound_here(port, run):
        # Bound here but nothing answers: the forward reaches this node and the
        # bridge behind it is down, or the forward itself is stale.
        lines += [
            f"  The reverse forward is bound on {node}, but nothing answers through it.",
            "  Fix, on WSL: systemctl --user restart wsl-clip-server.service",
            (
                "  Still failing (stale forward): "
                "systemctl --user restart imas-codex-tunnel-iter.service"
            ),
        ]
    else:
        lines += [
            f"  No reverse forward is bound on {node}: the WSL tunnel does not reach this node.",
            "  Fix, on WSL (binds on every login node, restarts the tunnel):",
            (
                "    uv run --project ~/Code/imas-codex imas-codex tunnel service install "
                "iter --reverse-node all"
            ),
            "  Already installed that way? The unit is down or restarting:",
            "    systemctl --user status imas-codex-tunnel-iter.service",
        ]
    return lines


@dataclass(frozen=True)
class FleetTarget:
    """A fleet allocation the scheduler reports running, and the node it holds."""

    job_id: str
    node: str


def _short_hostname() -> str:
    return socket.gethostname().split(".")[0]


def _read_record(environ: Mapping[str, str] | None) -> Mapping[str, object] | None:
    path = state_directory(environ) / RECORD_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def live_fleet(
    environ: Mapping[str, str] | None = None, run: Runner = subprocess.run
) -> FleetTarget | None:
    """The fleet the record names, when the scheduler says it is running.

    The record says which job the fleet is; only the scheduler says whether it
    is still alive and where. A record outlives its allocation, so a job that is
    pending, finished or unknown is no fleet.
    """
    record = _read_record(environ)
    if record is None:
        return None
    job_id = str(record.get("job_id") or "").strip()
    if not job_id.isdigit():
        return None
    try:
        reply = run(
            ["squeue", "-h", "-j", job_id, "-o", "%T %N"],
            capture_output=True,
            text=True,
            timeout=SCHEDULER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields = reply.stdout.split()
    if reply.returncode != 0 or len(fields) != 2 or fields[0] != "RUNNING":
        return None
    return FleetTarget(job_id=job_id, node=fields[1].split(".")[0])


def replicate(
    path: Path, target: FleetTarget, run: Runner = subprocess.run
) -> str | None:
    """Write ``path`` at the same path on the fleet node; the error, or None."""
    command = [
        "srun",
        "--overlap",
        f"--jobid={target.job_id}",
        "--ntasks=1",
        "--job-name=reckon-paste",
        "sh",
        "-c",
        _REMOTE_WRITE,
        "sh",
        str(path),
    ]
    try:
        with path.open("rb") as source:
            reply = run(
                command,
                stdin=source,
                capture_output=True,
                text=True,
                timeout=COPY_TIMEOUT_SECONDS,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if reply.returncode != 0:
        detail = (reply.stderr or reply.stdout or "").strip().splitlines()
        return detail[-1] if detail else f"srun exited {reply.returncode}"
    return None


def paste(
    *,
    fleet: bool = True,
    environ: Mapping[str, str] | None = None,
    run: Runner = subprocess.run,
    directory: Path = PASTE_DIRECTORY,
    out=None,
    err=None,
) -> int:
    """Paste the clipboard: print an image's path, or the text itself.

    Returns the process status: 0 when something was pasted, 1 when the bridge
    is unreachable or returned nothing.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    port = bridge_port(environ)

    if not bridge_healthy(port):
        headline, *remedy = bridge_diagnosis(port, run)
        print(f"reckon paste: {headline}", file=err)
        for line in remedy:
            print(line, file=err)
        return 1
    try:
        data = fetch_clipboard(port)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"reckon paste: the bridge did not return the clipboard: {exc}", file=err)
        return 1

    extension = image_extension(data)
    if extension is None:
        # Text: print it as it came, and leave nothing behind.
        out.write(data.decode("utf-8", errors="replace"))
        out.flush()
        return 0

    image = save_image(data, extension, directory)
    print(image, file=out)
    out.flush()

    if fleet:
        target = live_fleet(environ, run)
        if target is not None and target.node != _short_hostname():
            error = replicate(image, target, run)
            if error is None:
                print(
                    f"reckon paste: also on fleet node {target.node} (job {target.job_id})",
                    file=err,
                )
            else:
                print(
                    f"reckon paste: not copied to fleet node {target.node} "
                    f"(job {target.job_id}): {error}",
                    file=err,
                )

    # Put the path back on the client's clipboard so it can be pasted into an
    # agent's prompt directly.
    copy_to_clipboard(port, str(image))
    return 0
