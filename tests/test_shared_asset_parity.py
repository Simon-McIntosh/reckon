from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).parents[1]
CANONICAL_DASHBOARD = ROOT / "docs" / "_shared" / "dashboard.css"
SYNC_DASHBOARD = ROOT / "skills" / "reckon-sync" / "assets" / "dashboard.css"


def _asset_description(path: Path, content: bytes) -> str:
    return (
        f"{path.relative_to(ROOT)}: {len(content.splitlines())} lines, "
        f"sha256={sha256(content).hexdigest()}"
    )


def test_sync_dashboard_asset_matches_canonical_source():
    canonical = CANONICAL_DASHBOARD.read_bytes()
    sync_asset = SYNC_DASHBOARD.read_bytes()

    assert sync_asset == canonical, (
        "dashboard stylesheet drift: docs/_shared/dashboard.css is canonical; "
        f"{_asset_description(CANONICAL_DASHBOARD, canonical)}; "
        f"{_asset_description(SYNC_DASHBOARD, sync_asset)}"
    )
