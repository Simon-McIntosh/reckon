"""Request and response helpers for a running fleet supervisor in tests."""

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def wait_for(predicate: Callable[[], Any], timeout: float = 10) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.05)
    raise AssertionError("supervisor did not reach the expected state")


def send_request(runtime: Path, line: str) -> None:
    with (runtime / "requests").open("w", encoding="utf-8") as fifo:
        fifo.write(line + "\n")


def ready_response(runtime: Path, state: Path, token: str) -> dict[str, Any]:
    path = state / "migration" / f"ready-{token}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    send_request(runtime, f"ready {token}")
    return wait_for(lambda: json.loads(path.read_text()) if path.exists() else None)
