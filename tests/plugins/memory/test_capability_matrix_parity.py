"""KR-2 ST2 — Parity guard for the C2 capability matrix mirror.

The Python mirror at ``plugins/memory/isokron/capability_matrix_mirror.py``
must match the TS source of truth at
``packages/sea-mcp-server/src/capability-matrix.ts`` exactly for the
Kora column. This test parses the TS file at test time and asserts
parity in BOTH directions — any drift fails the test.

[kora.isokron.todo] C2 INTERIM — replaced by direct MCP call when K-7
(Sea MCP capability-row tool) lands. Until then this test is the only
guard against silent drift.

# Locating the TS source

The TS file lives in the IsoKron substrate repo (cloned alongside
Kora). Resolution order:

1. ``KORA_ISOKRON_REPO`` env var pointing at the substrate root.
2. Sibling repo paths (e.g. ``../isokron``).
3. ``~/code/kora-research/isokron*`` directories.

If none resolve, the test marks itself ``skipped`` with a clear
message asking the operator to clone + point the env var. CI must
clone the substrate and set the env var; otherwise this guard is
silently disabled.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import pytest

from plugins.memory.isokron.capability_matrix_mirror import (
    ACTOR_CAPABILITY_MATRIX_KORA_COLUMN,
    KORA_BROADER_CAPABILITIES_KORA_COLUMN,
    SEA_CAPABILITIES_KORA_COLUMN,
)


CAPABILITY_MATRIX_REL_PATH = (
    "packages/sea-mcp-server/src/capability-matrix.ts"
)


def _candidate_repo_roots() -> list[Path]:
    """Build the search list for the substrate repo root."""
    candidates: list[Path] = []
    env_path = os.environ.get("KORA_ISOKRON_REPO")
    if env_path:
        candidates.append(Path(env_path).expanduser())

    # Sibling layout — Kora repo is checked out at ~/code/kora-runtime,
    # IsoKron substrate at ~/code/kora-research/isokron*.
    home = Path.home()
    candidates.extend(
        [
            home / "code" / "kora-research" / "isokron",
            home / "code" / "kora-research" / "isokron-cc1-kora-K1",
            home / "code" / "kora-research" / "isokron-cc1-kora-K2",
            home / "code" / "kora-research" / "isokron-cc1-kora-K3",
            home / "code" / "kora-research" / "isokron-cc2-kora-KF2",
        ]
    )

    # Relative-to-Kora-repo fallback (matches mcp_endpoint example
    # `stdio://node ../isokron/...` in config.py docs).
    repo_root = Path(__file__).resolve().parents[3]  # tests/plugins/memory/.. → repo
    candidates.append(repo_root.parent / "isokron")
    return candidates


def _locate_ts_source() -> Optional[Path]:
    for root in _candidate_repo_roots():
        candidate = root / CAPABILITY_MATRIX_REL_PATH
        if candidate.is_file():
            return candidate
    return None


def _parse_kora_column(ts_source: str) -> dict[str, bool]:
    """Extract ``cap_name -> kora_value`` from the TS ACTOR_CAPABILITY_MATRIX.

    Walks the file line by line:
    - Track current capability name when a line matches ``^\\s*cap_[a-z_]+: \\{``
      (the opening of a capability's per-actor block).
    - Within that block, capture ``^\\s*kora:\\s*(true|false)``.
    - Reset on ``},`` (closing the block).

    Only entries that appear inside ``ACTOR_CAPABILITY_MATRIX`` count —
    any other cap definitions in the file (none today, but defensive)
    are ignored by anchoring to the matrix's opening line.
    """
    # Find the matrix block boundaries.
    start_match = re.search(
        r"^\s*export const ACTOR_CAPABILITY_MATRIX[^=]*=\s*\{",
        ts_source,
        re.MULTILINE,
    )
    if not start_match:
        raise AssertionError(
            "ACTOR_CAPABILITY_MATRIX const block not found in TS source — "
            "the TS file format has changed; update the parity test."
        )

    # Find the matching closing brace at column 0 (top-level `};`).
    after_start = ts_source[start_match.end():]
    end_match = re.search(r"^\};", after_start, re.MULTILINE)
    if not end_match:
        raise AssertionError(
            "Could not locate closing `};` for ACTOR_CAPABILITY_MATRIX — "
            "the TS file format has changed; update the parity test."
        )
    matrix_block = after_start[: end_match.start()]

    # cap_ names can contain digits (e.g. ``cap_propose_class2``).
    cap_open_re = re.compile(r"^\s*(cap_[a-z0-9_]+):\s*\{")
    cap_close_re = re.compile(r"^\s*\},")
    kora_line_re = re.compile(r"^\s*kora:\s*(true|false)\b")

    result: dict[str, bool] = {}
    current: Optional[str] = None
    for line in matrix_block.splitlines():
        if current is None:
            m = cap_open_re.match(line)
            if m:
                current = m.group(1)
            continue

        # Inside a capability block.
        if cap_close_re.match(line):
            current = None
            continue
        km = kora_line_re.match(line)
        if km:
            result[current] = km.group(1) == "true"

    return result


@pytest.fixture
def ts_source_path() -> Path:
    path = _locate_ts_source()
    if path is None:
        pytest.skip(
            "IsoKron substrate repo not located. Set KORA_ISOKRON_REPO to "
            "the repo root (containing packages/sea-mcp-server/src/"
            "capability-matrix.ts), or clone the substrate at "
            "~/code/kora-research/isokron. The C2 mirror is unguarded "
            "when this test is skipped — CI MUST clone + set the env var."
        )
    return path


def test_python_mirror_matches_ts_source_for_every_kora_column_value(
    ts_source_path,
):
    """Every cap in the TS ACTOR_CAPABILITY_MATRIX has the same kora-value here.

    Fails in either direction:
      (a) the TS source adds a cap not in the Python mirror,
      (b) the Python mirror has a cap not in the TS source,
      (c) any cap's kora-value differs.

    On a failure, the diff is included in the error message so the
    operator immediately sees which cap drifted.
    """
    ts_kora = _parse_kora_column(ts_source_path.read_text(encoding="utf-8"))

    py_keys = set(ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.keys())
    ts_keys = set(ts_kora.keys())

    extra_in_py = py_keys - ts_keys
    missing_in_py = ts_keys - py_keys
    assert not extra_in_py, (
        f"Python mirror has capabilities not in TS source: {sorted(extra_in_py)}. "
        f"Either the TS source removed them (delete from mirror) or this is drift."
    )
    assert not missing_in_py, (
        f"TS source has capabilities not in Python mirror: {sorted(missing_in_py)}. "
        f"Add them to capability_matrix_mirror.py with the correct kora-value."
    )

    mismatches: list[tuple[str, bool, bool]] = []
    for cap, ts_value in ts_kora.items():
        py_value = ACTOR_CAPABILITY_MATRIX_KORA_COLUMN[cap]
        if py_value != ts_value:
            mismatches.append((cap, ts_value, py_value))

    assert not mismatches, (
        "Kora-column drift detected — Python mirror disagrees with TS source.\n"
        + "\n".join(
            f"  - {cap}: ts={ts} mirror={py}" for cap, ts, py in mismatches
        )
    )


def test_python_mirror_has_expected_subset_counts():
    """Sanity: 24 SEA + 24 KORA_BROADER = 48 in the combined dict."""
    assert len(SEA_CAPABILITIES_KORA_COLUMN) == 24
    assert len(KORA_BROADER_CAPABILITIES_KORA_COLUMN) == 24
    assert len(ACTOR_CAPABILITY_MATRIX_KORA_COLUMN) == 48
    # No overlap between subsets.
    overlap = set(SEA_CAPABILITIES_KORA_COLUMN) & set(
        KORA_BROADER_CAPABILITIES_KORA_COLUMN
    )
    assert overlap == set(), f"subsets overlap: {overlap}"
