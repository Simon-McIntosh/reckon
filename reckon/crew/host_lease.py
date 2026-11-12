"""A renewable single-owner lease in the fleet's shared state directory."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

LEASE_RENEW_SECONDS = 5.0
LEASE_STALE_SECONDS = 30.0


def local_host() -> str:
    """The host identity a node lease writes and compares."""
    return socket.gethostname()


def _pid_namespace() -> str:
    """Identify the process table in which a holder's PID has meaning."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


@dataclass(frozen=True)
class LeaseHolder:
    host: str
    pid: int
    job: str
    token: str
    pid_namespace: str = ""


class HostLease:
    """Claim with an atomic link; renew and release only the claimed inode."""

    def __init__(
        self,
        state: Path,
        name: str,
        host: str,
        pid: int,
        job: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = state / "leases" / f"{name}.json"
        self.owner = LeaseHolder(host, pid, job, uuid4().hex, _pid_namespace())
        self._clock = clock
        self._inode: int | None = None

    def _read(self) -> tuple[LeaseHolder | None, os.stat_result | None]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                stat = os.fstat(stream.fileno())
                record = json.load(stream)
            return LeaseHolder(
                str(record["host"]),
                int(record["pid"]),
                str(record["job"]),
                str(record["token"]),
                str(record.get("pid_namespace") or ""),
            ), stat
        except (OSError, ValueError, KeyError, TypeError):
            try:
                return None, self.path.stat()
            except FileNotFoundError:
                return None, None

    def holder(self) -> LeaseHolder | None:
        """Return the fresh holder, if any."""
        holder, stat = self._read()
        if stat is None or self._clock() - stat.st_mtime >= LEASE_STALE_SECONDS:
            return None
        return holder

    def claim(self) -> bool:
        """Take an absent or stale lease, or reattach after an in-place exec."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            holder, stat = self._read()
            if holder is not None and stat is not None:
                if (
                    (holder.host, holder.pid, holder.job, holder.pid_namespace)
                    == (
                        self.owner.host,
                        self.owner.pid,
                        self.owner.job,
                        self.owner.pid_namespace,
                    )
                    and holder.pid_namespace
                    and self._clock() - stat.st_mtime < LEASE_STALE_SECONDS
                ):
                    self.owner = holder
                    self._inode = stat.st_ino
                    return True
                if self._clock() - stat.st_mtime < LEASE_STALE_SECONDS:
                    return False
            if stat is not None:
                try:
                    if self.path.stat().st_ino == stat.st_ino:
                        self.path.unlink()
                except FileNotFoundError:
                    pass
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".lease-",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self.owner.__dict__, stream)
                stream.flush()
                os.fsync(stream.fileno())
                os.utime(stream.fileno(), (self._clock(), self._clock()))
            try:
                os.link(temporary, self.path)
            except FileExistsError:
                continue
            finally:
                temporary.unlink()
            self._inode = self.path.stat().st_ino
            return True
        return False

    def renew(self) -> bool:
        """Refresh this lease without touching a successor's inode."""
        holder, stat = self._read()
        if holder != self.owner or stat is None or stat.st_ino != self._inode:
            return False
        try:
            descriptor = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            if os.fstat(descriptor).st_ino != self._inode:
                return False
            os.utime(descriptor, (self._clock(), self._clock()))
            return True
        finally:
            os.close(descriptor)

    def release(self) -> bool:
        """Remove only the entry still naming this owner and inode."""
        holder, stat = self._read()
        if holder != self.owner or stat is None or stat.st_ino != self._inode:
            return False
        try:
            if self.path.stat().st_ino != self._inode:
                return False
            self.path.unlink()
        except FileNotFoundError:
            return False
        self._inode = None
        return True

    def release_holder(self, holder: LeaseHolder) -> bool:
        """Clear a dead holder only from the process table its PID names.

        A worker can share the host's lease directory while seeing a separate
        PID namespace. Its failed PID probe says nothing about the host's
        holder, so it must leave the lease to renewal or stale expiry. Records
        with no namespace likewise cannot justify an early release.
        """
        if (
            holder.host != self.owner.host
            or not holder.pid_namespace
            or holder.pid_namespace != self.owner.pid_namespace
        ):
            return False
        current, stat = self._read()
        if current != holder or stat is None:
            return False
        try:
            if self.path.stat().st_ino != stat.st_ino:
                return False
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True
