"""Reconstruct a fixed cohort and execute it in isolated promotion repositories.

The copied requests retain their options and records. Filesystem paths and dead
process handles are relocated; production promotion guards and the drain run
without a substituted executor or recorded service-time model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from reckon._timestamps import parse_utc


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def collect(crew: Path, start: str, end: str, census: Path | None = None) -> dict:
    """Copy retained requests and bounded run records, without writing sources."""
    lower, upper = parse_utc(start), parse_utc(end)
    if lower is None or upper is None or lower >= upper:
        raise ValueError("selection needs an increasing UTC interval")
    sources = read_object(census / "sources.json") if census else {}
    history = list(sources.get("drained") or [])
    drained = crew / "promotion-drained.jsonl"
    if drained.is_file():
        for line in drained.read_text().splitlines():
            try:
                history.append(json.loads(line))
            except ValueError:
                continue
    historical = list((sources.get("requests") or {}).values())
    retained = [
        read_object(path)
        for path in sorted((crew / "promotion-requests").glob("*.json*"))
        if path.is_file()
    ]
    selected = {}
    for request in historical + retained:
        stamp = parse_utc(request.get("requested_at"))
        if stamp is not None and lower <= stamp < upper:
            selected[(request["run_id"], request["request_id"])] = request
    rows = []
    for (run_id, _request_id), request in sorted(selected.items()):
        pointer = read_object(crew / "live" / f"{run_id}.json")
        if not pointer:
            pointer = (sources.get("pointers") or {}).get(run_id, {})
        files = {}
        for name in (
            "dispatch-detail.json",
            "manifest.md",
            "supervisor.json",
            "worker.json",
            "exit.json",
            "end.json",
            "promotion.json",
            "claim.json",
        ):
            for directory in (
                crew / "runs" / run_id,
                crew / "archive" / "runs" / run_id,
            ):
                path = directory / name
                if path.is_file():
                    files[name] = path.read_text()
                    break
        rows.append(
            {
                "request": request,
                "pointer": pointer,
                "run_files": files,
                "ledger": (sources.get("ledgers") or {}).get(run_id, {}),
                "drained": [
                    row
                    for row in history
                    if row.get("run_id") == run_id
                    and row.get("requested_at") == request.get("requested_at")
                ],
            }
        )
    return {
        "header": {
            "selection_rule": f"{start} <= requested_at < {end}",
            "count": len(rows),
            "copied_set_sha256": digest(rows),
            "source_crew": str(crew),
            "source_census": str(census) if census else None,
            "method": "retained requests over historical census requests, keyed by run_id and request_id; bounded run files resolved through archive",
        },
        "rows": rows,
    }


def validate_dataset(data: dict) -> list[dict]:
    """Refuse empty or changed inputs before reporting any throughput."""
    rows = data["rows"]
    header = data["header"]
    if not rows:
        raise ValueError("empty request directory: no rate can be measured")
    if len(rows) != header["count"] or digest(rows) != header["copied_set_sha256"]:
        raise ValueError("copied set does not match its count and digest")
    return rows


def git(repository: Path, *arguments: str, timeout: float = 120) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def prepare(data: dict, destination: Path, mounts: dict) -> dict:
    """Freeze repository revisions and extra crew artifacts before either arm."""
    rows = validate_dataset(data)
    destination.mkdir(parents=True, exist_ok=True)
    projects = {}
    heads = {}
    extras = {}
    crew = Path(data["header"]["source_crew"])
    for row in rows:
        request, pointer = row["request"], row["pointer"]
        project = request.get("project") or pointer.get("project")
        if project and project not in projects:
            raw = pointer.get("repo")
            if not raw and project in mounts:
                raw = str(Path(mounts[project]).parent)
            if not raw:
                continue
            root = Path(raw).resolve()
            objects = Path(git(root, "rev-parse", "--git-path", "objects"))
            if not objects.is_absolute():
                objects = (root / objects).resolve()
            projects[project] = {
                "root": str(root),
                "objects": str(objects),
                "revision": git(root, "rev-parse", "HEAD"),
            }
        if project not in projects:
            continue
        root = Path(projects[project]["root"])
        citations = []
        manifest = row["run_files"].get("manifest.md", "")
        match = re.search(r"^commits:\s*(.*)$", manifest, re.MULTILINE)
        if match:
            citations.extend(re.findall(r"\b[0-9a-f]{7,40}\b", match[1]))
        if pointer.get("promoted_revision"):
            citations.append(pointer["promoted_revision"])
        if pointer.get("worktree") and Path(pointer["worktree"]).is_dir():
            with suppress(RuntimeError):
                citations.append(git(Path(pointer["worktree"]), "rev-parse", "HEAD"))
        for citation in reversed(citations):
            try:
                heads[request["run_id"]] = git(
                    root, "rev-parse", "--verify", f"{citation}^{{commit}}"
                )
                break
            except RuntimeError:
                continue
        # Only explicitly cited crew files are copied, never a recursive stream
        # or corpus walk. Missing artifacts stay missing for the real guard.
        for raw in re.findall(r"/[^\s\"'<>;,\]\)]+", manifest):
            path = Path(raw.rstrip("."))
            try:
                relative = path.relative_to(crew)
            except ValueError:
                continue
            if not path.is_file() and relative.parts[:1] == ("runs",):
                path = crew / "archive" / relative
            if not path.is_file() or path.stat().st_size > 5_000_000:
                continue
            blob = path.read_bytes()
            name = hashlib.sha256(str(relative).encode()).hexdigest()
            (destination / name).write_bytes(blob)
            extras[str(relative)] = {
                "file": name,
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
    result = {"projects": projects, "heads": heads, "extras": extras}
    result["sha256"] = digest(result)
    (destination / "sources.json").write_text(json.dumps(result, sort_keys=True))
    return result


def _repository(root: Path, source: dict) -> None:
    """Create a temporary checkout sharing only read-only historical objects."""
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / ".git/objects/info/alternates").write_text(source["objects"] + "\n")
    packed = root / ".git/objects/pack"
    for original in sorted((Path(source["objects"]) / "pack").glob("*")):
        if original.suffix in {".pack", ".idx"}:
            shutil.copyfile(original, packed / original.name)
    git(root, "config", "user.name", "Replay fixture")
    git(root, "config", "user.email", "replay@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "config", "gc.auto", "0")
    git(root, "config", "filter.lfs.process", "")
    git(root, "config", "filter.lfs.smudge", "cat")
    git(root, "config", "filter.lfs.required", "false")
    git(root, "read-tree", source["revision"])
    git(root, "checkout-index", "--all", timeout=600)
    git(root, "update-ref", "refs/heads/main", source["revision"])
    origin = root.parent / (root.name + "-origin.git")
    origin.mkdir()
    git(origin, "init", "-q", "--bare")
    (origin / "objects/info/alternates").write_text(source["objects"] + "\n")
    git(origin, "update-ref", "refs/heads/main", source["revision"])
    git(root, "remote", "add", "origin", str(origin))
    git(root, "config", "extensions.worktreeConfig", "true")
    environment = Path(source["root"]) / ".venv"
    if environment.exists():
        (root / ".venv").symlink_to(environment)


def _positive_control(root: Path, requests, runs) -> dict:
    """A fresh report must really promote before a zero can be believed."""
    project, run_id = "replay-instrument", "r-replay-instrument"
    repository = root / "control-repository"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.name", "Replay fixture")
    git(repository, "config", "user.email", "replay@example.invalid")
    git(repository, "config", "commit.gpgsign", "false")
    (repository / "seed.txt").write_text("instrument\n")
    (repository / "docs" / "state" / project).mkdir(parents=True)
    git(repository, "add", "seed.txt")
    git(
        repository,
        "commit",
        "-qm",
        "test: seed promotion instrument\n\nProvide a private repository for a real report promotion.",
    )
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        f"node: replay-instrument\nstatus: complete\ncommits: none\nchanged_paths: none\ntests: {sys.executable} -c pass\n"
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": requests._now(),
            "manifest_path": str(manifest),
            "node": {
                "id": "replay-instrument",
                "role": "review",
                "write_paths": [],
                "time_budget": "25m",
            },
        },
    )
    result = _drain(
        [
            {
                "request": {
                    "run_id": run_id,
                    "request_id": "replay-instrument",
                    "project": project,
                    "kind": "complete",
                    "requested_at": requests._now(),
                    "priority_override": None,
                    "options": {
                        "gate": "passed",
                        "root": str(repository),
                        "unplanned_reason": "private promotion instrument",
                        "no_impl_change": "the instrument moves no plan",
                    },
                }
            }
        ],
        requests,
        runs,
        timeout=120,
    )
    if result["verdicts"] != {requests.PROMOTED: 1}:
        raise RuntimeError("positive control did not promote: " + json.dumps(result))
    return {"phase": requests.PROMOTED, "elapsed_seconds": result["elapsed_seconds"]}


def _relocate(value, replacements: dict):
    if isinstance(value, str):
        for original in sorted(replacements, key=len, reverse=True):
            value = value.replace(original, replacements[original])
        return value
    if isinstance(value, list):
        return [_relocate(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: None
            if key
            in {
                "pid",
                "worker_pid",
                "launcher_pid",
                "process_id",
                "job_id",
                "slurm_job_id",
            }
            else _relocate(item, replacements)
            for key, item in value.items()
        }
    return value


def _drain(rows: list[dict], requests, runs, *, timeout: float) -> dict:
    observed, reasons = {}, {}
    pending = list(rows)
    began = time.monotonic()
    while pending:
        active, remaining = {}, []
        for row in pending:
            request = row["request"]
            run_id = request["run_id"]
            if run_id in active:
                remaining.append(row)
                continue
            active[run_id] = request["request_id"]
            runs._write_json(
                runs.crew_home() / "promotion-requests" / f"{run_id}.json",
                {**request, "phase": requests.REQUESTED},
            )
        service = requests.PromotionService()
        service.start_refill()
        try:
            service.tick()
            while active:
                for run_id, request_id in list(active.items()):
                    receipt = requests.read_promotion(run_id)
                    phase = receipt.get("phase")
                    if receipt.get("request_id") == request_id and phase in {
                        requests.PROMOTED,
                        requests.REFUSED,
                        requests.FAILED,
                        requests.DEFERRED,
                        requests.WAITING,
                    }:
                        observed[request_id] = phase
                        reasons[request_id] = str(
                            receipt.get("error") or receipt.get("waiting_reason") or ""
                        )[:2000]
                        del active[run_id]
                if time.monotonic() - began > timeout:
                    raise TimeoutError(
                        f"real replay exceeded {timeout:g} seconds with {len(active)} requests active"
                    )
                if active:
                    time.sleep(0.01)
        finally:
            service.stop_refill()
            service.begin_drain()
            if service.wait_for_in_flight(timeout):
                raise TimeoutError("a real promotion remains in flight")
        pending = remaining
    elapsed = time.monotonic() - began
    counts = Counter(observed.values())
    return {
        "measurement": "real drain and promotion execution in temporary repositories",
        "requests": len(rows),
        "elapsed_seconds": elapsed,
        "verdicts": dict(counts),
        "verdicts_by_request": observed,
        "reasons_by_request": reasons,
        "successful_promotions_per_hour": counts[requests.PROMOTED] * 3600 / elapsed,
        "queue_report": requests.promotion_queue_report(),
    }


def replay(
    data: dict,
    *,
    sources: dict,
    assets: Path,
    scratch_root: Path | None = None,
    timeout: float = 1800,
) -> dict:
    """Run the original requests with relocated files and no substituted executor."""
    from reckon.crew import promotion_requests as requests
    from reckon.crew import runs

    rows = validate_dataset(data)
    with tempfile.TemporaryDirectory(
        prefix="promotion-real-replay-", dir=scratch_root or os.environ.get("TMPDIR")
    ) as scratch:
        root = Path(scratch)
        repositories = {}
        for project, source in sources["projects"].items():
            repository = root / "repositories" / project
            _repository(repository, source)
            repositories[project] = repository
        crew = root / "home" / "crew"
        original_crew = data["header"]["source_crew"]
        replacements = {
            original_crew + "/archive/runs": str(crew / "runs"),
            original_crew + "/runs": str(crew / "runs"),
            original_crew: str(crew),
            **{
                source["root"]: str(repositories[project])
                for project, source in sources["projects"].items()
            },
        }
        for row in rows:
            pointer = row["pointer"]
            project = row["request"].get("project") or pointer.get("project")
            run_id = row["request"]["run_id"]
            original_tree = pointer.get("worktree")
            if (
                original_tree
                and original_tree not in replacements
                and project in repositories
            ):
                worker = root / "workers" / run_id
                worker.parent.mkdir(exist_ok=True)
                revision = (
                    sources["heads"].get(run_id)
                    or sources["projects"][project]["revision"]
                )
                git(
                    repositories[project],
                    "worktree",
                    "add",
                    "--detach",
                    "--no-checkout",
                    str(worker),
                    revision,
                )
                git(worker, "config", "--worktree", "core.sparseCheckout", "true")
                git(worker, "config", "--worktree", "core.sparseCheckoutCone", "false")
                sparse = Path(
                    git(worker, "rev-parse", "--git-path", "info/sparse-checkout")
                )
                if not sparse.is_absolute():
                    sparse = worker / sparse
                sparse.parent.mkdir(parents=True, exist_ok=True)
                patterns = [
                    "/*",
                    "!/docs/state/",
                    "!/docs/figures/",
                    "!/docs/evidence/",
                ]
                declared = pointer.get("node", {}).get("write_paths", [])
                manifest = row["run_files"].get("manifest.md", "")
                match = re.search(r"^changed_paths:\s*(.*)$", manifest, re.MULTILINE)
                if match:
                    declared = [*declared, *re.findall(r"docs/[^\s,\]\"']+", match[1])]
                patterns.extend(
                    "/" + path.rstrip("/")
                    for path in declared
                    if path.startswith("docs/")
                )
                sparse.write_text("\n".join(patterns) + "\n")
                git(worker, "read-tree", "-mu", "HEAD", timeout=600)
                environment = Path(sources["projects"][project]["root"]) / ".venv"
                if environment.exists():
                    (worker / ".venv").symlink_to(environment)
                replacements[original_tree] = str(worker)
        for relative, entry in sources.get("extras", {}).items():
            blob = (assets / entry["file"]).read_bytes()
            if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
                raise ValueError("copied replay artifact does not match its digest")
            target = Path(_relocate(str(Path(original_crew) / relative), replacements))
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_text(_relocate(blob.decode(), replacements))
            except UnicodeDecodeError:
                target.write_bytes(blob)
        relocated = []
        for row in rows:
            copied = _relocate(row, replacements)
            run_id = row["request"]["run_id"]
            directory = crew / "runs" / run_id
            directory.mkdir(parents=True, exist_ok=True)
            for name, content in copied["run_files"].items():
                # Terminal promotion receipts belong to the source attempt,
                # never to an execution that this arm has not performed yet.
                if name == "promotion.json":
                    continue
                relocated_content = content
                with suppress(ValueError):
                    relocated_content = json.dumps(
                        _relocate(json.loads(content), replacements)
                    )
                (directory / name).write_text(relocated_content)
            if copied["pointer"]:
                copied["pointer"]["launch"] = "in-harness"
                pointer_path = crew / "live" / f"{run_id}.json"
                pointer_path.parent.mkdir(exist_ok=True)
                pointer_path.write_text(json.dumps(copied["pointer"]))
            relocated.append(copied)
        home = root / "home"
        (home / "mounts.json").write_text(
            json.dumps(
                {project: str(path / "docs") for project, path in repositories.items()}
            )
        )
        environment = {
            "RECKON_HOME": str(home),
            "RECKON_RUN_STORE": str(crew / "run_store.db"),
            "RECKON_MOUNTS_PATH": str(home / "mounts.json"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "TMPDIR": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TERMINAL_PROMPT": "0",
        }
        with patch.dict(os.environ, environment):
            with patch.dict(
                os.environ,
                {
                    "RECKON_HOME": str(root / "control-home"),
                    "RECKON_RUN_STORE": str(root / "control-store.db"),
                    "RECKON_MOUNTS_PATH": str(root / "control-home" / "mounts.json"),
                },
            ):
                control = _positive_control(root, requests, runs)
            result = _drain(relocated, requests, runs, timeout=timeout)
        result["positive_control"] = control
        result["header"] = {
            **data["header"],
            "repository_sources_sha256": sources["sha256"],
            "relocation": "repositories and crew paths relocated; process and scheduler handles cleared; request options and promotion guards retained",
            "worker_materialization": "sparse checkout retains source and declared deliverables, omitting duplicate docs/state, docs/figures and docs/evidence except declared paths; canonical repositories and Git objects remain complete",
            "object_materialization": "private copies of packed objects and indexes; read-only original alternates supply any loose historical objects",
        }
        return result


def protect_sources(data: dict, sources: dict, output: Path):
    """Refuse Python writes to original homes and source repositories."""
    roots = [
        Path(data["header"]["source_crew"]).resolve(),
        *(Path(source["root"]).resolve() for source in sources["projects"].values()),
    ]
    allowed = output.resolve()
    state = {"active": True, "refusals": 0}

    def audit(event, arguments):
        if not state["active"]:
            return
        paths = []
        if event == "open" and not isinstance(arguments[0], int):
            mode, flags = arguments[1:3]
            if (
                isinstance(mode, str) and any(char in mode for char in "wa+")
            ) or flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                paths = [arguments[0]]
        elif event in {
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.rename",
            "os.chmod",
            "os.chown",
            "os.truncate",
        }:
            paths = arguments[:2] if event == "os.rename" else arguments[:1]
        for raw in paths:
            if not isinstance(raw, (str, bytes, os.PathLike)):
                continue
            path = Path(os.fsdecode(raw)).absolute()
            if path != allowed and any(
                path == root or root in path.parents for root in roots
            ):
                state["refusals"] += 1
                raise PermissionError(
                    f"replay refuses a write to original source {path}"
                )

    sys.addaudithook(audit)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", type=Path)
    parser.add_argument("--census", type=Path)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--mounts", type=Path)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--start", default="2026-11-16T21:00:00Z")
    parser.add_argument("--end", default="2026-11-16T22:30:00Z")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.prepare:
        data = json.loads(args.prepare.read_text())
        result = prepare(data, args.output, read_object(args.mounts))
        print(
            json.dumps(
                {
                    "projects": list(result["projects"]),
                    "worker_heads": len(result["heads"]),
                    "extra_artifacts": len(result["extras"]),
                    "sha256": result["sha256"],
                }
            ),
            flush=True,
        )
        return 0
    if args.replay:
        data = json.loads(args.replay.read_text())
        print(json.dumps(data["header"], sort_keys=True), flush=True)
        sources = read_object(args.assets / "sources.json")
        protection = protect_sources(data, sources, args.output)
        try:
            result = replay(
                data, sources=sources, assets=args.assets, timeout=args.timeout
            )
        finally:
            protection["active"] = False
        result["original_source_write_refusals"] = protection["refusals"]
        args.output.write_text(json.dumps(result, sort_keys=True))
        print(
            json.dumps(
                {
                    key: value
                    for key, value in result.items()
                    if key not in {"verdicts_by_request", "reasons_by_request"}
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    if args.collect is None:
        parser.error("supply --collect, --prepare or --replay")
    data = collect(args.collect, args.start, args.end, args.census)
    args.output.write_text(json.dumps(data, sort_keys=True))
    print(json.dumps(data["header"], sort_keys=True), flush=True)
    return 0 if data["rows"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
