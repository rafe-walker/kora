"""Kora CLI — unified command-line interface for the Kora runtime.

Provides subcommands for:
- kora chat                 — interactive chat (same as ./kora)
- kora gateway              — run gateway in foreground (HTTP API + messaging adapters)
- kora gateway start        — start gateway service
- kora gateway stop         — stop gateway service
- kora setup                — interactive setup wizard
- kora status               — show status of all components
- kora cron                 — manage cron jobs
- kora mcp serve            — expose Kora's gateway as an MCP server
- kora migrate-hermes-home  — KR-1 ST3 ~/.hermes → ~/.kora migration

Inherited from upstream NousResearch/hermes-agent (MIT). Forked at
commit 5e743559e (release v2026.5.16); see ``cmd_version`` for the
fork provenance string.
"""

import os
import sys

# Kora's own version stream. Independent of the upstream Hermes 0.14.0
# we forked from; reset to 0.1.0 at the start of KR-1 so the runtime
# can version its Kora-specific surface separately from the inherited
# Hermes runtime body.
__version__ = "0.1.0"
__release_date__ = "2026.5.20"

# Upstream provenance — printed by `kora --version` and embedded in
# bug reports / telemetry so a fork-point regression is diagnosable.
__hermes_inherited_version__ = "0.14.0"
__hermes_inherited_release_date__ = "2026.5.16"
__hermes_fork_commit__ = "5e743559e0157df42e0f640cd06d736e898370d0"
__hermes_fork_commit_short__ = "5e743559e"


def _ensure_utf8():
    """Force UTF-8 stdout/stderr on Windows to prevent UnicodeEncodeError.

    Windows services and terminals default to cp1252, which cannot encode
    box-drawing characters used in CLI output. This causes unhandled
    UnicodeEncodeError crashes on gateway startup.
    """
    if sys.platform != "win32":
        return
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            if getattr(stream, "encoding", "").lower().replace("-", "") != "utf8":
                new_stream = open(
                    stream.fileno(), "w", encoding="utf-8",
                    buffering=1, closefd=False,
                )
                setattr(sys, stream_name, new_stream)
        except (AttributeError, OSError):
            pass


_ensure_utf8()
