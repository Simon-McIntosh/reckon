"""Paste the terminal client's clipboard onto this host and onto the live fleet.

The clipboard lives on the machine the terminal runs on, not on the host the
shell reached. A bridge on the client answers HTTP on a fixed port, and the ssh
tunnel reverse-forwards that port to the login nodes, so ``localhost:<port>`` on
a login node reaches it: ``GET /health`` answers when the bridge is up,
``GET /paste`` returns the clipboard, and ``POST /copy`` puts text back on it. A
reverse forward binds only on the node its ssh session reached, so a shell on a
login node the tunnel did not reach has no bridge at all.

A compute node never has one, and a fleet pane is a shell on a compute node.
Inside a SLURM job with no forward bound here, the bridge is reached through
the job's submit host instead: ``curl`` runs there over ssh against that login
node's forward, the same instrument the tunnel's own health probe uses.

When the bridge does not answer, the diagnosis says which side is broken by
asking the tunnel's second reverse forward, the client's sshd on the ssh-back
port. A banner through it proves the tunnel reaches the node, so the bridge
behind it is down. Silence through both means the ports are held by a tunnel
session that dropped with the client's network, which the server keeps for
minutes and which no restart on the client can displace; ``imas-codex tunnel
reclaim`` on the login node ends it.

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

import contextlib
import json
import os
import shlex
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

# The tunnel's ssh-back forward: the client's sshd, reverse-forwarded beside the
# bridge. Its banner is the witness that tells a down bridge from a dead tunnel.
SSH_PORT_ENV = "WSL_SSH_PORT"
DEFAULT_SSH_PORT = 2222

HEALTH_TIMEOUT_SECONDS = 2.0
PASTE_TIMEOUT_SECONDS = 12.0
SCHEDULER_TIMEOUT_SECONDS = 10.0
COPY_TIMEOUT_SECONDS = 60.0
BANNER_TIMEOUT_SECONDS = 4
# What ssh itself may add to a relayed request: connecting to the login node.
RELAY_OVERHEAD_SECONDS = 8.0

# curl -f exits with this when the server answered with an HTTP error status.
_CURL_HTTP_ERROR = 22

_SSH = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR")

# The client-side tunnel is managed by this CLI; every remedy names it.
_TUNNEL_CLI = "uv run --project ~/Code/imas-codex imas-codex tunnel"

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


def ssh_back_port(environ: Mapping[str, str] | None = None) -> int:
    """The port the tunnel forwards the client's sshd to on the login nodes."""
    environ = os.environ if environ is None else environ
    value = str(environ.get(SSH_PORT_ENV, "")).strip()
    return int(value) if value.isdigit() else DEFAULT_SSH_PORT


def relay_host(environ: Mapping[str, str] | None = None) -> str | None:
    """The login node to reach the bridge through from inside a SLURM job.

    The job's submit host is a login node, and the tunnel binds its reverse
    forwards on the login nodes. None outside a job, or when this host is the
    submit host itself.
    """
    environ = os.environ if environ is None else environ
    host = str(environ.get("SLURM_SUBMIT_HOST", "")).strip()
    if not host or host.split(".")[0] == _short_hostname():
        return None
    return host


def _on_host(host: str, run: Runner) -> Runner:
    """A runner that runs each command on ``host`` over ssh instead of here."""

    def remote(argv, **kwargs):
        return run([*_SSH, host, shlex.join(argv)], **kwargs)

    return remote


class RelayError(Exception):
    """The bridge did not answer through the login node relayed through.

    ``reached`` is False when ssh never reached the login node, so nothing is
    known about the bridge; ``answered`` is True when the bridge replied with an
    HTTP error, so it is up and the request itself failed.
    """

    def __init__(self, detail: str, *, reached: bool, answered: bool = False):
        super().__init__(detail)
        self.reached = reached
        self.answered = answered


def _relay(
    host: str,
    port: int,
    route: str,
    run: Runner,
    *,
    timeout: float,
    data: bytes | None = None,
) -> bytes:
    """One bridge request made by ``curl`` on ``host``, against its forward."""
    command = ["curl", "-fsS", "--noproxy", "*", "--max-time", str(int(timeout))]
    if data is not None:
        command += ["--data-binary", "@-"]
    command.append(f"http://127.0.0.1:{port}/{route}")
    try:
        reply = run(
            [*_SSH, host, shlex.join(command)],
            input=data,
            capture_output=True,
            timeout=timeout + RELAY_OVERHEAD_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RelayError(str(exc), reached=False) from exc
    if reply.returncode != 0:
        detail = (reply.stderr or b"").decode("utf-8", errors="replace").strip()
        lines = detail.splitlines()
        raise RelayError(
            lines[-1] if lines else f"exit {reply.returncode}",
            reached=reply.returncode != 255,
            answered=reply.returncode == _CURL_HTTP_ERROR,
        )
    return reply.stdout


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


def _ssh_back_answers(port: int, run: Runner) -> bool | None:
    """Whether the client's sshd answers through the ssh-back forward.

    None when that forward is not bound, so it can witness nothing. A banner is
    read rather than a login attempted, so the verdict describes the tunnel and
    not which keys the client authorises.
    """
    if not _forward_bound_here(port, run):
        return None
    try:
        reply = run(
            [
                "timeout",
                str(BANNER_TIMEOUT_SECONDS),
                "bash",
                "-c",
                'exec 3<>"/dev/tcp/127.0.0.1/$0"; head -c 4 <&3',
                str(port),
            ],
            capture_output=True,
            text=True,
            timeout=SCHEDULER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return reply.stdout.startswith("SSH-")


def bridge_diagnosis(
    port: int,
    run: Runner = subprocess.run,
    *,
    ssh_port: int = DEFAULT_SSH_PORT,
    node: str | None = None,
) -> list[str]:
    """Say why the bridge is unreachable on ``node``, and what restores it.

    ``run`` executes on ``node``: this host by default, or a login node relayed
    through. A remedy to run on the node is spelled for wherever the reader is.
    """
    here = _short_hostname()
    node = node or here
    on_node = "" if node == here else f"ssh {node} "
    reclaim = f"{on_node}{_TUNNEL_CLI} reclaim"
    restart_bridge = "systemctl --user restart wsl-clip-server.service"
    headline = f"clipboard bridge unreachable on {node}:{port}"
    if not _forward_bound_here(port, run):
        return [
            headline,
            (
                f"  No reverse forward is bound on {node}: the tunnel from WSL is "
                "down or reconnecting, or does not reach this node."
            ),
            "  Check it, on WSL:",
            f"    {_TUNNEL_CLI} service status iter",
            "  Not yet installed to bind on every login node? On WSL:",
            f"    {_TUNNEL_CLI} service install iter --reverse-node all",
        ]
    witness = _ssh_back_answers(ssh_port, run)
    if witness:
        return [
            headline,
            f"  The tunnel reaches {node}: WSL's sshd answers through :{ssh_port}.",
            f"  So the bridge behind :{port} is down. Fix, on WSL:",
            f"    {restart_bridge}",
            (
                f"  WSL is reachable from {node} through the same tunnel: "
                f"ssh -p {ssh_port} localhost"
            ),
        ]
    if witness is False:
        return [
            headline,
            (
                f"  Both reverse forwards on {node} (:{port}, :{ssh_port}) are "
                "bound, and nothing answers through either."
            ),
            (
                "  A tunnel session that dropped with WSL's network still holds "
                "them, so the reconnected tunnel cannot bind here."
            ),
            (
                "  The tunnel service frees them itself within a minute or two. "
                f"To free them now, on {node}:"
            ),
            f"    {reclaim}",
        ]
    return [
        headline,
        (
            f"  The reverse forward on {node} is bound, but nothing answers through "
            f"it, and with no ssh-back forward on :{ssh_port}"
        ),
        "  there is no telling a dropped tunnel session from a down bridge.",
        (
            "  A dropped session (it ends only one whose forwards all go "
            f"unanswered), on {node}:"
        ),
        f"    {reclaim}",
        "  A down bridge, on WSL:",
        f"    {restart_bridge}",
    ]


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
    ssh_port = ssh_back_port(environ)

    # The login node the bridge is reached through, when this host has no
    # forward of its own; None reads this host's forward directly.
    via: str | None = None
    if not bridge_healthy(port):
        via = relay_host(environ)
        if via is None or _forward_bound_here(port, run):
            _report(bridge_diagnosis(port, run, ssh_port=ssh_port), err)
            return 1

    if via is None:
        try:
            data = fetch_clipboard(port)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            print(
                f"reckon paste: the bridge did not return the clipboard: {exc}",
                file=err,
            )
            return 1
    else:
        try:
            data = _relay(via, port, "paste", run, timeout=PASTE_TIMEOUT_SECONDS)
        except RelayError as exc:
            node = via.split(".")[0]
            here = _short_hostname()
            if exc.answered:
                print(
                    f"reckon paste: the bridge did not return the clipboard: {exc}",
                    file=err,
                )
                return 1
            if not exc.reached:
                print(
                    f"reckon paste: {here} has no clipboard forward of its own, and "
                    f"{node} could not be reached to relay through: {exc}",
                    file=err,
                )
                return 1
            context = (
                f"  {here} has no clipboard forward of its own (a compute node never "
                f"does), so the bridge was asked for through {node}."
            )
            diagnosis = bridge_diagnosis(
                port, _on_host(via, run), ssh_port=ssh_port, node=node
            )
            _report(diagnosis, err, context=context)
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
    if via is None:
        copy_to_clipboard(port, str(image))
    else:
        # Best effort, as copy_to_clipboard is: the path is already printed.
        with contextlib.suppress(RelayError):
            _relay(
                via,
                port,
                "copy",
                run,
                timeout=HEALTH_TIMEOUT_SECONDS,
                data=str(image).encode("utf-8"),
            )
    return 0


def _report(lines: list[str], err, *, context: str | None = None) -> None:
    headline, *remedy = lines
    print(f"reckon paste: {headline}", file=err)
    if context:
        print(context, file=err)
    for line in remedy:
        print(line, file=err)
