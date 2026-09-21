"""Bounded HTTP reachability for the origin reported by a resolved launcher.

The caller supplies a documented, side-effect-free ``origin_args`` query; its
stdout must be a JSON object containing ``origin``. No flag is guessed: an
unknown flag can start an agent, and an endpoints document cannot establish
which origin a particular executable selects. Without that query the reading
is unknown. The endpoints declaration opts the backend into this check only.

``serving`` means an HTTP success from the requested health path, not model availability,
authentication for inference, or a successful generation. Redirects and HTTP
errors are refusals, with ``answered=True`` distinguishing them from transport
failures. Discovery and HTTP share one wall-clock budget. HTTP runs in a bounded
subprocess so DNS resolution and trickled headers cannot bypass that budget.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def check_serving_origin(
    backend: Mapping[str, Any],
    launcher_path: str | Path,
    *,
    origin_args: Sequence[str] | None = None,
    timeout: float = 5.0,
    probe_path: str = "/v1/models",
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Read the launcher's origin and probe it within ``timeout`` seconds.

    Pass the resolved absolute launcher path without dereferencing symlinks,
    and the same environment, working directory and model selection arguments
    the launch will use. ``origin_args`` must invoke an origin-only query, never
    a worker. Unsupported introspection returns unknown rather than consulting
    another launcher's config or mistaking a published endpoint for this one.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if (
        not probe_path.startswith("/")
        or probe_path.startswith("//")
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in probe_path
        )
    ):
        raise ValueError("probe_path must be an absolute path on the same origin")
    started = time.monotonic()
    path = str(launcher_path)
    origin = None

    def result(
        status: str,
        detail: str,
        *,
        answered: bool | None = None,
        timed_out: bool = False,
        http_status: int | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "launcher_path": path,
            "origin": origin,
            "answered": answered,
            "timeout_seconds": timeout,
            "elapsed_seconds": time.monotonic() - started,
            "timed_out": timed_out,
            "http_status": http_status,
            "probe_path": probe_path,
            "detail": f"launcher {path!r}, origin {origin!r}: {detail}",
        }

    if not backend.get("endpoints_document"):
        return result("not-applicable", "backend declares no endpoints document")
    if not Path(path).is_absolute():
        return result("unknown", "launcher path must already be resolved and absolute")
    if not origin_args:
        return result("unknown", "no safe origin-only launcher query supplied")
    if isinstance(origin_args, (str, bytes)) or any(
        not isinstance(arg, str) for arg in origin_args
    ):
        raise ValueError("origin_args must be an argument vector")

    try:
        discovery = subprocess.run(
            [path, *origin_args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return result("unknown", "origin discovery timed out", timed_out=True)
    except (OSError, UnicodeError) as exc:
        return result("unknown", f"origin discovery failed ({type(exc).__name__})")
    if discovery.returncode:
        return result("unknown", f"origin query exited {discovery.returncode}")
    try:
        origin = _parse_origin(discovery.stdout)
    except (ValueError, TypeError):
        return result("unknown", "origin query did not return a valid HTTP origin")

    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        return result(
            "refusal",
            "timeout exhausted before HTTP probe",
            answered=False,
            timed_out=True,
        )
    try:
        probe = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                str(Path(__file__).absolute()),
                origin + probe_path,
                str(remaining),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return result("refusal", "HTTP probe timed out", answered=False, timed_out=True)
    except (OSError, UnicodeError) as exc:
        return result(
            "refusal", f"HTTP probe failed ({type(exc).__name__})", answered=False
        )
    if probe.returncode:
        return result(
            "refusal", f"HTTP probe exited {probe.returncode}", answered=False
        )
    try:
        reading = json.loads(probe.stdout)
        status = reading["http_status"]
        timed_out = reading["timed_out"]
    except (ValueError, KeyError, TypeError):
        return result(
            "refusal", "HTTP probe returned an invalid reading", answered=False
        )
    if (
        status is not None and (type(status) is not int or not 100 <= status <= 599)
    ) or type(timed_out) is not bool:
        return result(
            "refusal", "HTTP probe returned an invalid reading", answered=False
        )
    if status is None:
        return result(
            "refusal",
            "HTTP probe timed out" if timed_out else "origin did not answer HTTP",
            answered=False,
            timed_out=timed_out,
        )
    return result(
        "serving" if 200 <= status < 300 else "refusal",
        f"origin answered HTTP {status}",
        answered=True,
        http_status=status,
    )


def _parse_origin(stdout: str) -> str:
    payload = json.loads(stdout)
    candidate = payload.get("origin") if isinstance(payload, dict) else None
    if not isinstance(candidate, str) or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in candidate
    ):
        raise ValueError("invalid origin")
    parsed = urllib.parse.urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "?" in candidate
        or "#" in candidate
    ):
        raise ValueError("invalid origin")
    # Access validates the port even when it is not explicitly needed.
    _ = parsed.port
    return f"{parsed.scheme}://{parsed.netloc}"


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_reading(url: str, timeout: float) -> dict[str, Any]:
    """Read only response headers, directly from the named origin."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RejectRedirects()
    )
    try:
        with opener.open(url, timeout=timeout) as response:
            return {"http_status": response.status, "timed_out": False}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {"http_status": exc.code, "timed_out": False}
    except (OSError, ValueError) as exc:
        cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return {"http_status": None, "timed_out": isinstance(cause, TimeoutError)}


if __name__ == "__main__":
    print(json.dumps(_http_reading(sys.argv[1], float(sys.argv[2]))))
