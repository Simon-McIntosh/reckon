"""A reload image carries the model catalogue its own package reads."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from reckon import checkout_trust
from reckon.crew.ticker import MODEL

CATALOGUE_RELATIVE_PATH = "docs/state/reckon/model-catalogue.yaml"
ALIAS = "dsv4.1-flash"
MODEL_IDENTIFIER = "deepseek-v4.1-flash"
BACKEND = "clive"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(root, "add", relative)
    _git(root, "commit", "-qm", "Change source content")


def _catalogue(alias: str) -> str:
    return (
        "version: 1\n"
        "backends:\n"
        f"  {BACKEND}:\n"
        f"    model: {MODEL_IDENTIFIER}\n"
        f'    alias: "{alias}"\n'
    )


def _source(tmp_path: Path) -> Path:
    """A temporary checkout carrying the package, its script and a catalogue."""
    root = tmp_path / "source"
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(
        source / "reckon",
        root / "reckon",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    script = root / "skills/reckon-build/scripts/worktree_fleet.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(source / "skills/reckon-build/scripts/worktree_fleet.py", script)
    catalogue = root / CATALOGUE_RELATIVE_PATH
    catalogue.parent.mkdir(parents=True)
    catalogue.write_text(_catalogue(ALIAS))
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Reload Test")
    _git(root, "config", "user.email", "reload@example.invalid")
    _git(root, "add", "reckon", "skills", "docs")
    _git(root, "commit", "-qm", "Seed source")
    return root


def _host_config(tmp_path: Path) -> Path:
    """A host layer declaring the backend the catalogue fills.

    Written to a temp path and named by ``RECKON_FLIGHT_CONFIG`` so resolution
    reads the operator's own flight configuration from nowhere.
    """
    host = tmp_path / "host-flight.yaml"
    host.write_text(
        "version: 1\n"
        f"default_backend: {BACKEND}\n"
        "backends:\n"
        f"  {BACKEND}:\n"
        "    launch: cli\n"
        "    command: claude\n"
    )
    return host


_READ_IMAGE = r"""
import json
from reckon import checkout_trust, flight
from reckon.crew.routing import _agent_configuration
from reckon.crew.dispatch_peer import _stamp_agent_display
from reckon.crew.ticker import declared_model_aliases, model_cell_width

aliases = declared_model_aliases()
width = model_cell_width(aliases)
backend = flight.resolve().config["backends"].get("clive") or {}
agent = _stamp_agent_display(
    _agent_configuration("clive", backend.get("launch") or "cli", backend), backend
)
print(checkout_trust.__file__)
print(json.dumps({
    "aliases": sorted(aliases),
    "width": width,
    "model": agent.get("model"),
    "alias": agent.get("alias"),
}))
"""


def _read_image(image: Path, cwd: Path, host: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(image)
    environment["RECKON_FLIGHT_CONFIG"] = str(host)
    environment.pop("RECKON_MODEL_CATALOGUE", None)
    environment.pop("RECKON_FOLLOWER_IMAGE_COMMIT", None)
    return subprocess.run(
        [sys.executable, "-c", _READ_IMAGE],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_reloaded_image_carries_the_model_catalogue(
    tmp_path: Path, monkeypatch
) -> None:
    root = _source(tmp_path)
    monkeypatch.setattr(
        checkout_trust, "_committed_tree_cache", lambda: tmp_path / "images"
    )
    if os.environ.get("RECKON_RELOAD_WITHOUT_CATALOGUE") == "1":
        monkeypatch.setattr(
            checkout_trust, "_committed_catalogue_blob", lambda source_root: None
        )
    host = _host_config(tmp_path)
    identity = checkout_trust.source_code_identity(root / "reckon", source_root=root)
    assert identity != _git(root, "rev-parse", "HEAD:reckon")
    image, _, _, pin = checkout_trust.materialize_committed_tree(root, identity)
    os.close(pin)
    assert (image / "reckon/__init__.py").is_file()
    assert (image / "skills").is_dir()

    payload = _payload(_read_image(image, tmp_path, host))
    assert ALIAS in payload["aliases"]
    assert payload["width"] == len(ALIAS)
    assert payload["model"] == MODEL_IDENTIFIER
    assert payload["alias"] == ALIAS
    assert (image / CATALOGUE_RELATIVE_PATH).is_file()

    _commit(root, CATALOGUE_RELATIVE_PATH, _catalogue("dsv4.1-flash-next"))
    changed = checkout_trust.source_code_identity(root / "reckon", source_root=root)
    assert changed != identity
    _commit(root, "NOTES.txt", "outside every carried tree\n")
    assert (
        checkout_trust.source_code_identity(root / "reckon", source_root=root)
        == changed
    )


def test_an_image_without_the_catalogue_reverts_to_the_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    root = _source(tmp_path)
    monkeypatch.setattr(
        checkout_trust, "_committed_tree_cache", lambda: tmp_path / "images"
    )
    monkeypatch.setattr(
        checkout_trust, "_committed_catalogue_blob", lambda source_root: None
    )
    host = _host_config(tmp_path)
    identity = checkout_trust.source_code_identity(root / "reckon", source_root=root)
    image, _, _, pin = checkout_trust.materialize_committed_tree(root, identity)
    os.close(pin)
    assert not (image / "docs").exists()

    payload = _payload(_read_image(image, tmp_path, host))
    assert ALIAS not in payload["aliases"]
    assert payload["width"] == MODEL
    assert payload["model"] is None
    assert payload["alias"] is None
