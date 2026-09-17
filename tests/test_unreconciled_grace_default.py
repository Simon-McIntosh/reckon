"""The shipped unreconciled-run grace and the schema artifacts that declare it.

Three properties, each with the refusal it prevents:

  - the resolved default is nine hundred seconds when no layer overrides it.
    The measured promotion beat is two to six minutes, so a five-minute grace
    is shorter than the task it exists to accommodate and the refusal fired on
    a coordinator verifying and promoting a run — the job, not a backlog.
  - an explicit override still wins, so the grace stays a tunable rather than
    a constant compiled into the surface.
  - the generated declarations agree with the LinkML source, so an edit landed
    against the source without regenerating its artifacts is caught here
    rather than by a later reader trusting a stale copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from reckon.crew.node import parse_duration
from reckon.flight import resolve

ROOT = Path(__file__).resolve().parent.parent

SCHEMA_SOURCE = ROOT / "reckon" / "schema" / "flight.yaml"
PYDANTIC_MODEL = ROOT / "reckon" / "_flight_schema.py"
JSON_SCHEMA = ROOT / "docs" / "_shared" / "flight.schema.json"

KEY = "unreconciled_run_grace"
ORIGIN = "fences." + KEY


def _resolved(tmp_path: Path, *, overrides=None, project=None):
    """Resolve with only the shipped layer present, isolated from the host."""
    return resolve(
        overrides=overrides,
        host_path=tmp_path / "absent-host" / "flight.yaml",
        project_path=project or (tmp_path / "absent-project" / "flight.yaml"),
    )


def test_the_shipped_default_is_nine_hundred_seconds(tmp_path):
    """With no layer overriding it, the resolved grace is the shipped fifteen minutes."""
    resolved = _resolved(tmp_path)

    assert parse_duration(resolved.config["fences"][KEY]) == 900
    assert resolved.origin(ORIGIN) == "shipped"


def test_an_explicit_layer_still_overrides_the_default(tmp_path):
    """The grace stays tunable: a project may tighten it, a caller may override it."""
    project = tmp_path / "project" / "flight.yaml"
    project.parent.mkdir(parents=True)
    project.write_text("fences:\n  " + KEY + ": 3m\n")

    from_project = _resolved(tmp_path, project=project)
    assert parse_duration(from_project.config["fences"][KEY]) == 180
    assert from_project.origin(ORIGIN) == "project"

    overridden = _resolved(
        tmp_path, overrides={"fences": {KEY: "90s"}}, project=project
    )
    assert parse_duration(overridden.config["fences"][KEY]) == 90
    assert overridden.origin(ORIGIN) == "override"


def _source_slot() -> dict:
    """The slot declaration the generators read, and the class that carries it."""
    schema = yaml.safe_load(SCHEMA_SOURCE.read_text())
    assert KEY in schema["classes"]["FenceConfig"]["slots"]
    return schema["slots"][KEY]


def test_the_generated_artifacts_agree_with_the_schema_source():
    """A source edit landed without its regeneration shows here as a divergence."""
    slot = _source_slot()

    model = PYDANTIC_MODEL.read_text().split("def pattern_" + KEY, 1)[1]
    model = model.split("def ", 1)[0]
    model_pattern = re.search(r'pattern\s*=\s*re\.compile\(r"([^"]+)"\)', model)
    assert model_pattern is not None, (
        "the generated model declares no pattern for " + KEY
    )
    assert model_pattern.group(1) == slot["pattern"]

    generated = json.loads(JSON_SCHEMA.read_text())
    json_slot = generated["$defs"]["FenceConfig"]["properties"][KEY]
    assert json_slot["pattern"] == slot["pattern"]
    assert json_slot["type"] == ["string", "null"]


def test_the_shipped_default_satisfies_the_declared_format():
    """The value a fresh install runs with is accepted by the schema that declares it."""
    slot = _source_slot()

    shipped = yaml.safe_load(
        (ROOT / "reckon" / "schema" / "flight-defaults.yaml").read_text()
    )
    grace = shipped["fences"][KEY]

    assert re.fullmatch(slot["pattern"], grace)
    assert parse_duration(grace) == 900
