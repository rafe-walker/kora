"""Constants for the short-circuit sub-plugin.

The bundled phrasebook YAML stays at
``kora_cli/short_circuit/default_slack_dm_phrasebook.yml`` —
phrasebook editor (PR #177) reads/writes it through
``kora_cli.short_circuit`` package data, and moving the YAML
would force a coordinated editor change. The matcher resolves
the YAML through these constants so the locator + the loader
share one source of truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final


# Package that owns the bundled YAML phrasebook. Resolved via
# ``importlib.resources.files(...)`` so this works from both the
# source tree and an installed wheel.
BUNDLED_PHRASEBOOK_PACKAGE: Final[str] = "kora_cli.short_circuit"


# Filename of the bundled default phrasebook (read via
# ``files(BUNDLED_PHRASEBOOK_PACKAGE).joinpath(...).read_text()``).
BUNDLED_PHRASEBOOK_FILENAME: Final[str] = "default_slack_dm_phrasebook.yml"


# Operator-override location relative to ``${KORA_HOME}``. When
# present, takes precedence over the bundled default.
OPERATOR_OVERRIDE_RELATIVE: Final[Path] = Path("phrasebook") / "slack_dm.yml"
