"""Flight control — layered, schema-validated worker-routing configuration.

One surface tunes worker routing, gate strictness and worker fences for every
project, and the current prompt can always deviate from it. Four layers resolve
upward, each overriding the one below:

    shipped   reckon/schema/flight-defaults.yaml, in the wheel
    host      <config-home>/flight.yaml
    project   <repo>/docs/state/<project>/flight.yaml
    override  values supplied by the caller for this task

Merging is per-key and deep, so a project overriding one backend's model does
not restate the rest of that backend. Every resolved leaf carries the name of
the layer that supplied it: a value whose origin is invisible cannot be tuned,
and with defaults shipped as a real layer rather than as model field defaults,
"the shipped value" and "nobody set this" stay distinguishable.

A malformed layer raises :class:`FlightConfigError` naming the file, the key
path and the violated constraint. There is no silent fallback to defaults —
misconfigured routing that looks like it worked is worse than a stopped run.

Backend names, commands, model identifiers and effort levels are user data read
from a config file's values. Neither this module nor the schema enumerates any
of them.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from reckon._store import _config_home

# Layer names, lowest precedence first. The order is the merge order.
LAYER_ORDER = ("shipped", "host", "project", "override")

# Maps whose keys are user-chosen names rather than schema-fixed keys. Their
# entries are inlined objects whose identifier slot is the map key.
_KEYED_MAPS = ("backends", "roles")

_AUTH_PROBE_TIMEOUT_SECONDS = 10
_CATALOG_PROBE_TIMEOUT_SECONDS = 10
_ENVIRONMENT_REFERENCE = re.compile(r"\$\{([^{}]+)\}")


class FlightConfigError(Exception):
    """A flight config layer is malformed.

    Carries the three facts needed to fix it without guessing: which file, which
    key path inside that file, and which constraint the value broke.
    """

    def __init__(
        self,
        source: str | Path,
        key_path: str,
        constraint: str,
    ) -> None:
        self.source = str(source)
        self.key_path = key_path
        self.constraint = constraint
        location = f"{self.source}: {key_path}" if key_path else str(self.source)
        super().__init__(f"{location}: {constraint}")


@dataclass(frozen=True)
class LayerSource:
    """Where one resolution layer came from and whether it contributed."""

    name: str
    path: str | None
    present: bool


@dataclass
class ResolvedFlight:
    """A merged flight config plus the origin of every value in it."""

    config: dict[str, Any]
    provenance: dict[str, str]
    layers: list[LayerSource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def origin(self, key_path: str) -> str | None:
        """Return the layer that supplied ``key_path``, or None if unset."""
        return self.provenance.get(key_path)


class ResolvedConfig(dict[str, Any]):
    """Resolved values carrying compatibility warnings for runtime callers."""

    def __init__(self, values: Mapping[str, Any], *, warnings: Iterable[str] = ()):
        super().__init__(values)
        self.warnings = tuple(warnings)


# ── Paths ───────────────────────────────────────────────────────────────────


def shipped_defaults_path() -> Path:
    """Path to the defaults shipped inside the package."""
    return Path(__file__).resolve().parent / "schema" / "flight-defaults.yaml"


def schema_source_path() -> Path:
    """Path to the LinkML source the committed artifacts derive from."""
    return Path(__file__).resolve().parent / "schema" / "flight.yaml"


def host_config_path() -> Path:
    """Path to this workstation's flight config.

    Honours RECKON_FLIGHT_CONFIG so a test or a one-off run can point at a
    different host layer without touching the real one.
    """
    env = os.environ.get("RECKON_FLIGHT_CONFIG")
    if env:
        return Path(env).expanduser()
    return _config_home() / "flight.yaml"


def project_config_path(project: str, checkout_path: str | Path | None = None) -> Path:
    """Path to a project's flight config inside its own checkout.

    ``checkout_path`` is the repo root containing ``docs/``; without it the
    project layer is resolved through the registered mount, matching how the
    rest of reckon reaches a project's state directory.
    """
    if checkout_path is not None:
        docs_root = Path(checkout_path).expanduser() / "docs"
    else:
        docs_root = _project_docs_root(project)
    return docs_root / "state" / project / "flight.yaml"


def mounted_project_docs() -> dict[str, Path]:
    """Return every registered project's resolved docs directory.

    Mount registration is the host's repository-authority boundary.  Callers
    making a write decision need the lossless set, not the state-directory
    fallback used when an optional project flight layer is absent.  Invalid
    mount data therefore fails closed and names the offending entry.
    """
    from reckon._store import _mounts_path

    import json

    mounts_file = _mounts_path()
    if not mounts_file.is_file():
        return {}
    try:
        payload = json.loads(mounts_file.read_text())
    except (OSError, ValueError) as exc:
        raise FlightConfigError(
            mounts_file, "", "must contain a readable JSON object"
        ) from exc
    if not isinstance(payload, Mapping):
        raise FlightConfigError(mounts_file, "", "must contain a JSON object")
    entries = payload.get("mounts") if "mounts" in payload else payload
    if not isinstance(entries, Mapping):
        raise FlightConfigError(
            mounts_file, "mounts", "must be an object keyed by project"
        )

    resolved: dict[str, Path] = {}
    for project, entry in sorted(entries.items(), key=lambda item: str(item[0])):
        if isinstance(entry, Mapping):
            entry = entry.get("docs") or entry.get("path")
        if not isinstance(entry, str) or not entry.strip():
            raise FlightConfigError(
                mounts_file,
                str(project),
                "must name a docs directory with a string path",
            )
        resolved[str(project)] = Path(entry).expanduser().resolve()
    return resolved


def _project_docs_root(project: str) -> Path:
    """Resolve a mounted project's docs directory, or its state symlink."""
    from reckon._store import _state_root

    try:
        entry = mounted_project_docs().get(project)
    except FlightConfigError:
        entry = None
    if entry is not None:
        return entry
    # No mount: the state root is symlinked into the repo, so its parent of the
    # project directory is the same docs/state tree the mount would have named.
    return _state_root().parent


# ── Loading and validation ──────────────────────────────────────────────────


def _require_yaml():
    """Import PyYAML, turning a missing runtime dependency into a clear error."""
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - packaging failure
        raise FlightConfigError(
            "<runtime>",
            "",
            "PyYAML is required to read flight configuration",
        ) from exc
    return yaml


def read_layer_file(path: str | Path) -> dict[str, Any]:
    """Parse one YAML layer, returning {} when the file does not exist.

    An absent layer is normal — most installs have no project layer. A file that
    exists but does not parse, or does not hold a mapping, is an error.
    """
    yaml = _require_yaml()
    path = Path(path)
    if not path.exists():
        return {}
    text = _read_text(path)
    raw = _load_yaml_unique_keys(text, path, yaml)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise FlightConfigError(
            path,
            "",
            f"must hold a mapping at the top level, found {type(raw).__name__}",
        )
    return dict(raw)


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise FlightConfigError(path, "", f"cannot be read — {exc}") from exc


def _load_yaml_unique_keys(raw: str, source: str | Path, yaml) -> Any:
    """Parse a layer, refusing any mapping that repeats a key within one scope.

    ``yaml.safe_load`` silently keeps the last of two identical mapping keys,
    which dropped an entire backend definition (its ``budget_check`` and a quiet
    time-budget change) in the measured incident. That collision cannot be seen
    after the fact — the parser has already discarded the earlier definition —
    so it is caught here, while the raw mapping is being constructed, and named
    with the key's dotted path through the file.
    """
    from yaml.nodes import MappingNode
    from yaml.resolver import BaseResolver

    node_paths: dict[int, str] = {}

    class _UniqueKeyLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader: _UniqueKeyLoader, node, deep: bool = False):
        parent_path = node_paths.get(id(node), "")
        seen: set = set()
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            key_str = str(key)
            key_path = f"{parent_path}.{key_str}" if parent_path else key_str
            if key in seen:
                raise FlightConfigError(
                    source,
                    key_path,
                    f"defines key '{key_str}' more than once; YAML keeps only "
                    "the last, silently dropping the earlier definition",
                )
            seen.add(key)
            if isinstance(value_node, MappingNode):
                node_paths[id(value_node)] = key_path
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    _UniqueKeyLoader.add_constructor(
        BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
    )
    try:
        return yaml.load(raw, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise FlightConfigError(source, "", f"not valid YAML — {exc}") from exc


def _inject_map_keys(data: Mapping[str, Any]) -> dict[str, Any]:
    """Copy ``data`` with each keyed-map entry carrying its key as ``name``.

    The schema models backends and roles as inlined objects identified by
    ``name``; on the wire the map key is that identifier, so it is supplied here
    rather than written twice in every config file.
    """
    out = copy.deepcopy(dict(data))
    for map_name in _KEYED_MAPS:
        entries = out.get(map_name)
        if not isinstance(entries, Mapping):
            continue
        rebuilt: dict[str, Any] = {}
        for key, value in entries.items():
            if value is None:
                value = {}
            if isinstance(value, Mapping):
                value = {**value, "name": key}
            rebuilt[key] = value
        out[map_name] = rebuilt
    return out


def _ignore_removed_backend_keys(
    data: Mapping[str, Any], source: str | Path
) -> tuple[dict[str, Any], list[str]]:
    """Drop retired backend declarations while reporting each compatibility read."""
    migrated = copy.deepcopy(dict(data))
    warnings: list[str] = []
    backends = migrated.get("backends")
    if not isinstance(backends, Mapping):
        return migrated, warnings
    for name, settings in backends.items():
        if not isinstance(settings, dict) or "concurrency" not in settings:
            continue
        settings.pop("concurrency")
        warnings.append(
            f"{source}: backends.{name}.concurrency is retired and was ignored; "
            "the crew roster is the concurrency authority"
        )
    return migrated, warnings


def validate_layer(data: Mapping[str, Any], source: str | Path) -> None:
    """Schema-check one layer, raising FlightConfigError on the first violation.

    Every layer is partial — a host config setting one backend's model is
    complete in itself — so this checks shape, types, enums, ranges, patterns and
    unknown keys, and leaves whole-config rules to :func:`_validate_resolved`.
    """
    from pydantic import ValidationError

    from reckon._flight_schema import FlightConfig

    try:
        FlightConfig.model_validate(_inject_map_keys(data))
    except ValidationError as exc:
        first = exc.errors()[0]
        key_path = ".".join(str(part) for part in first.get("loc", ()))
        raise FlightConfigError(
            source, key_path, first.get("msg", "is invalid")
        ) from exc

    for backend_name, backend in (data.get("backends") or {}).items():
        if not isinstance(backend, Mapping):
            continue
        if "environment" in backend:
            environment = backend["environment"]
            key_path = f"backends.{backend_name}.environment"
            if not isinstance(environment, Mapping):
                raise FlightConfigError(source, key_path, "must be a mapping")
            for variable, value in environment.items():
                if not isinstance(variable, str) or not variable:
                    raise FlightConfigError(
                        source, key_path, "variable names must be non-empty strings"
                    )
                if not isinstance(value, str):
                    raise FlightConfigError(
                        source,
                        f"{key_path}.{variable}",
                        "must be a string",
                    )
        catalog = backend.get("catalog")
        if isinstance(catalog, Mapping):
            pattern = catalog.get("model_pattern")
            if isinstance(pattern, str) and "{model}" not in pattern:
                raise FlightConfigError(
                    source,
                    f"backends.{backend_name}.catalog.model_pattern",
                    "must contain the {model} placeholder",
                )
            if isinstance(pattern, str):
                try:
                    re.compile(pattern.replace("{model}", "model"))
                except re.error as exc:
                    raise FlightConfigError(
                        source,
                        f"backends.{backend_name}.catalog.model_pattern",
                        f"must be a valid regular expression — {exc}",
                    ) from exc


def _validate_resolved(config: Mapping[str, Any], sources: str) -> None:
    """Check the rules that only a fully merged config can be judged against."""
    backends = config.get("backends") or {}
    default_backend = config.get("default_backend")
    if default_backend and default_backend not in backends:
        known = ", ".join(sorted(backends)) or "none"
        raise FlightConfigError(
            sources,
            "default_backend",
            f"names backend '{default_backend}', which no layer defines "
            f"(defined backends: {known})",
        )
    local_backend = config.get("local_backend")
    if local_backend and local_backend not in backends:
        known = ", ".join(sorted(backends)) or "none"
        raise FlightConfigError(
            sources,
            "local_backend",
            f"names backend '{local_backend}', which no layer defines "
            f"(defined backends: {known})",
        )
    for backend_name, backend in backends.items():
        if not isinstance(backend, Mapping):
            continue
        catalog = backend.get("catalog")
        if not isinstance(catalog, Mapping):
            continue
        for catalog_field in ("list_command", "model_pattern"):
            if not catalog.get(catalog_field):
                raise FlightConfigError(
                    sources,
                    f"backends.{backend_name}.catalog.{catalog_field}",
                    "is required when catalog is declared",
                )
    for role_name, role in (config.get("roles") or {}).items():
        if not isinstance(role, Mapping):
            continue
        backend_name = role.get("backend") or default_backend
        if backend_name and backend_name not in backends:
            known = ", ".join(sorted(backends)) or "none"
            raise FlightConfigError(
                sources,
                f"roles.{role_name}.backend",
                f"names backend '{backend_name}', which no layer defines "
                f"(defined backends: {known})",
            )
        by_spec_level = role.get("by_spec_level") or {}
        if isinstance(by_spec_level, Mapping):
            for level, overlay in by_spec_level.items():
                if not isinstance(overlay, Mapping):
                    continue
                overlay_backend = overlay.get("backend")
                if overlay_backend and overlay_backend not in backends:
                    known = ", ".join(sorted(backends)) or "none"
                    raise FlightConfigError(
                        sources,
                        f"roles.{role_name}.by_spec_level.{level}.backend",
                        f"names backend '{overlay_backend}', which no layer defines "
                        f"(defined backends: {known})",
                    )
        backend = backends.get(backend_name) if backend_name else None
        if not isinstance(backend, Mapping):
            continue
        sandbox = role.get("sandbox") or backend.get("sandbox")
        execution_capable = role.get("execution_capable")
        if execution_capable is True and sandbox in {None, "read-only"}:
            raise FlightConfigError(
                sources,
                f"roles.{role_name}.sandbox",
                "an execution-capable role requires a sandbox that permits "
                "worktree writes",
            )
        if sandbox == "read-only" and execution_capable is not False:
            raise FlightConfigError(
                sources,
                f"roles.{role_name}.execution_capable",
                "a read-only sandbox is reserved for roles explicitly declared "
                "non-execution-capable",
            )


# ── Merge and provenance ────────────────────────────────────────────────────


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` onto ``base`` key by key, recursing into mappings.

    Mappings merge; everything else replaces. That distinction is the whole
    point of the layering: overriding one backend's model must leave that
    backend's other keys standing, while a list-valued key like ``summary.at``
    is a single choice and replaces wholesale.
    """
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _record_provenance(
    data: Mapping[str, Any],
    layer: str,
    provenance: dict[str, str],
    prefix: str = "",
) -> None:
    """Stamp ``layer`` onto every leaf key path present in ``data``."""
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            _record_provenance(value, layer, provenance, prefix=f"{path}.")
        else:
            provenance[path] = layer


def _sorted(value: Any) -> Any:
    """Return ``value`` with every mapping key ordered, recursively.

    Deterministic ordering is a contract of the machine output: an agent
    diffing two runs must see a change only where a value changed.
    """
    if isinstance(value, Mapping):
        return {key: _sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted(item) for item in value]
    return value


def unresolved_environment_references(
    backend: Mapping[str, Any],
) -> list[tuple[str, str]]:
    """Return configured variable names paired with missing references."""
    unresolved: list[tuple[str, str]] = []
    environment = backend.get("environment")
    if not isinstance(environment, Mapping):
        return unresolved
    for variable, raw_value in environment.items():
        for match in _ENVIRONMENT_REFERENCE.finditer(str(raw_value)):
            referenced = match.group(1)
            if referenced not in os.environ:
                unresolved.append((str(variable), referenced))
    return unresolved


def expand_backend_environment(
    backend_name: str,
    backend: Mapping[str, Any],
    *,
    source: str = "<resolved flight>",
) -> dict[str, str]:
    """Expand one selected backend's environment or raise a typed error."""
    environment = backend.get("environment")
    if not isinstance(environment, Mapping):
        return {}
    expanded: dict[str, str] = {}
    for variable, raw_value in environment.items():
        key_path = f"backends.{backend_name}.environment.{variable}"

        def replace_reference(
            match: re.Match[str], *, resolved_key_path: str = key_path
        ) -> str:
            referenced = match.group(1)
            if referenced not in os.environ:
                raise FlightConfigError(
                    source,
                    resolved_key_path,
                    f"references unset environment variable {referenced!r}",
                )
            return os.environ[referenced]

        expanded[str(variable)] = _ENVIRONMENT_REFERENCE.sub(
            replace_reference, str(raw_value)
        )
    return expanded


def resolve(
    project: str | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    host_path: str | Path | None = None,
    project_path: str | Path | None = None,
    checkout_path: str | Path | None = None,
    shipped_path: str | Path | None = None,
) -> ResolvedFlight:
    """Resolve the four layers into one config plus per-key provenance.

    ``project`` selects the project layer; without it only shipped, host and
    override contribute. ``overrides`` is the prompt layer — the runtime choice
    for the current task, which always wins.
    """
    shipped_file = Path(shipped_path) if shipped_path else shipped_defaults_path()
    host_file = Path(host_path) if host_path else host_config_path()

    project_file: Path | None = None
    if project_path is not None:
        project_file = Path(project_path)
    elif project:
        project_file = project_config_path(project, checkout_path)

    candidates: list[tuple[str, Path | None, Mapping[str, Any] | None]] = [
        ("shipped", shipped_file, None),
        ("host", host_file, None),
        ("project", project_file, None),
        ("override", None, overrides),
    ]

    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    layers: list[LayerSource] = []
    contributing: list[str] = []
    warnings: list[str] = []

    for name, path, inline in candidates:
        if inline is not None:
            data = dict(inline)
            source: str | Path = "<override>"
        elif path is None:
            layers.append(LayerSource(name=name, path=None, present=False))
            continue
        else:
            data = read_layer_file(path)
            source = path
        present = bool(data)
        layers.append(
            LayerSource(
                name=name,
                path=None if path is None else str(path),
                present=present,
            )
        )
        if not present:
            continue
        data, compatibility_warnings = _ignore_removed_backend_keys(data, source)
        warnings.extend(compatibility_warnings)
        validate_layer(data, source)
        merged = deep_merge(merged, data)
        _record_provenance(data, name, provenance)
        contributing.append(str(source))

    if not merged:
        raise FlightConfigError(
            shipped_file, "", "shipped defaults are missing or empty"
        )

    _validate_resolved(merged, " + ".join(contributing))
    return ResolvedFlight(
        config=ResolvedConfig(_sorted(merged), warnings=warnings),
        provenance=dict(sorted(provenance.items())),
        layers=layers,
        warnings=warnings,
    )


def select_local_backend(config: Mapping[str, Any]) -> ResolvedConfig:
    """Return one dispatch overlay selecting the declared local backend."""
    local_backend = str(config.get("local_backend") or "").strip()
    if not local_backend:
        raise FlightConfigError(
            "<resolved flight>",
            "local_backend",
            "must be set before `reckon crew dispatch --local` can route work",
        )
    backends = config.get("backends") or {}
    if local_backend not in backends:
        known = ", ".join(sorted(backends)) or "none"
        raise FlightConfigError(
            "<resolved flight>",
            "local_backend",
            f"names backend '{local_backend}', which no layer defines "
            f"(defined backends: {known})",
        )
    return ResolvedConfig(
        deep_merge(config, {"default_backend": local_backend}),
        warnings=getattr(config, "warnings", ()),
    )


# ── Availability ────────────────────────────────────────────────────────────


def probe_availability(
    config: Mapping[str, Any],
    *,
    probe_auth: bool = False,
) -> dict[str, dict[str, Any]]:
    """Report whether each `cli` backend is on PATH and appears authenticated.

    Reported, never acted on: a backend that is configured but missing stays in
    the resolved config, and the caller decides whether to degrade to another
    one. Silently rerouting would hide exactly the misconfiguration this is for.

    Authentication is only knowable per provider, and reckon knows no providers,
    so it runs the backend's own ``auth_check`` argument vector — user data —
    and reports its exit status. That spawns a process, so it happens only when
    asked for; otherwise authentication is reported as unprobed.
    """
    report: dict[str, dict[str, Any]] = {}
    for name, backend in sorted((config.get("backends") or {}).items()):
        if not isinstance(backend, Mapping):
            continue
        launch = backend.get("launch")
        if launch != "cli":
            report[name] = {
                "launch": launch,
                "command": None,
                "command_found": True,
                "command_path": None,
                "authenticated": None,
                "detail": "in-harness backend needs no external command",
            }
            continue
        command = backend.get("command")
        located = shutil.which(command) if command else None
        entry: dict[str, Any] = {
            "launch": launch,
            "command": command,
            "command_found": located is not None,
            "command_path": located,
            "authenticated": None,
            "detail": "",
        }
        unresolved = unresolved_environment_references(backend)
        if unresolved:
            variable, referenced = unresolved[0]
            entry["detail"] = (
                f"environment variable {variable!r} references unset "
                f"dispatcher variable {referenced!r}"
            )
        elif not command:
            entry["detail"] = "backend declares launch: cli but no command"
        elif located is None:
            entry["detail"] = f"'{command}' is not on PATH"
        else:
            entry.update(_probe_auth(backend, probe_auth=probe_auth))
            entry.update(_probe_catalog(backend))
        report[name] = entry
    return report


def _probe_catalog(backend: Mapping[str, Any]) -> dict[str, Any]:
    """Report whether the configured model appears in a declared catalog."""
    catalog = backend.get("catalog")
    if not isinstance(catalog, Mapping):
        return {}
    model = str(backend.get("model") or "")
    command = catalog.get("list_command")
    pattern = catalog.get("model_pattern")
    if not model:
        return {
            "model_served": False,
            "detail": "catalog declared but backend has no configured model",
        }
    if not command or not pattern:
        return {
            "model_served": False,
            "detail": "catalog declaration requires list_command and model_pattern",
        }
    try:
        result = subprocess.run(
            [str(part) for part in command],
            capture_output=True,
            text=True,
            timeout=_CATALOG_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "model_served": False,
            "detail": f"catalog failed to run for model {model!r} — {exc}",
        }
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0:
        offered = " | ".join(lines) or "<no catalog output>"
        return {
            "model_served": False,
            "detail": (
                f"catalog exited {result.returncode} for model {model!r}; "
                f"catalog offered: {offered}"
            ),
        }
    matcher = re.compile(str(pattern).replace("{model}", re.escape(model)))
    matched = next((line for line in lines if matcher.search(line)), None)
    if matched is not None:
        return {
            "model_served": True,
            "detail": f"model {model!r} matched catalog line: {matched}",
        }
    offered = " | ".join(lines) or "<no catalog output>"
    return {
        "model_served": False,
        "detail": f"model {model!r} is not served; catalog offered: {offered}",
    }


def _probe_auth(
    backend: Mapping[str, Any],
    *,
    probe_auth: bool,
) -> dict[str, Any]:
    """Run a backend's declared auth check, or explain why it was not run."""
    check = backend.get("auth_check")
    if not check:
        return {"authenticated": None, "detail": "no auth_check declared"}
    if not probe_auth:
        return {"authenticated": None, "detail": "auth_check not run"}
    try:
        result = subprocess.run(
            [str(part) for part in check],
            capture_output=True,
            text=True,
            timeout=_AUTH_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"authenticated": False, "detail": f"auth_check failed to run — {exc}"}
    if result.returncode == 0:
        return {"authenticated": True, "detail": "auth_check exited 0"}
    return {
        "authenticated": False,
        "detail": f"auth_check exited {result.returncode}",
    }


def flight_report(
    project: str | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    probe_auth: bool = False,
    **resolve_kwargs: Any,
) -> dict[str, Any]:
    """Build the whole machine-readable answer: config, provenance, availability."""
    resolved = resolve(project, overrides=overrides, **resolve_kwargs)
    return {
        "availability": probe_availability(resolved.config, probe_auth=probe_auth),
        "config": resolved.config,
        "layers": [
            {"name": layer.name, "path": layer.path, "present": layer.present}
            for layer in resolved.layers
        ],
        "project": project,
        "provenance": resolved.provenance,
        "warnings": list(resolved.warnings),
    }


def parse_overrides(pairs: Iterable[str]) -> dict[str, Any]:
    """Turn ``dotted.key=value`` strings into a nested override layer.

    Values are parsed as YAML scalars so that ``session_reuse=true`` is a
    boolean rather than a string the schema would reject.
    """
    yaml = _require_yaml()
    overrides: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            raise FlightConfigError(
                "<override>", pair, "must be written as dotted.key=value"
            )
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise FlightConfigError("<override>", key, f"unparsable value — {exc}")
        cursor = overrides
        parts = [part for part in key.strip().split(".") if part]
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = value
    return overrides
