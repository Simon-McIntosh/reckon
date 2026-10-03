"""Call the typed Decisions endpoint without exposing credentials."""

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

JEV_MODEL = "typesafe/jev-1.13"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
TIMEOUT_SECONDS = 10

# Names the credential file to read instead of the checkout's own ``.env``. A
# caller that must not reach the live service points this at a path holding no
# credential, so resolution fails deterministically without a request.
CREDENTIAL_ENV = "RECKON_PICKER_CREDENTIAL"


class LiveJevDisabledError(RuntimeError):
    """The live Jev service was not called, so no request was sent.

    Raised where a request would otherwise be built, so no connection is opened
    and no credential is spent. The picker records this as its fallback reason
    and dispatch continues along deterministic routing.
    """


def credential_path() -> Path:
    """Resolve the tool's environment file independently of the target project."""
    override = os.environ.get(CREDENTIAL_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path(__file__).resolve().parents[3] / ".env").resolve()


def load_key(env_path: Path) -> str:
    value = os.environ.get("OPENROUTER_API_KEY_RECKON", "").strip()
    if value:
        return value
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            key, separator, value = line.removeprefix("export ").partition("=")
            if separator and key.strip() == "OPENROUTER_API_KEY_RECKON":
                value = value.strip().strip("\"'")
                if value:
                    return value
    raise LiveJevDisabledError(
        "live Jev is disabled: OPENROUTER_API_KEY_RECKON is absent from the "
        f"environment and from {env_path}"
    )


def ask(
    state: dict[str, Any], questions: dict[str, Any], *, env_path: Path
) -> dict[str, Any]:
    request = urllib.request.Request(
        DECISIONS_URL,
        data=json.dumps(
            {"model": JEV_MODEL, "state": state, "questions": questions}
        ).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {load_key(env_path)}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise TypeError("Jev response is not an object")
    return payload
