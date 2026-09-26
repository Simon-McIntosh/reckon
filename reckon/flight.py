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
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

# The plan-review gate's mode. Its polarity is the opposite of the evidence
# gates' own gate key: an evidence gate ships strict and is relaxed by naming a
# weaker mode, while the plan-review gate ships advisory and is strengthened by
# naming ``enforce``. One key carrying both polarities would be read backwards
# by whoever configured the other, so the two are separate.
PLAN_REVIEW_GATE_KEY = "plan_review_gate"
PLAN_REVIEW_GATE_MODES = ("report", "enforce")
PLAN_REVIEW_GATE_DEFAULT = "report"


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


def _validate_plan_review_gate(data: Mapping[str, Any], source: str | Path) -> None:
    """Check the plan-review gate mode, which the generated schema does not carry.

    The key is checked here rather than by adding a property to the LinkML source
    and regenerating the model from it: the generated artifact is not hand-edited,
    and the mode is a two-word enum whose default polarity already differs from
    the gate key the schema does carry. A value outside the declared modes is
    refused naming the file and the key, as any schema violation is.
    """
    if PLAN_REVIEW_GATE_KEY not in data:
        return
    value = data[PLAN_REVIEW_GATE_KEY]
    if value not in PLAN_REVIEW_GATE_MODES:
        raise FlightConfigError(
            source,
            PLAN_REVIEW_GATE_KEY,
            "must be one of " + ", ".join(PLAN_REVIEW_GATE_MODES),
        )


def plan_review_gate_enforces(config: Mapping[str, Any] | None) -> bool:
    """Whether a resolved config selects the plan-review gate's enforced mode.

    Absence is the report-only default rather than a missing fact, because the
    shipped default layer names it and a config assembled by hand — a test, an
    in-process caller — should still report rather than refuse.
    """
    mode = (config or {}).get(PLAN_REVIEW_GATE_KEY) or PLAN_REVIEW_GATE_DEFAULT
    return str(mode) == "enforce"


def placement_for(backend: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return this backend's declared placement, or None when it declares none.

    Absence is not an empty placement: a backend declaring one is launched
    inside the scheduler invocation it names, and a backend declaring none is
    launched exactly as it always has been, as a child of whoever started the
    coordinator.
    """
    if not isinstance(backend, Mapping):
        return None
    placement = backend.get("placement")
    if not isinstance(placement, Mapping) or not placement:
        return None
    return dict(placement)


def placement_requirement_entries(
    placement: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return the requirement set a placement declares, in declaration order.

    Each entry names one thing the placement's workers need visible where they
    land: a filesystem path or a network endpoint. A placement declaring none
    requires nothing, which is the shape every configuration had before the
    field existed.
    """
    if not isinstance(placement, Mapping):
        return []
    entries = placement.get("requirements")
    if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes)):
        return []
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


def placement_scheduler_queries(
    placement: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    """The state and reason queries a placement declares for its own wrapper.

    Read from the declaration rather than from a table keyed on the wrapper's
    name, so which reporting verb answers a given scheduler is configuration.
    A key that is absent or empty is left out rather than recorded as an empty
    answer, so a caller can tell a placement that says how to ask from one that
    does not.
    """
    declared: dict[str, list[str]] = {}
    if not isinstance(placement, Mapping):
        return declared
    for key in ("state_query", "reason_query"):
        value = placement.get(key)
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
            continue
        tokens = [str(token) for token in value if str(token)]
        if tokens:
            declared[key] = tokens
    return declared


def validate_layer(data: Mapping[str, Any], source: str | Path) -> None:
    """Schema-check one layer, raising FlightConfigError on the first violation.

    Every layer is partial — a host config setting one backend's model is
    complete in itself — so this checks shape, types, enums, ranges, patterns and
    unknown keys, and leaves whole-config rules to :func:`_validate_resolved`.

    The one key the generated schema does not carry, the plan-review gate's
    mode, is held out of the schema check and validated just below, so the
    generated model stays the artifact its source produces.
    """
    from pydantic import ValidationError

    from reckon._flight_schema import FlightConfig

    schema_layer = {
        key: value for key, value in data.items() if key != PLAN_REVIEW_GATE_KEY
    }
    try:
        FlightConfig.model_validate(_inject_map_keys(schema_layer))
    except ValidationError as exc:
        first = exc.errors()[0]
        key_path = ".".join(str(part) for part in first.get("loc", ()))
        raise FlightConfigError(
            source, key_path, first.get("msg", "is invalid")
        ) from exc

    _validate_plan_review_gate(data, source)

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
    for backend_name, backend in backends.items():
        if not isinstance(backend, Mapping):
            continue
        placement = backend.get("placement")
        if not isinstance(placement, Mapping):
            continue
        for entry in placement.get("requirements") or ():
            if not isinstance(entry, Mapping):
                continue
            named = [field for field in ("path", "endpoint") if entry.get(field)]
            if len(named) != 1:
                raise FlightConfigError(
                    sources,
                    f"backends.{backend_name}.placement.requirements"
                    f".{entry.get('name')}",
                    "must declare exactly one of path and endpoint",
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


# ── Input modalities ────────────────────────────────────────────────────────

#: Input modalities a named profile requires of the lane that serves it.
#:
#: The profile names what the work needs from its lane, so eligibility comes
#: from the work rather than from whoever picks a backend and hopes. The text
#: profile requires nothing: every lane receives a prompt, so demanding a
#: declaration for it would refuse every backend whose layer predates the slot.
#: A profile that needs a modality beyond the prompt lists it here, and a lane
#: that does not declare it cannot serve that profile.
PROFILE_INPUT_MODALITIES: dict[str, tuple[str, ...]] = {
    "text": (),
    "figure": ("image",),
}


def input_modalities_required(profile: str) -> tuple[str, ...]:
    """Return the input modalities a named profile requires of its lane."""
    try:
        return PROFILE_INPUT_MODALITIES[profile]
    except KeyError:
        known = ", ".join(sorted(PROFILE_INPUT_MODALITIES)) or "none"
        raise FlightConfigError(
            "<profile>",
            "profile",
            f"names profile '{profile}', which declares no input modalities "
            f"(known profiles: {known})",
        ) from None


def declared_input_modalities(
    config: Mapping[str, Any], backend: str
) -> tuple[str, ...]:
    """Return the input modalities a backend declares, empty when none are.

    Absence is read as no declared modality rather than as every modality: a
    lane that does not say it receives an image is treated as not receiving
    one, which is what turns a silent wrong verdict into a refusal.
    """
    backends = config.get("backends") or {}
    entry = backends.get(backend) or {}
    declared = entry.get("input_modalities") or []
    return tuple(str(modality) for modality in declared)


def missing_input_modalities(
    config: Mapping[str, Any], backend: str, profile: str
) -> tuple[str, ...]:
    """Return the modalities ``profile`` requires that ``backend`` lacks."""
    declared = declared_input_modalities(config, backend)
    return tuple(
        modality
        for modality in input_modalities_required(profile)
        if modality not in declared
    )


def select_profile_backend(
    config: Mapping[str, Any],
    profile: str,
    *,
    backend: str | None = None,
) -> ResolvedConfig:
    """Return a dispatch overlay selecting the lane that can serve ``profile``.

    ``backend`` names the lane the caller would use; absent, the resolved
    ``default_backend`` is the candidate. Either way the candidate is checked
    against the modalities the profile requires, and one that does not declare
    them is refused by name.

    The refusal is terminal. No other backend is tried and ``default_backend``
    is never substituted for a named candidate, because the substitution is not
    a downgrade of cost or speed: a lane without image input still answers, with
    a well-formed verdict formed from the filename and the diff, and the review
    record then reports a judgement its author could not have made. A profile
    that needs a capability can therefore only be served by a lane declaring
    it, or not served at all.
    """
    backends = config.get("backends") or {}
    candidate = str(backend or config.get("default_backend") or "").strip()

    if not candidate:
        raise FlightConfigError(
            "<resolved flight>",
            "default_backend",
            f"must name the backend that serves a '{profile}' profile, and no "
            "layer declares one",
        )
    if candidate not in backends:
        known = ", ".join(sorted(backends)) or "none"
        raise FlightConfigError(
            "<resolved flight>",
            "backends",
            f"names backend '{candidate}', which no layer defines "
            f"(defined backends: {known})",
        )

    missing = missing_input_modalities(config, candidate, profile)
    if missing:
        declared = declared_input_modalities(config, candidate)
        declared_text = ", ".join(declared) or "none"
        missing_text = ", ".join(missing)
        raise FlightConfigError(
            "<resolved flight>",
            f"backends.{candidate}.input_modalities",
            f"backend '{candidate}' cannot serve a '{profile}' profile: it "
            f"does not declare {missing_text} input (declared input modalities: "
            f"{declared_text}). Declare the modality on a lane whose model "
            "actually receives it; no fallback to another backend is taken, "
            "because a lane that does not receive the image returns a "
            "well-formed verdict formed from the filename and the diff",
        )

    return ResolvedConfig(
        deep_merge(config, {"default_backend": candidate}),
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

    ``command_found`` reports only that the launcher exists on PATH. A backend
    can also declare ``endpoints_document`` — a document publishing the endpoints
    its lane currently serves — and ``serving`` is read from that document, so a
    lane whose wrapper exists but lists no live endpoint is distinguishable from
    one that can actually serve. A backend may further declare ``lane_document``
    — the document its lane publishes with the reading of its own state,
    headroom and binding observation — and that reading is reported beside the
    serving verdict.
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
                **_probe_serving(backend),
                **_probe_lane_document(backend),
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
        entry.update(_probe_serving(backend))
        entry.update(_probe_lane_document(backend))
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


def _probe_serving(backend: Mapping[str, Any]) -> dict[str, Any]:
    """Report whether a backend's lane can serve, read from its declared document.

    A backend declaring no ``endpoints_document`` reports ``unknown``: absence of
    a declaration is not evidence the lane is down. The document is a local JSON
    file listing the live endpoints; it is read directly and never over the
    network. An empty list reports ``not-serving``. A document listing at least
    one endpoint whose ``model_id`` equals the backend's configured ``model``
    reports ``serving``; the same document whose listed models match none of the
    configured model reports ``mismatch``, so a lane that is up but serving the
    wrong checkpoint is told apart from one that is down. A document that is
    missing, unreadable or unparsable reports ``unknown`` rather than a guess.
    """
    document = backend.get("endpoints_document")
    if not document:
        return {
            "serving": "unknown",
            "serving_detail": "backend declares no endpoints document",
        }
    path = Path(str(document)).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "serving": "unknown",
            "serving_detail": f"endpoints document {str(path)!r} cannot be read — {exc}",
        }
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return {
            "serving": "unknown",
            "serving_detail": (
                f"endpoints document {str(path)!r} is not valid JSON — {exc}"
            ),
        }
    endpoints = payload.get("endpoints") if isinstance(payload, Mapping) else None
    if not isinstance(endpoints, list):
        return {
            "serving": "unknown",
            "serving_detail": (
                f"endpoints document {str(path)!r} has no 'endpoints' list"
            ),
        }
    if not endpoints:
        return {
            "serving": "not-serving",
            "serving_detail": f"endpoints document {str(path)!r} lists no endpoints",
        }
    configured_model = backend.get("model")
    offered = [
        endpoint.get("model_id") if isinstance(endpoint, Mapping) else None
        for endpoint in endpoints
    ]
    if configured_model and configured_model in offered:
        return {
            "serving": "serving",
            "serving_detail": (
                f"endpoints document {str(path)!r} lists {len(endpoints)} endpoint(s), "
                f"serving configured model {configured_model!r}"
            ),
        }
    served = ", ".join(str(model) for model in offered if model is not None)
    return {
        "serving": "mismatch",
        "serving_detail": (
            f"endpoints document {str(path)!r} lists {len(endpoints)} endpoint(s), "
            f"none serving configured model {configured_model!r}; "
            f"offered: {served or '<endpoints carry no model id>'}"
        ),
    }


def _observation_age(stamp: object) -> float | None:
    """Return seconds since an observation stamp, or None if it cannot be aged.

    Both an epoch-seconds number and an ISO-8601 string are accepted; a string
    without an explicit zone is read as UTC. Anything else — a missing stamp, an
    unparsable one — returns None, so an undated reading is never silently
    treated as current.
    """
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        return max(0.0, time.time() - float(stamp))
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - parsed).total_seconds())


def _probe_lane_document(backend: Mapping[str, Any]) -> dict[str, Any]:
    """Report a backend's lane reading, taken from its declared lane document.

    A backend declaring no ``lane_document`` reports ``unknown``: absence of a
    declaration is not evidence the lane is down. The document is a local JSON
    file a serving lane publishes, carrying the lane's measured ``state``, its
    remaining ``headroom``, whether a worker binding was ``binding_observed``,
    the ``observed_at`` stamp the reading was taken at, and the
    ``suggested_shelf_life_seconds`` the lane itself suggests the reading stays
    fresh for. It is read directly and never over the network.

    A reading older than its own suggested shelf life reports ``lane_state:
    "unknown"`` while keeping the measured figure and its age, so a reader can
    see both what was measured and how stale it is. A document that is missing,
    unreadable, unparsable, not an object, or carries no recognizable state or
    stamp reports ``unknown`` with a reason and never raises: absence of a
    signal is not evidence of a healthy lane. A measured zero headroom stays a
    number and stays distinct from the ``"unknown"`` an unavailable lane reports.
    """
    base: dict[str, Any] = {
        "lane_state": "unknown",
        "lane_headroom": "unknown",
        "lane_binding_observed": None,
        "lane_age_seconds": None,
        "lane_shelf_life_seconds": None,
        "lane_detail": "",
    }
    document = backend.get("lane_document")
    if not document:
        return {**base, "lane_detail": "backend declares no lane document"}
    path = Path(str(document)).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            **base,
            "lane_detail": f"lane document {str(path)!r} cannot be read — {exc}",
        }
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return {
            **base,
            "lane_detail": f"lane document {str(path)!r} is not valid JSON — {exc}",
        }
    if not isinstance(payload, Mapping):
        return {
            **base,
            "lane_detail": f"lane document {str(path)!r} does not hold a JSON object",
        }

    state_raw = payload.get("state")
    state_verdict = str(state_raw).strip() if state_raw is not None else ""
    headroom = payload.get("headroom")
    if not isinstance(headroom, (int, float)) or isinstance(headroom, bool):
        headroom = "unknown"
    binding = payload.get("binding_observed")
    if not isinstance(binding, bool):
        binding = None
    shelf_life = payload.get("suggested_shelf_life_seconds")
    shelf_life = (
        float(shelf_life)
        if isinstance(shelf_life, (int, float)) and not isinstance(shelf_life, bool)
        else None
    )
    age = _observation_age(payload.get("observed_at"))

    report = {
        "lane_state": state_verdict or "unknown",
        "lane_headroom": headroom,
        "lane_binding_observed": binding,
        "lane_age_seconds": age,
        "lane_shelf_life_seconds": shelf_life,
        "lane_detail": "",
    }
    if not state_verdict:
        report["lane_state"] = "unknown"
        report["lane_detail"] = (
            f"lane document {str(path)!r} carries no recognizable state"
        )
    elif age is None:
        report["lane_state"] = "unknown"
        report["lane_detail"] = (
            f"lane document {str(path)!r} carries no observation stamp; "
            "an undated reading cannot describe the present"
        )
    elif shelf_life is not None and age > shelf_life:
        report["lane_state"] = "unknown"
        report["lane_detail"] = (
            f"lane document {str(path)!r} reading is {age:.0f}s old, older than "
            f"its own {shelf_life:.0f}s suggested shelf life"
        )
    else:
        report["lane_detail"] = (
            f"lane document {str(path)!r} reports state {state_verdict!r}"
        )
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


def configured_backends_without_meteredness(
    config: Mapping[str, Any],
) -> list[str]:
    """Return configured backend names lacking a boolean meteredness value."""
    backends = config.get("backends")
    if not isinstance(backends, Mapping):
        return []
    return sorted(
        str(name)
        for name, backend in backends.items()
        if not isinstance(backend, Mapping)
        or not isinstance(backend.get("metered"), bool)
    )


def flight_report(
    project: str | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    probe_auth: bool = False,
    portfolio_sources: Mapping[str, Any] | None = None,
    **resolve_kwargs: Any,
) -> dict[str, Any]:
    """Build the whole machine-readable answer: config, provenance, availability.

    The portfolio project key (``*``) selects no project layer and no single
    config to resolve, so it answers with :func:`portfolio_report` instead:
    one row per mounted project, ranked by uncovered critical hours.
    ``portfolio_sources`` is the test seam carrying the mounts, live pointers
    and readers that composition consumes.
    """
    if project == PORTFOLIO_PROJECT:
        return {
            "project": PORTFOLIO_PROJECT,
            "portfolio": portfolio_report(**(portfolio_sources or {})),
        }
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
        "undeclared_meteredness": configured_backends_without_meteredness(
            resolved.config
        ),
        "warnings": list(resolved.warnings),
    }


# ── Portfolio ───────────────────────────────────────────────────────────────

# The cross-project selector, shared with the roadmap's portfolio read: a
# project argument of ``*`` means every mounted project rather than a project
# actually named ``*``.
PORTFOLIO_PROJECT = "*"

# The row keys in fixed order, so the table's columns are one list rather than
# a smaller convention each reader restates.
PORTFOLIO_COLUMNS = (
    "project",
    "pushed_sprint",
    "critical_path",
    "coverage",
    "uncovered_critical_hours",
    "live_width",
    "live_width_by_role",
    "unreconciled_runs",
    "lane_headroom",
    "lane_reading_age_seconds",
)


def _project_roadmap(project: str, docs_dir: Path) -> dict[str, Any]:
    """Build one mounted project's roadmap, restoring each sprint's theme.

    Sprint rows carry membership and progress but not the sprint's theme, and
    the portfolio names the pushed sprint by both. The theme is stamped here,
    where the discovered sprint resources are already in hand, rather than
    re-discovered by every reader that wants one.
    """
    from reckon._store import read_plan
    from reckon.roadmap import build_roadmap
    from reckon.serve import discover_plans

    repo_root = docs_dir.parent
    discovered = discover_plans(docs_dir, project, docs_dir / "state")
    index_data, _index_version = read_plan(project, "index", repo_root)
    project_rows = index_data.get("projects") or []
    roadmap = build_roadmap(
        project,
        list(discovered.get("inventory") or []),
        list(discovered.get("sprints") or []),
        active_sprint_id=(
            discovered.get("active_sprint_id") or index_data.get("active_sprint_id")
        ),
        project_manifest=(
            project_rows[0]
            if project_rows and isinstance(project_rows[0], dict)
            else {}
        ),
    )
    themes = {
        str(sprint.get("id")): str(sprint.get("theme") or "")
        for sprint in discovered.get("sprints") or []
        if isinstance(sprint, Mapping) and sprint.get("id")
    }
    for row in roadmap.get("sprints") or []:
        if isinstance(row, dict):
            row.setdefault("theme", themes.get(str(row.get("id")), ""))
    return roadmap


def _project_lane_reading(project: str, docs_dir: Path) -> dict[str, Any]:
    """Report the project's newest lane reading: its headroom and its age.

    Each backend's lane document is read fresh and one row is reported, because
    pairing the newest age with a different reading's headroom would report a
    figure no lane currently reports. A project that declares no readable lane
    document reports ``unknown`` with a null age rather than a stale figure:
    absence of a reading is not a healthy lane.
    """
    try:
        config = resolve(project, checkout_path=docs_dir.parent).config
    except FlightConfigError:
        return {"lane_headroom": "unknown", "lane_reading_age_seconds": None}
    readings = [
        _probe_lane_document(backend)
        for backend in (config.get("backends") or {}).values()
        if isinstance(backend, Mapping)
    ]
    aged = []
    for reading in readings:
        age = reading.get("lane_age_seconds")
        if isinstance(age, (int, float)) and not isinstance(age, bool):
            aged.append((float(age), reading))
    if not aged:
        return {"lane_headroom": "unknown", "lane_reading_age_seconds": None}
    age_seconds, newest = min(aged, key=lambda item: item[0])
    return {
        "lane_headroom": newest.get("lane_headroom", "unknown"),
        "lane_reading_age_seconds": round(age_seconds),
    }


def _path_coverage(
    path_plans: list[str],
    length_hours: float,
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], float]:
    """Count the live runs standing on a critical path, and the hours left bare.

    A path plan is covered when at least one run whose process is alive targets
    it. The uncovered fraction of the path's elapsed length is the hours no
    worker currently stands on, which is what ranks one project above another.
    Liveness comes from the pointer's own classification, never from transition
    events: a working run rewrites its manifest only when it finishes, so an
    event-based reading calls a whole live fleet absent.
    """
    live_by_plan: dict[str, list[str]] = {}
    for row in rows:
        if row.get("process_alive") is not True:
            continue
        plan = str(row.get("plan") or "")
        if plan:
            live_by_plan.setdefault(plan, []).append(str(row.get("run_id") or ""))
    total = len(path_plans)
    covered_plans = [plan for plan in path_plans if live_by_plan.get(plan, [])]
    runs = [
        {"run_id": run_id, "plan": plan}
        for plan in path_plans
        for run_id in live_by_plan.get(plan, [])
    ]
    covered_fraction = len(covered_plans) / total if total else 1.0
    coverage = {
        "plans": total,
        "covered_plans": len(covered_plans),
        "covered_fraction": round(covered_fraction, 4),
        "live_runs": len(runs),
        "runs": runs,
    }
    return coverage, round(length_hours * (1.0 - covered_fraction), 3)


def _portfolio_live_rows(
    pointers: Iterable[Mapping[str, Any]],
    classifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Classify the live pointers and group the rows by project.

    Classification is the liveness authority, and it is host-aware: a pid is
    meaningful only on the machine that issued it, so a pointer launched
    elsewhere carries its stored answer as unproven rather than having its
    process table read from here. Only the fields the portfolio reads are kept,
    so a caller cannot mistake a row for the classifier's whole answer.
    """
    from reckon.crew import recovery as recovery_module

    classify = classifier or recovery_module.classify_pointer
    grouped: dict[str, list[dict[str, Any]]] = {}
    for pointer in pointers:
        if not isinstance(pointer, Mapping):
            continue
        project = str(pointer.get("project") or "")
        if not project:
            continue
        classified = classify(pointer)
        recorded = pointer.get("closure_disposition")
        disposition = (
            str(recorded.get("kind") or "") if isinstance(recorded, Mapping) else ""
        )
        classification = str(classified.get("classification") or "")
        grouped.setdefault(project, []).append(
            {
                "run_id": str(classified.get("run_id") or ""),
                "plan": str(classified.get("plan") or ""),
                "role": str(pointer.get("role") or ""),
                "classification": classification,
                "process_alive": classified.get("process_alive"),
                # A run whose turn is still open is not awaiting anyone's
                # reconciliation, so it is not counted here; anything else that
                # carries no disposition excusing it — including a run that
                # declared ``still-working`` and has since stopped — is the
                # forgotten work this column exists to surface. The closure
                # drain narrows the same predicate to pointers past their grace
                # window and already finished with; the portfolio reports the
                # whole population, so a pointer still inside its grace window
                # is visible here before the drain would name it.
                "unreconciled": classification != "running"
                and not recovery_module.closure_disposition_valid(
                    disposition, classification
                ),
            }
        )
    return grouped


def _portfolio_row(
    project: str,
    rows: list[dict[str, Any]],
    roadmap: Mapping[str, Any],
    lane: Mapping[str, Any],
) -> dict[str, Any]:
    """Reduce one project's roadmap, live pointers and lane reading to one row."""
    critical = roadmap.get("critical_path")
    critical = critical if isinstance(critical, Mapping) else {}
    path_plans = [str(plan) for plan in (critical.get("plans") or [])]
    length_hours = float(critical.get("length_hours") or 0.0)
    coverage, uncovered_hours = _path_coverage(path_plans, length_hours, rows)
    alive = [row for row in rows if row.get("process_alive") is True]
    by_role: dict[str, int] = {}
    for row in alive:
        role = str(row.get("role") or "") or "unspecified"
        by_role[role] = by_role.get(role, 0) + 1
    pushed_id = str(roadmap.get("active_sprint_id") or "")
    theme = ""
    for sprint in roadmap.get("sprints") or []:
        if isinstance(sprint, Mapping) and str(sprint.get("id")) == pushed_id:
            theme = str(sprint.get("theme") or "")
            break
    return {
        "project": project,
        "pushed_sprint": {"id": pushed_id or None, "theme": theme},
        "critical_path": {
            "plans": path_plans,
            "length_hours": length_hours,
            "length_unit": str(critical.get("length_unit") or "elapsed-hours"),
            "worker_hours": float(critical.get("worker_hours") or 0.0),
            "effort_unit": str(critical.get("effort_unit") or ""),
        },
        "coverage": coverage,
        "uncovered_critical_hours": uncovered_hours,
        "live_width": len(alive),
        "live_width_by_role": dict(sorted(by_role.items())),
        "unreconciled_runs": sum(1 for row in rows if row.get("unreconciled")),
        "lane_headroom": lane.get("lane_headroom", "unknown"),
        "lane_reading_age_seconds": lane.get("lane_reading_age_seconds"),
    }


def _read_roadmap(
    project: str,
    docs_dir: Path,
    reader: Callable[[str, Path], Mapping[str, Any]],
) -> tuple[Mapping[str, Any], str]:
    """Read one project's roadmap, reporting a failure instead of raising it.

    One malformed project must not take the whole table down with it, and a
    silent zero is worse than a named gap, so the refusal text is carried on
    the row and repeated in the report's own ``errors`` list where a reader
    scanning the sort order cannot miss it.
    """
    try:
        return reader(project, docs_dir), ""
    except Exception as exc:  # noqa: BLE001 — a bad project is data, not a crash
        return {}, f"{project}: {type(exc).__name__}: {exc}"


def portfolio_report(
    *,
    mounts: Mapping[str, str | Path] | None = None,
    live_pointers: Iterable[Mapping[str, Any]] | None = None,
    roadmap_reader: Callable[[str, Path], Mapping[str, Any]] | None = None,
    lane_reader: Callable[[str, Path], Mapping[str, Any]] | None = None,
    classifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compose one row per mounted project, ranked by uncovered critical hours.

    Rows sort by the pushed sprint's uncovered critical hours descending, so
    the project whose critical path has the most bare hours sits first. The
    figures are read from the roadmap, the live pointers and each project's own
    lane reading; nothing is derived from a transition event, and no state file
    is added.
    """
    mounted = mounted_project_docs() if mounts is None else mounts
    if live_pointers is None:
        from reckon.crew.runs import list_live

        live_pointers = list_live()
    read_roadmap = roadmap_reader or _project_roadmap
    read_lane = lane_reader or _project_lane_reading
    grouped = _portfolio_live_rows(live_pointers, classifier=classifier)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for project in sorted(mounted):
        docs_dir = Path(mounted[project])
        roadmap, roadmap_error = _read_roadmap(project, docs_dir, read_roadmap)
        if roadmap_error:
            errors.append(roadmap_error)
            rows.append(
                {
                    "project": project,
                    "error": roadmap_error,
                    "uncovered_critical_hours": None,
                }
            )
            continue
        rows.append(
            _portfolio_row(
                project,
                grouped.get(project, []),
                roadmap,
                read_lane(project, docs_dir),
            )
        )
    rows.sort(
        key=lambda row: (
            row["uncovered_critical_hours"] is None,
            -(row["uncovered_critical_hours"] or 0.0),
            row["project"],
        )
    )
    return {
        "projects": len(rows),
        "columns": list(PORTFOLIO_COLUMNS),
        "errors": errors,
        "live_width": sum(int(row.get("live_width") or 0) for row in rows),
        "unreconciled_runs": sum(
            int(row.get("unreconciled_runs") or 0) for row in rows
        ),
        "uncovered_critical_hours": round(
            sum(float(row["uncovered_critical_hours"] or 0.0) for row in rows), 3
        ),
        "rows": rows,
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
