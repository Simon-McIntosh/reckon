"""A batch dispatch refuses --dry-run instead of queueing real requests.

On 8 November ``crew dispatch --batch <file> --dry-run`` exited 0, printed
"queued" and wrote a live queued pointer: the batch path ignored the flag. A
batch queues each entry as it reads it, so it refuses the combination before
reading the file or writing anything.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon import cli


def test_a_batch_with_dry_run_is_refused_and_writes_nothing(
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

    assert result.exit_code == 2, result.output
    assert "--dry-run cannot be combined with --batch" in result.output
    live = home / "crew" / "live"
    assert not live.exists() or not any(live.iterdir())
