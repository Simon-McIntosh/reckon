"""Inspect the instruction and skill context applicable to an agent target."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PROJECT_DOC_MAX_BYTES = 32 * 1024
_PRIMARY_INSTRUCTION_FILES = ("AGENTS.override.md", "AGENTS.md")


@dataclass(frozen=True)
class ContextRequest:
    """Inputs for a target-path context preflight."""

    target: Path
    user_home: Path
    agent: str = "codex"
    agent_root: Path | None = None
    project_doc_max_bytes: int | None = None
    activated_skills: tuple[str, ...] = ()


def build_context_manifest(request: ContextRequest) -> dict[str, Any]:
    """Return a deterministic, content-free manifest for an agent launch target."""

    agent = request.agent.lower()
    if agent not in {"codex", "claude"}:
        raise ValueError(f"unsupported agent: {request.agent}")

    user_home = _absolute(request.user_home)
    agent_root = _absolute(
        request.agent_root or user_home / (".codex" if agent == "codex" else ".claude")
    )
    target = _absolute(request.target)
    target_dir = target if target.is_dir() else target.parent
    findings: list[dict[str, str]] = []

    if not target.exists():
        _finding(findings, "error", "target_missing", target, "Target does not exist.")

    canonical_path = user_home / ".agents" / "AGENTS.md"
    canonical = _file_record(canonical_path, findings, "canonical_policy")
    entrypoint_name = "AGENTS.md" if agent == "codex" else "CLAUDE.md"
    entrypoint_path = agent_root / entrypoint_name
    entrypoint = _file_record(entrypoint_path, findings, "agent_entrypoint")
    entrypoint["relationship"] = _entrypoint_relationship(
        entrypoint_path, canonical_path, canonical, entrypoint, findings
    )

    config = _read_agent_config(agent, agent_root, findings)
    budget_limit, budget_source = _budget_limit(request, config, agent_root)
    fallback_names = _fallback_names(config)

    repo_root = _find_repository_root(target_dir)
    project_chain, scanned_dirs = _project_instruction_chain(
        repo_root, target_dir, fallback_names, findings
    )
    project_bytes = sum(item["bytes"] for item in project_chain)
    remaining = budget_limit - project_bytes
    overflow = remaining < 0
    if overflow:
        _finding(
            findings,
            "error",
            "project_instruction_budget_exceeded",
            target,
            "Applicable project instructions exceed the byte budget by "
            f"{-remaining} bytes.",
        )

    effective_chain: list[dict[str, Any]] = []
    if entrypoint.get("readable"):
        effective_chain.append(
            {
                **entrypoint,
                "scope": "user",
                "loaded_via": str(entrypoint_path),
            }
        )
    effective_chain.extend({**item, "scope": "project"} for item in project_chain)

    metadata_roots = _skill_roots(user_home, agent_root, repo_root, target_dir)
    discovered_skills = _discover_skills(metadata_roots, findings)
    activated_bodies = _activated_skill_bodies(
        request.activated_skills, discovered_skills, findings
    )

    # Every path this build consulted, recorded as it is read so the cache stamp
    # follows the recorded dependencies. New readers must add their input paths.
    # The agent config is included whether or not it exists, so its later
    # appearance is a miss; the scanned directories are included so a new
    # instruction file in one of them is a miss.
    input_paths: set[str] = {
        str(canonical_path),
        str(entrypoint_path),
        str(agent_root / "config.toml"),
    }
    input_paths.update(str(directory) for directory in scanned_dirs)
    input_paths.update(str(item["path"]) for item in project_chain)
    input_paths.update(str(root["path"]) for root in metadata_roots)
    input_paths.update(str(item["path"]) for item in discovered_skills)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "agent": agent,
        "target": str(target),
        "canonical_policy": canonical,
        "entrypoint": entrypoint,
        "repository": {
            "root": str(repo_root) if repo_root else None,
            "instruction_filenames": [
                *_PRIMARY_INSTRUCTION_FILES,
                *fallback_names,
            ],
        },
        "instructions": {
            "project_chain": project_chain,
            "effective_chain": effective_chain,
            "total_bytes": sum(item["bytes"] for item in effective_chain),
        },
        "budget": {
            "limit_bytes": budget_limit,
            "source": budget_source,
            "project_bytes": project_bytes,
            "remaining_bytes": max(0, remaining),
            "overflow_bytes": max(0, -remaining),
            "truncation_risk": overflow,
        },
        "skills": {
            "metadata_roots": metadata_roots,
            "discovered": discovered_skills,
            "activated_bodies": activated_bodies,
        },
        "input_paths": sorted(input_paths),
        "findings": sorted(
            findings,
            key=lambda item: (item["severity"], item["code"], item["path"]),
        ),
    }
    manifest["ok"] = not any(item["severity"] == "error" for item in findings)
    return manifest


def _manifest_file_paths(manifest: Mapping[str, Any]) -> list[str]:
    """Every file or directory a built manifest's content depends on.

    Stamping these lets a second process trust a cached manifest without
    re-reading any of them: a changed instruction file, a skill added to a
    scanned root, or a relocated repository root all move the stamp. The
    manifest's own ``input_paths`` — recorded during the build from the paths it
    actually consulted, the agent config included — leads the set, so the stamp
    tracks the recorded dependencies. Readers must keep recording every file
    they consult; equivalence tests check this dependency contract.
    """

    paths: set[str] = set()
    for value in manifest.get("input_paths") or ():
        if value:
            paths.add(str(value))
    for key in ("canonical_policy", "entrypoint"):
        record = manifest.get(key)
        if isinstance(record, Mapping):
            for field in ("path", "resolved_path"):
                value = record.get(field)
                if value:
                    paths.add(str(value))
    instructions = manifest.get("instructions")
    if isinstance(instructions, Mapping):
        for item in instructions.get("effective_chain") or ():
            if not isinstance(item, Mapping):
                continue
            for field in ("path", "resolved_path", "directory"):
                value = item.get(field)
                if value:
                    paths.add(str(value))
    repository = manifest.get("repository")
    if isinstance(repository, Mapping) and repository.get("root"):
        paths.add(str(repository["root"]))
    skills = manifest.get("skills")
    if isinstance(skills, Mapping):
        for root in skills.get("metadata_roots") or ():
            if isinstance(root, Mapping) and root.get("path"):
                paths.add(str(root["path"]))
        for item in skills.get("discovered") or ():
            if isinstance(item, Mapping):
                for field in ("path", "resolved_path"):
                    value = item.get(field)
                    if value:
                        paths.add(str(value))
    return sorted(paths)


def manifest_file_stamp(manifest: Mapping[str, Any]) -> list[Any]:
    """A cheap stamp of the files a manifest was built from."""

    from reckon import capabilities

    return [
        [path, capabilities.file_stamp(path)] for path in _manifest_file_paths(manifest)
    ]


def cached_context_manifest(request: ContextRequest) -> dict[str, Any]:
    """Return a context manifest, reusing a cached one while its files stand.

    The manifest reads the instruction chain and its skill roots, a walk whose
    repository lookup shells out to git. It is a pure function of those files,
    so a fresh process reuses the cached manifest and re-stamps only the handful
    of paths it names. A changed file, an added skill or a relocated root is a
    miss and the manifest is rebuilt.
    """

    from reckon import capabilities

    request_key = {
        "agent": request.agent.lower(),
        "target": str(_absolute(request.target)),
        "user_home": str(_absolute(request.user_home)),
        "agent_root": str(request.agent_root) if request.agent_root else None,
        "project_doc_max_bytes": request.project_doc_max_bytes,
        "activated_skills": list(request.activated_skills),
    }
    return capabilities.cached_pick_input_stamped(
        "context-manifest-" + _digest(json.dumps(request_key, sort_keys=True).encode()),
        request_key,
        manifest_file_stamp,
        lambda: build_context_manifest(request),
    )


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_record(
    path: Path,
    findings: list[dict[str, str]],
    role: str,
    *,
    missing_is_error: bool = True,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(path),
        "resolved_path": None,
        "exists": path.exists() or path.is_symlink(),
        "readable": False,
        "bytes": 0,
        "sha256": None,
    }
    if not record["exists"]:
        if missing_is_error:
            _finding(
                findings,
                "error",
                f"{role}_missing",
                path,
                f"{role.replace('_', ' ').title()} is missing.",
            )
        return record
    try:
        if path.stat().st_mode & 0o444 == 0:
            raise PermissionError(path)
        data = path.read_bytes()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        _finding(
            findings,
            "error",
            f"{role}_unreadable",
            path,
            f"{role.replace('_', ' ').title()} is unreadable: {type(exc).__name__}.",
        )
        return record
    record.update(
        {
            "resolved_path": str(resolved),
            "readable": True,
            "bytes": len(data),
            "sha256": _digest(data),
        }
    )
    return record


def _entrypoint_relationship(
    entrypoint_path: Path,
    canonical_path: Path,
    canonical: dict[str, Any],
    entrypoint: dict[str, Any],
    findings: list[dict[str, str]],
) -> str:
    if not entrypoint["exists"] or not entrypoint["readable"]:
        return "unresolved"
    if not canonical["readable"]:
        return "canonical-unavailable"
    if entrypoint["resolved_path"] == canonical["resolved_path"]:
        return "canonical-link"
    if entrypoint["sha256"] == canonical["sha256"]:
        _finding(
            findings,
            "warning",
            "entrypoint_independent_copy",
            entrypoint_path,
            "Entrypoint is an independent copy and can become stale.",
        )
        return "identical-copy"
    if _imports_canonical(entrypoint_path, canonical_path):
        return "canonical-import"
    _finding(
        findings,
        "error",
        "entrypoint_split_brain",
        entrypoint_path,
        "Entrypoint conflicts with the canonical policy.",
    )
    return "conflicting-copy"


def _imports_canonical(entrypoint_path: Path, canonical_path: Path) -> bool:
    try:
        text = entrypoint_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    references = {
        f"@{canonical_path}",
        "@~/.agents/AGENTS.md",
        f"@{canonical_path.as_posix()}",
    }
    return any(line.strip() in references for line in text.splitlines())


def _read_agent_config(
    agent: str, agent_root: Path, findings: list[dict[str, str]]
) -> dict[str, Any]:
    if agent != "codex":
        return {}
    path = agent_root / "config.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _finding(
            findings,
            "warning",
            "agent_config_unreadable",
            path,
            f"Agent configuration could not be read: {type(exc).__name__}.",
        )
        return {}


def _budget_limit(
    request: ContextRequest, config: dict[str, Any], agent_root: Path
) -> tuple[int, str]:
    if request.project_doc_max_bytes is not None:
        if request.project_doc_max_bytes < 0:
            raise ValueError("project_doc_max_bytes must be non-negative")
        return request.project_doc_max_bytes, "override"
    configured = config.get("project_doc_max_bytes")
    if isinstance(configured, int) and configured >= 0:
        return configured, str(agent_root / "config.toml")
    return DEFAULT_PROJECT_DOC_MAX_BYTES, "default"


def _fallback_names(config: dict[str, Any]) -> list[str]:
    configured = config.get("project_doc_fallback_filenames", [])
    if not isinstance(configured, list):
        return []
    return sorted(
        {
            name
            for name in configured
            if isinstance(name, str)
            and name
            and "/" not in name
            and "\\" not in name
            and name not in _PRIMARY_INSTRUCTION_FILES
        }
    )


def _find_repository_root(target_dir: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(target_dir), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return _absolute(Path(root)) if root else None


def _project_instruction_chain(
    repo_root: Path | None,
    target_dir: Path,
    fallback_names: list[str],
    findings: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[Path]]:
    if repo_root is None:
        return [], []
    try:
        relative = target_dir.relative_to(repo_root)
    except ValueError:
        _finding(
            findings,
            "error",
            "target_outside_repository",
            target_dir,
            "Target is outside the detected repository root.",
        )
        return [], []

    directories = [repo_root]
    current = repo_root
    for part in relative.parts:
        current /= part
        directories.append(current)

    chain: list[dict[str, Any]] = []
    candidates = (*_PRIMARY_INSTRUCTION_FILES, *fallback_names)
    for directory in directories:
        selected = next(
            (
                directory / name
                for name in candidates
                if (directory / name).is_file()
            ),
            None,
        )
        if selected is None:
            continue
        record = _file_record(selected, findings, "project_instruction")
        record["directory"] = str(directory)
        record["selected_name"] = selected.name
        chain.append(record)
    return chain, directories


def _skill_roots(
    user_home: Path,
    agent_root: Path,
    repo_root: Path | None,
    target_dir: Path,
) -> list[dict[str, Any]]:
    candidates: list[tuple[str, Path]] = [
        ("user", user_home / ".agents" / "skills"),
        ("agent", agent_root / "skills"),
    ]
    if repo_root is not None:
        directories = [repo_root]
        try:
            relative = target_dir.relative_to(repo_root)
        except ValueError:
            relative = Path()
        current = repo_root
        for part in relative.parts:
            current /= part
            directories.append(current)
        candidates.extend(
            ("project", directory / ".agents" / "skills")
            for directory in directories
        )

    roots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scope, path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append({"scope": scope, "path": key, "exists": path.is_dir()})
    return roots


def _discover_skills(
    roots: list[dict[str, Any]], findings: list[dict[str, str]]
) -> list[dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    for precedence, root in enumerate(roots):
        if not root["exists"]:
            continue
        root_path = Path(root["path"])
        try:
            skill_files = sorted(root_path.glob("*/SKILL.md"))
        except OSError as exc:
            _finding(
                findings,
                "warning",
                "skill_root_unreadable",
                root_path,
                f"Skill metadata root is unreadable: {type(exc).__name__}.",
            )
            continue
        for skill_path in skill_files:
            record = _file_record(
                skill_path, findings, "skill_metadata", missing_is_error=False
            )
            if not record["readable"]:
                continue
            metadata = _skill_metadata(skill_path)
            name = metadata.get("name") or skill_path.parent.name
            record.update(
                {
                    "name": name,
                    "scope": root["scope"],
                    "metadata_only": True,
                    "_precedence": precedence,
                }
            )
            current = discovered.get(name)
            if current is None or precedence >= current["_precedence"]:
                discovered[name] = record
    result = []
    for name in sorted(discovered):
        record = dict(discovered[name])
        record.pop("_precedence", None)
        result.append(record)
    return result


def _skill_metadata(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key.strip() == "name":
            metadata[key.strip()] = value.strip().strip("'\"")
    return metadata


def _activated_skill_bodies(
    names: Iterable[str],
    discovered: list[dict[str, Any]],
    findings: list[dict[str, str]],
) -> list[dict[str, Any]]:
    by_name = {item["name"]: item for item in discovered}
    activated = []
    for name in sorted(set(names)):
        skill = by_name.get(name)
        if skill is None:
            _finding(
                findings,
                "error",
                "activated_skill_missing",
                Path(name),
                "Explicitly activated skill body was not found.",
            )
            continue
        activated.append(
            {
                "name": name,
                "path": skill["path"],
                "resolved_path": skill["resolved_path"],
                "bytes": skill["bytes"],
                "sha256": skill["sha256"],
            }
        )
    return activated


def _finding(
    findings: list[dict[str, str]],
    severity: str,
    code: str,
    path: Path,
    message: str,
) -> None:
    findings.append(
        {
            "severity": severity,
            "code": code,
            "path": str(path),
            "message": message,
        }
    )
