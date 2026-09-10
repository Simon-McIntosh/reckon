from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module

_SHARED_ASSETS = ("foundation.css", "dashboard.css", "state.js", "badge.svg")


TEMPLATE_TITLE = "reckon · plan system"
TEMPLATE_PROJECT = "reckon"

_TEMPLATE_INDEX = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="{TEMPLATE_PROJECT}">
  <title>{TEMPLATE_TITLE}</title>
  <link rel="stylesheet" href="/_shared/foundation.css">
</head>
<body><div id="root"></div></body>
</html>
"""


def _build_asset_root(root: Path) -> Path:
    """Lay out the minimum tree ``_asset_root`` accepts as a canonical source."""
    (root / "ui").mkdir(parents=True)
    (root / "_shared").mkdir(parents=True)
    (root / "index.html").write_text(_TEMPLATE_INDEX)
    for name in ("shell.jsx", "state-loader.js"):
        (root / "ui" / name).write_text(f"// {name}\n")
    for name in _SHARED_ASSETS:
        (root / "_shared" / name).write_text(f"/* {name} */\n")
    return root


def _invoke_self_sync(tmp_path: Path, docs: Path):
    return CliRunner().invoke(
        cli_module.main,
        [
            "sync",
            str(docs),
            "--project",
            "sample",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "config-state"),
            "--claude-settings",
            str(tmp_path / "settings.json"),
        ],
    )


@pytest.fixture
def canonical_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A docs dir that is simultaneously the asset source and the sync target."""
    docs = _build_asset_root(tmp_path / "checkout" / "docs")
    monkeypatch.setattr(cli_module, "_asset_root", lambda: docs)
    return docs


def test_sync_of_the_asset_source_itself_succeeds(tmp_path: Path, canonical_docs: Path):
    """Syncing the checkout that supplies the assets is a no-op, not a crash.

    The asset root and the destination resolve to one path, and copying a file
    onto itself raises. The command must report the assets as already in place
    and carry on to the remaining scaffold steps.
    """
    result = _invoke_self_sync(tmp_path, canonical_docs)

    assert result.exit_code == 0, result.output
    for name in ("foundation.css", "dashboard.css", "badge.svg"):
        assert f"canonical _shared/{name} — already in place" in result.output
        assert f"copied _shared/{name}" not in result.output


def test_sync_of_the_asset_source_leaves_the_spa_template_as_authored(
    tmp_path: Path, canonical_docs: Path
):
    """The template must not be overwritten by its own rendered output.

    Every project's ``index.html`` is rendered from this one file, with the
    project name substituted into the ``docs-project`` meta and the title. When
    the target *is* the template, writing the render back would bake one
    project's identity into the source the rest inherit.
    """
    index = canonical_docs / "index.html"

    assert _invoke_self_sync(tmp_path, canonical_docs).exit_code == 0

    rendered = index.read_text()
    assert f"<title>{TEMPLATE_TITLE}</title>" in rendered
    assert f'content="{TEMPLATE_PROJECT}"' in rendered
    assert "sample" not in rendered


def test_sync_of_the_asset_source_preserves_asset_content(
    tmp_path: Path, canonical_docs: Path
):
    """The skipped copy must leave every shared asset byte-identical."""
    before = {
        name: (canonical_docs / "_shared" / name).read_bytes()
        for name in _SHARED_ASSETS
    }

    assert _invoke_self_sync(tmp_path, canonical_docs).exit_code == 0

    after = {
        name: (canonical_docs / "_shared" / name).read_bytes()
        for name in _SHARED_ASSETS
    }
    assert after == before


def test_sync_of_the_asset_source_leaves_the_real_checkout_untouched(
    tmp_path: Path, canonical_docs: Path
):
    """Isolation holds for writes, not only for reads.

    The temp tree stands in for the canonical checkout, so the repository's own
    ``docs/_shared`` must be identical before and after. An isolated read does
    not prove an isolated write.
    """
    real_shared = Path(cli_module.__file__).resolve().parent.parent / "docs" / "_shared"
    before = {
        path.name: path.read_bytes()
        for path in sorted(real_shared.iterdir())
        if path.is_file()
    }

    assert _invoke_self_sync(tmp_path, canonical_docs).exit_code == 0

    after = {
        path.name: path.read_bytes()
        for path in sorted(real_shared.iterdir())
        if path.is_file()
    }
    assert after == before
