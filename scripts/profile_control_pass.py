"""Profile one control pass in a disposable copy with a read-only outer filesystem.

Prepare a snapshot once, then give each process a fresh copy of that snapshot.
The SQLite counters measure returned payload bytes, not physical disk traffic.
Allocation sites come from the largest sampled live traced heap; the traced
peak and process high-water RSS are measured independently. Execution actions
(worker starts, promotions and session resumes) are recorded without running.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import resource
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import defaultdict
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class UnsafeProfileError(ValueError):
    """The requested destination cannot be proved disposable."""


def live_home() -> Path:
    from reckon._store import _config_home

    return Path(os.environ.get("RECKON_PROFILE_REAL_HOME") or _config_home()).resolve()


def require_copy(home: Path, source: Path) -> None:
    """Refuse live paths, ancestors, descendants and symlink aliases before writing."""
    home, source = home.resolve(), source.resolve()
    for protected in (source, live_home()):
        if home.is_relative_to(protected) or protected.is_relative_to(home):
            raise UnsafeProfileError(
                f"refusing real crew home or overlapping path: {home}"
            )
    scratch = Path(
        os.environ.get("RECKON_PROFILE_SCRATCH") or tempfile.gettempdir()
    ).resolve()
    if home == scratch or not home.is_relative_to(scratch):
        raise UnsafeProfileError(
            f"copy must be below the temporary directory: {scratch}"
        )


def inventory(root: Path) -> dict[str, dict]:
    """Hash regular files and symlink text without following directory symlinks."""
    if not root.is_dir():
        raise UnsafeProfileError(f"source directory is absent: {root}")
    result = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in sorted(folders + files):
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            try:
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    result[relative] = {"link": os.readlink(path)}
                elif stat.S_ISREG(mode):
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    result[relative] = {
                        "bytes": path.stat().st_size,
                        "sha256": digest.hexdigest(),
                    }
                elif stat.S_ISDIR(mode):
                    result[relative] = {"directory": True}
                else:
                    result[relative] = {"mode": stat.S_IFMT(mode)}
            except FileNotFoundError:
                result[relative] = {"changed_during_inventory": True}
    return result


def input_digest(listing: dict) -> str:
    return hashlib.sha256(json.dumps(listing, sort_keys=True).encode()).hexdigest()


def require_snapshot(home: Path, receipt: dict) -> None:
    """Bind the measured input to the receipt before starting another process."""
    listing = {
        name: value
        for name, value in inventory(home).items()
        if name != ".profile-copy.json"
        and not re.fullmatch(
            r"(?:profile|integrity)-(?:dispatch|admission|station)-[0-9]+\.json", name
        )
    }
    if (
        listing != receipt["input_listing"]
        or input_digest(listing) != receipt["input_digest"]
    ):
        raise UnsafeProfileError(
            "the copied input changed since its receipt was written"
        )


def rebind_paths(destination: Path, source: Path) -> None:
    """Rebind source-owned JSON paths and symlinks in a newly created copy."""
    for directory, folders, files in os.walk(destination, followlinks=False):
        for name in folders + files:
            path = Path(directory) / name
            if path.name == ".profile-copy.json":
                continue
            if path.is_symlink():
                target = path.resolve()
                if target.is_relative_to(source):
                    path.unlink()
                    path.symlink_to(destination / target.relative_to(source))
            elif path.suffix == ".json" and path.is_file():
                text = path.read_text()
                if str(source) in text:
                    path.write_text(text.replace(str(source), str(destination)))


def prepare(source: Path, destination: Path) -> dict:
    """Copy the entire named config home, with symlinks kept read-only by the fence."""
    require_copy(destination, source)
    before = inventory(source)
    shutil.copytree(source, destination, symlinks=True)
    # A database may change while being copied. SQLite's backup pins a committed
    # reading rather than accepting a file assembled across two transactions.
    for database in (source / "crew").glob("*.db"):
        target = destination / database.relative_to(source)
        if target.is_symlink():
            target.unlink()
        with (
            contextlib.closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            ) as reader,
            contextlib.closing(sqlite3.connect(target)) as writer,
        ):
            reader.backup(writer)
    rebind_paths(destination, source)
    after = inventory(source)
    copied = inventory(destination)
    receipt = {
        "source_home": str(source.resolve()),
        "source_before": before,
        "source_after": after,
        "source_unchanged_during_copy": before == after,
        "input_digest": input_digest(copied),
        "input_listing": copied,
    }
    (destination / ".profile-copy.json").write_text(json.dumps(receipt, sort_keys=True))
    return receipt


class StoreReads:
    """Account for every SQLite result row without retaining the returned rows."""

    def __init__(self):
        self.stores = defaultdict(
            lambda: {
                "rows_returned": 0,
                "returned_bytes": 0,
                "rows_decoded": 0,
                "decoded_bytes": 0,
            }
        )
        self._connect = sqlite3.connect
        self._decoding = threading.local()

    def connect(self, database, *args, **kwargs):
        owner = self
        if kwargs.get("factory") is not None:
            raise RuntimeError("profiling cannot replace a caller's SQLite factory")
        name = (
            unquote(urlparse(str(database)).path)
            if str(database).startswith("file:")
            else str(database)
        )

        class Cursor(sqlite3.Cursor):
            def execute(self, sql, parameters=()):
                self.store_name = name
                if re.search(r'"?archive"?\.', sql, re.IGNORECASE):
                    self.store_name = str(Path(name).with_suffix(".archive.db"))
                return super().execute(sql, parameters)

            def counted(self, row):
                if row is not None:
                    counter = owner.stores[getattr(self, "store_name", name)]
                    counter["rows_returned"] += 1
                    counter["returned_bytes"] += sum(
                        len(value.encode())
                        if isinstance(value, str)
                        else len(value)
                        if isinstance(value, bytes)
                        else 0
                        for value in row
                    )
                return row

            def fetchone(self):
                return self.counted(super().fetchone())

            def fetchall(self):
                rows = super().fetchall()
                for row in rows:
                    self.counted(row)
                return rows

            def fetchmany(self, size=None):
                rows = super().fetchmany() if size is None else super().fetchmany(size)
                for row in rows:
                    self.counted(row)
                return rows

            def __next__(self):
                return self.counted(super().__next__())

        class Connection(sqlite3.Connection):
            def cursor(self, factory=Cursor):
                return super().cursor(factory)

            def execute(self, sql, parameters=()):
                return self.cursor().execute(sql, parameters)

            def executemany(self, sql, parameters):
                return self.cursor().executemany(sql, parameters)

            def executescript(self, sql):
                return self.cursor().executescript(sql)

        kwargs["factory"] = Connection
        return self._connect(database, *args, **kwargs)

    @contextlib.contextmanager
    def measure(self):
        from reckon import run_store
        from reckon.crew import station_store

        record_rows = run_store._record_rows
        decoded_document = station_store.StationStore._decoded_document

        decode = json.loads

        def decoding(text, *args, **kwargs):
            tagged = getattr(self._decoding, "tagged", {})
            row = tagged.get(id(text))
            if row is not None:
                key, token = row
                counter = self.stores[key]
                counter["decoded_bytes"] += len(text.encode())
                if not token[0]:
                    counter["rows_decoded"] += 1
                    token[0] = True
            return decode(text, *args, **kwargs)

        def rows(connection, **kwargs):
            databases = {
                row[1]: row[2] for row in connection.execute("PRAGMA database_list")
            }
            for schema in kwargs.pop("schemas", ("main",)):
                key = databases.get(schema, databases.get("main", "unknown"))
                for row in record_rows(connection, schemas=(schema,), **kwargs):
                    previous = getattr(self._decoding, "tagged", {})
                    token = [False]
                    self._decoding.tagged = {
                        id(v): (key, token) for v in row[1:] if isinstance(v, str)
                    }
                    try:
                        yield row
                    finally:
                        self._decoding.tagged = previous

        def document(store, kind, identity, text):
            key = (kind, identity)
            kept = store._decoded_documents.get(key)
            if kept is None or kept[0] != text:
                counter = self.stores[str(store.root / station_store.DATABASE_NAME)]
                counter["rows_decoded"] += 1
                counter["decoded_bytes"] += len(text.encode())
            return decoded_document(store, kind, identity, text)

        with (
            patch.object(sqlite3, "connect", self.connect),
            patch.object(json, "loads", decoding),
            patch.object(run_store, "_record_rows", rows),
            patch.object(station_store.StationStore, "_decoded_document", document),
        ):
            yield


class AllocationSample:
    """Keep only the ten largest sites of the largest sampled live traced heap."""

    def __init__(self):
        self.high = 0
        self.sites = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def sample(self):
        current, _ = tracemalloc.get_traced_memory()
        if current <= self.high:
            return
        snapshot = tracemalloc.take_snapshot()
        self.high = current
        self.sites = [
            {"site": str(item.traceback), "bytes": item.size, "allocations": item.count}
            for item in snapshot.statistics("lineno")[:10]
        ]

    def watch(self):
        while not self.stop.wait(0.5):
            current, _ = tracemalloc.get_traced_memory()
            if current > self.high + 64 * 1024 * 1024:
                self.sample()

    def __enter__(self):
        tracemalloc.start()
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        self.sample()
        self.current, self.peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()


def resident_bytes() -> int:
    """This process's resident set size now, not its high-water mark."""
    with open("/proc/self/statm", encoding="ascii") as handle:
        pages = int(handle.read().split()[1])
    return pages * os.sysconf("SC_PAGE_SIZE")


#: The result fields that describe when a pass ran rather than what it decided.
_TIMING_SUFFIX = "_seconds"


def decision_digest(result: dict) -> str:
    """A digest of a pass's decisions, with every timing field removed.

    A steady-memory measure across a hundred passes has to show the passes made
    the same decisions throughout, so the comparison is over the verdicts and
    the request lists a pass returned and never over how long each stage took:
    a duration differs between two runs of the same input and would make the
    digest say the decisions moved when only the clock did.
    """

    def scrubbed(value, *, key: str = ""):
        if isinstance(value, dict):
            return {
                name: scrubbed(item, key=str(name))
                for name, item in value.items()
                if not str(name).endswith(_TIMING_SUFFIX)
                and name not in {"seconds", "started_at", "ended_at", "moment"}
            }
        if isinstance(value, (list, tuple)):
            return [scrubbed(item, key=key) for item in value]
        return value

    canonical = json.dumps(scrubbed(result), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _admission_passes(count: int) -> dict:
    """Run ``count`` admission passes in this process, sampling after each.

    Each pass is the one §7's single-pass profile runs: the launch boundary
    returns a receipt, so admission still chooses, claims, writes handovers and
    records its verdicts while no worker starts. The resident size is read
    after every pass, so the tenth and the hundredth are compared on the same
    measurement rather than inferred from a high-water mark, and each pass's
    decisions are digested so a drift in behaviour shows up beside the memory
    series.
    """
    samples = []
    started = time.monotonic()
    for index in range(1, count + 1):
        result = _pass("admission", home=None, batch=None, entries=1, repository=None)
        samples.append(
            {
                "pass": index,
                "resident_bytes": resident_bytes(),
                "decision_digest": decision_digest(result),
            }
        )
    tenth = samples[min(9, len(samples) - 1)]["resident_bytes"]
    last = samples[-1]["resident_bytes"]
    digests = sorted({sample["decision_digest"] for sample in samples})
    return {
        "passes": len(samples),
        "samples": samples,
        "tenth_pass_resident_bytes": tenth,
        "last_pass_resident_bytes": last,
        "growth_ratio": round(last / tenth, 4) if tenth else None,
        "within_twenty_percent": bool(tenth) and last <= tenth * 1.2,
        "under_four_gib": last < 4 * 1024**3,
        "decision_digests": digests,
        "decisions_steady": len(digests) == 1,
        "seconds": time.monotonic() - started,
    }


def _pass(
    command: str,
    *,
    home: Path | None,
    batch: Path | None,
    entries: int,
    repository: Path | None,
) -> dict:
    from reckon import flight
    from reckon.crew import dispatch_batch, station, station_store
    from reckon.crew.admission import launching, service

    if command == "dispatch":
        if batch is None:
            raise ValueError("dispatch requires --batch")
        chosen = dispatch_batch.read_batch_entries(batch)[:entries]
        if len(chosen) != entries:
            raise ValueError("the batch contains fewer entries than requested")
        result = dispatch_batch.queue_batch(
            chosen,
            session="profile-control",
            repo_root=repository,
            config_for=lambda project: (
                flight.resolve(project, checkout_path=repository).config
            ),
        )
        if result.get("refused") or result.get("queued") != entries:
            raise RuntimeError(
                f"dispatch did not store every requested entry: {result}"
            )
        return result
    if command == "admission":

        def planned(pointer, **kwargs):
            # Admission still chooses, claims, writes handovers and records its
            # verdict; the worker execution boundary returns a copied receipt.
            return {**pointer, "phase": "profile-planned", "profile_only": True}

        from reckon.crew.promotion_requests import PromotionService

        with (
            patch.object(launching, "_launch_queued", planned),
            patch.object(PromotionService, "tick", return_value=None),
        ):
            return service.admit_until_stopped(
                once=True, lander=lambda: {"profile_only": True}
            )
    if command == "station":
        runtime = home / ".profile-runtime"
        runtime.mkdir(exist_ok=True)

        # A synchronous derivation is allowed to finish inside the process's
        # external deadline. Sweeps are execution actions, recorded separately.
        class Duties:
            def drain(self, timeout=None):
                return None

            def committing(self):
                return contextlib.nullcontext()

            def request(self, project, **kwargs):
                from reckon.crew import station_obligations

                with station_store.StationStore(runtime) as store:
                    station_obligations.publish(
                        store,
                        project=project,
                        home=kwargs["home"],
                        changed=kwargs["changed"],
                        now=kwargs["moment"],
                    )
                return True

        sweeps = []

        class Sweeps:
            on_report = None

            def request(self, project, session, **kwargs):
                sweeps.append({"project": project, "session": session})
                return True

        with station_store.StationStore(runtime) as store:
            instance = station.Station(
                store, home=home / "crew", duties=Duties(), sweeps=Sweeps()
            )
            events = []
            tick = instance.tick

            def record_tick():
                events.extend(tick())

            instance.tick = record_tick
            # Execution sweeps stay suppressed; lease and heartbeat writers run.
            with patch.object(instance.reflex, "start", return_value=None):
                station.run_station(once=True, store=store, station_instance=instance)
        return {"events": events, "suppressed_sweeps": sweeps}
    raise ValueError(f"unknown control command: {command}")


class _Untraced:
    """The allocation figures a multi-pass run does not collect.

    A hundred traced passes cost more wall time than the measure is worth and
    inflate the resident figures the measure exists to read, so the multi-pass
    mode reads the resident size itself and reports no traced peak.
    """

    peak: ClassVar[int] = 0
    current: ClassVar[int] = 0
    high: ClassVar[int] = 0
    sites: ClassVar[list] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def profile(
    command: str,
    *,
    home: Path,
    batch: Path | None,
    entries: int,
    repository: Path,
    passes: int = 1,
) -> dict:
    """Run only inside the fence, preserving failures as nonzero receipts."""
    if os.environ.get("RECKON_PROFILE_FENCED") != "1":
        raise UnsafeProfileError(
            "control passes require the read-only filesystem fence"
        )
    receipt = json.loads((home / ".profile-copy.json").read_text())
    require_copy(home, Path(receipt["source_home"]))
    # SQLite spills sorts and temporary tables to disk. Its directory must
    # exist before the first query, since every fallback directory is read-only.
    for name in (".profile-tmp", ".profile-cache", ".profile-runtime"):
        (home / name).mkdir(exist_ok=True)
    stores = StoreReads()
    started = time.monotonic()
    error = None
    result = None
    multi_pass = command == "admission" and passes > 1
    sampler = _Untraced() if multi_pass else AllocationSample()
    try:
        with sampler as allocations, stores.measure():
            result = (
                _admission_passes(passes)
                if multi_pass
                else _pass(
                    command,
                    home=home,
                    batch=batch,
                    entries=entries,
                    repository=repository,
                )
            )
    except Exception as exc:  # noqa: BLE001 - failed passes must still deliver a receipt
        error = f"{type(exc).__name__}: {exc}"
    return {
        "command": command,
        "passes": passes,
        "entries": entries if command == "dispatch" else None,
        "revision": subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "input_digest": receipt["input_digest"],
        "seconds": time.monotonic() - started,
        "peak_resident_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * 1024,
        "traced_peak_bytes": allocations.peak,
        "traced_retained_bytes": allocations.current,
        "allocation_sample_bytes": allocations.high,
        "largest_allocation_sites": allocations.sites,
        "stores": dict(stores.stores),
        "result": result,
        "error": error,
        "exit_status": int(error is not None),
        "execution_actions": "worker starts, promotions and resumes are suppressed",
        "bytes_semantics": "UTF-8 payload bytes returned by SQLite; not physical disk reads",
        "decode_semantics": "durable rows whose JSON is decoded, and station JSON cache misses; excludes timestamp projections",
    }


def fenced_command(home: Path, arguments: list[str]) -> list[str]:
    """Make the copy the only writable mount, including for any subprocess."""
    return [
        "bwrap",
        "--die-with-parent",
        "--unshare-user",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--bind",
        str(home),
        str(home),
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--chdir",
        str(ROOT),
        "--setenv",
        "RECKON_PROFILE_REAL_HOME",
        str(live_home()),
        "--setenv",
        "RECKON_PROFILE_SCRATCH",
        tempfile.gettempdir(),
        "--setenv",
        "RECKON_HOME",
        str(home),
        "--setenv",
        "RECKON_RUN_STORE",
        str(home / "crew" / "run_store.db"),
        "--setenv",
        "RECKON_STATE_ROOT",
        str(home / "state"),
        "--setenv",
        "RECKON_MOUNTS_PATH",
        str(home / "mounts.json"),
        "--setenv",
        "RECKON_FLIGHT_CONFIG",
        str(home / "flight.yaml"),
        "--setenv",
        "XDG_RUNTIME_DIR",
        str(home / ".profile-runtime"),
        "--setenv",
        "XDG_CACHE_HOME",
        str(home / ".profile-cache"),
        "--setenv",
        "TMPDIR",
        str(home / ".profile-tmp"),
        "--setenv",
        "SQLITE_TMPDIR",
        str(home / ".profile-tmp"),
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "RECKON_PROFILE_FENCED",
        "1",
        "--",
        sys.executable,
        str(Path(__file__).resolve()),
        *arguments,
        "--child",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("prepare", "dispatch", "admission", "station")
    )
    parser.add_argument("--source-home", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--entries", type=int, default=1)
    parser.add_argument(
        "--passes",
        type=int,
        default=1,
        help="consecutive admission passes to run, sampling resident memory after each",
    )
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args(argv)
    try:
        require_copy(args.home, args.source_home)
        if args.command == "prepare":
            receipt = prepare(args.source_home.resolve(), args.home.resolve())
            print(
                json.dumps(
                    {
                        key: value
                        for key, value in receipt.items()
                        if not isinstance(value, dict)
                    }
                )
            )
            return 0
        receipt = json.loads((args.home / ".profile-copy.json").read_text())
        if Path(receipt["source_home"]).resolve() != args.source_home.resolve():
            raise UnsafeProfileError("source does not match the copy receipt")  # noqa: TRY301 - CLI reports refusal uniformly
        if args.child:
            record = profile(
                args.command,
                home=args.home,
                batch=args.batch,
                entries=args.entries,
                repository=args.repository,
                passes=args.passes,
            )
            (args.home / f"profile-{args.command}-{args.entries}.json").write_text(
                json.dumps(record, indent=2)
            )
            print(json.dumps(record))
            return record["exit_status"]
        require_snapshot(args.home, receipt)
        before = inventory(args.source_home)
        # Each process starts with the same input, even when the preceding pass
        # wrote pointers, verdicts, station state or a SQLite transaction.
        with tempfile.TemporaryDirectory(prefix="control-profile-") as scratch:
            working = Path(scratch) / "home"
            shutil.copytree(
                args.home,
                working,
                symlinks=True,
                ignore=shutil.ignore_patterns("profile-*.json", "integrity-*.json"),
            )
            rebind_paths(working, args.home.resolve())
            arguments = list(argv) if argv is not None else sys.argv[1:]
            arguments[arguments.index("--home") + 1] = str(working)
            completed = subprocess.run(
                fenced_command(working, arguments), timeout=args.timeout, check=False
            )
            report = working / f"profile-{args.command}-{args.entries}.json"
            if report.is_file():
                shutil.copyfile(report, args.home / report.name)
        after = inventory(args.source_home)
        integrity = {"before": before, "after": after, "unchanged": before == after}
        (args.home / f"integrity-{args.command}-{args.entries}.json").write_text(
            json.dumps(integrity)
        )
        if before != after:
            print(
                "source crew home changed during pass; no unchanged-source claim is valid",
                file=sys.stderr,
            )
            return 1
        return completed.returncode
    except (UnsafeProfileError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"profile-refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
