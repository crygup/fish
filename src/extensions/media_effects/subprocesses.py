"""Consistent subprocess helpers for media probing and rendering."""

from __future__ import annotations

import json
import subprocess
from typing import Any


def run_media_command(
    command: list[str],
    *,
    timeout: int,
    check: bool = True,
    text: bool = False,
    timeout_message: str,
    failure_prefix: str,
) -> subprocess.CompletedProcess[Any]:
    """Run a media subprocess and expose a concise, user-safe error."""

    try:
        return subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=check,
            text=text,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(timeout_message) from error
    except subprocess.CalledProcessError as error:
        stderr = error.stderr
        detail = (
            stderr
            if isinstance(stderr, str)
            else (stderr or b"").decode("utf-8", "replace")
        )
        detail = detail.strip()
        lines = detail.splitlines()
        message = lines[-1] if lines else "ffmpeg could not process that file."
        wrapped = ValueError(f"{failure_prefix}: {message[:300]}")
        # Keep the user-facing exception concise, but preserve the complete
        # tool diagnostic in its traceback. The command error handler sends
        # that traceback to the error webhook, which makes failures such as
        # invalid color metadata or an unsupported codec actionable.
        wrapped.add_note(
            "Media subprocess diagnostics:\n"
            + (detail or "No stderr was returned by the media tool.")
        )
        raise wrapped from error


def probe_media_json(
    path: str,
    *,
    show_entries: str,
    select_streams: str | None = None,
    timeout: int = 15,
    check: bool = True,
) -> dict[str, Any]:
    """Return validated JSON output from ffprobe for a local media file."""

    command = ["ffprobe", "-v", "error"]
    if select_streams:
        command.extend(["-select_streams", select_streams])
    command.extend(["-show_entries", show_entries, "-of", "json", path])
    result = run_media_command(
        command,
        timeout=timeout,
        check=check,
        text=True,
        timeout_message="That file took too long to inspect.",
        failure_prefix="Could not inspect that media",
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise ValueError("Could not inspect that media file.") from error
    if not isinstance(payload, dict):
        raise ValueError("Could not inspect that media file.")
    return payload
