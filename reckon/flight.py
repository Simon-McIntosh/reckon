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


def _project_docs_root(project: str) -> Path:
    """Resolve a mounted project's docs directory, or its state symlink."""
    from reckon._store import _mounts_path, _state_root

    mounts_file = _mounts_path()
    if mounts_file.exists():
        import json

        try:
            mounts = json.loads(mounts_file.read_text())
        except (OSError, ValueError):
            mounts = {}
        entry = (mounts.get("mounts") or mounts).get(project)
        if isinstance(entry, Mapping):
            entry = entry.get("docs") or entry.get("path")
        if entry:
            return Path(str(entry)).expanduser()
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
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise FlightConfigError(path, "", f"not valid YAML — {exc}") from exc
    except OSError as exc:
        raise FlightConfigError(path, "", f"cannot be read — {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise FlightConfigError(
            path,
            "",
            f"must hold a mapping at the top level, found {type(raw).__name__}",
        )
    return dict(raw)


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
    for role_name, role in (config.get("roles") or {}).items():
        if not isinstance(role, Mapping):
            continue
        backend = role.get("backend")
        if backend and backend not in backends:
            known = ", ".join(sorted(backends)) or "none"
            raise FlightConfigError(
                sources,
                f"roles.{role_name}.backend",
                f"names backend '{backend}', which no layer defines "
                f"(defined backends: {known})",
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
        if not command:
            entry["detail"] = "backend declares launch: cli but no command"
        elif located is None:
            entry["detail"] = f"'{command}' is not on PATH"
        else:
            entry.update(_probe_auth(backend, probe_auth=probe_auth))
        report[name] = entry
    return report


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
