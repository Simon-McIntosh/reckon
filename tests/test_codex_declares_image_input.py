"""The codex lane declares image input in the project flight layer.

The figure review profile requires a lane that declares ``image`` input; a lane
that does not is refused by name rather than served with a verdict its author
could not have formed. The codex lane's declaration is what admits it to that
profile, and a layer edit that quietly drops the line would restore the
well-formed-but-wrong-review path the modality check exists to close. This test
pins the declaration against the repository's own project layer.

  - the repository's own ``docs/state/reckon/flight.yaml`` resolves with the
    host layer pointed at an empty file, so nothing on the developer's machine
    or in a mounted layer can supply the value;
  - the resolved codex backend declares the ``image`` modality the figure
    profile requires.

The head half is also the negative control's subject: an armed run points
``RECKON_PROJECT_FLIGHT_LAYER`` at a scratch copy of the layer whose
``input_modalities`` line has been removed, and the assertion then fails.
"""

from __future__ import annotations

import os
from pathlib import Path

from reckon import flight

ROOT = Path(__file__).resolve().parent.parent

#: The repository's own project flight layer, the semantic authority for the
#: codex lane's declaration.
PROJECT_LAYER = ROOT / "docs" / "state" / "reckon" / "flight.yaml"

#: When set, the test resolves this layer file instead of the repository's own
#: layer. The negative control's armed run points it at a scratch copy with the
#: ``input_modalities`` line removed, so the same test runs and fails.
PROJECT_LAYER_OVERRIDE = "RECKON_PROJECT_FLIGHT_LAYER"


def layer_path() -> Path:
    """Return the layer under test, honouring the negative control's override."""
    override = os.environ.get(PROJECT_LAYER_OVERRIDE)
    return Path(override) if override else PROJECT_LAYER


def codex_declared_input_modalities(path: Path, tmp_path: Path) -> tuple[str, ...]:
    """Resolve ``path`` with an empty host layer and return codex's modalities.

    The host layer is an empty file inside the test's temporary directory, so
    the resolved value comes from the repository's own layers alone.
    """
    empty_host = tmp_path / "empty-host.yaml"
    empty_host.write_text("")
    resolved = flight.resolve(project_path=path, host_path=empty_host)
    return flight.declared_input_modalities(resolved.config, "codex")


def test_codex_lane_declares_the_image_modality(tmp_path):
    declared = codex_declared_input_modalities(layer_path(), tmp_path)

    assert "image" in declared
