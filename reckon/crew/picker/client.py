"""Call the typed Decisions endpoint without exposing credentials."""

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

JEV_MODEL = "~typesafe/jev-latest"
DECISIONS_ORIGIN = "https://openrouter.ai"
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


def checkout_root() -> Path:
    """The checkout that supplied this import, which may be a linked worktree."""
    return Path(__file__).resolve().parents[3]


def main_checkout(root: Path) -> Path:
    """Resolve ``root`` to the main checkout when it is a linked worktree.

    A linked worktree carries a ``.git`` file naming its git directory under the
    main checkout; that directory's ``commondir`` file names the shared git
    directory, whose parent is the main checkout. Every other layout — a main
    checkout, an export without ``.git``, a malformed pointer — resolves to
    ``root`` itself.
    """
    marker = root / ".git"
    if not marker.is_file():
        return root
    try:
        pointer = marker.read_text().strip()
    except OSError:
        return root
    if not pointer.startswith("gitdir:"):
        return root
    gitdir = Path(pointer[len("gitdir:") :].strip())
    if not gitdir.is_absolute():
        gitdir = root / gitdir
    try:
        common = (gitdir / "commondir").read_text().strip()
    except OSError:
        return root
    shared = Path(common)
    if not shared.is_absolute():
        shared = gitdir / shared
    return shared.resolve().parent


def credential_path() -> Path:
    """Resolve the tool's environment file independently of the target project.

    The file is the main checkout's ``.env``: a follower, service or MCP server
    may import reckon from a linked worktree, and a worktree carries no key.
    """
    override = os.environ.get(CREDENTIAL_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (main_checkout(checkout_root())) / ".env"


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
