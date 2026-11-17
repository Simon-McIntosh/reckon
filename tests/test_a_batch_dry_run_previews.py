"""A batch dispatch reads --dry-run as a preview, never as a silent launch.

On 8 November ``crew dispatch --batch <file> --dry-run`` exited 0, printed
"queued" and wrote a live queued pointer: the batch path ignored the flag. The
flag now reaches the batch, so the run answers as a preview and writes no
pointer.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon import cli


def test_a_batch_with_dry_run_previews_instead_of_queueing(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            [{"project": "p", "plan": "x", "section": "s1", "node": "n", "goal": "g"}]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--batch",
            str(batch),
            "--session",
            "session-a",
            "--dry-run",
        ],
    )

    # The combination is no longer a usage refusal: the batch answers in its own
    # document, saying what it would store.
    assert result.exit_code != 2, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    live = home / "crew" / "live"
    assert not live.exists() or not any(live.iterdir())
