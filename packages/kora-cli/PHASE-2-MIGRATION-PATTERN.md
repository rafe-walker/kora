# Phase 2 migration pattern — per-subcommand carve

**Authored**: CC#3, 2026-05-24
**Status**: Reference doc. Phase 2B/2C buckets (operator task #456 and follow-ons) apply this pattern mechanically.

This doc captures the two extraction styles used in KR-KORA-PIP-RESTRUCTURE-PHASE-2 so subsequent buckets can carve the remaining ~40 subcommands consistently. Phase 2 (Option II per the operator's 2026-05-24 confirmation) carved 7 subcommands as the demonstration set: 2 full extractions + 5 dispatch shims. The same pattern scales to the rest.

---

## The two styles

Every Phase 2 carve falls into one of these two buckets:

### Style A — Full extraction

The handler's implementation file moves bodily out of `kora_cli/` and into `packages/kora-cli/src/kora_cli_pkg/commands/<name>.py`. The wheel ships the actual code; the pip install runs the handler standalone WITHOUT needing the Kora monorepo on the Python path.

**Use when:**
- The handler has **zero `kora_cli.*` imports** (or only imports modules that are themselves extracted in the same bucket).
- The handler's file is **self-contained** in the dependency sense — the only imports are stdlib, third-party packages already in `kora-cli`'s `dependencies =`, or `kora_cli_pkg.*` siblings.
- The handler is **valuable as a standalone surface** — i.e., BYOA agents installing just `kora-cli` would benefit from running the subcommand without the rest of the monorepo.

**Mechanics:**

```bash
git mv kora_cli/<handler>.py packages/kora-cli/src/kora_cli_pkg/commands/<handler>.py
```

Then:

1. **Verify** the moved file's imports are all in-package or stdlib. If any `from kora_cli.X import Y` remains, either:
   - Co-extract `X` (same bucket, if it's also self-contained), OR
   - Refactor the handler to drop the dep (e.g., Phase 2's `version` handler dropped `kora_cli.__version__` in favor of `importlib.metadata.version('kora-cli')`), OR
   - Downgrade to **Style B** (dispatch shim) and defer the full carve.
2. **Rewrite** the handler's signature to `def main(argv: list[str]) -> int` so it's dispatchable by `kora_cli_pkg.main.cli_entrypoint`.
3. **Back-compat shim** at the old path (`kora_cli/<handler>.py`) re-exports `main` from the new location. Mirrors the `kora_control_writer.py` pattern from Phase 1B and the `migrate_hermes_home.py` pattern from this Phase. Pin the explicit symbols the in-tree callers import (don't rely solely on `*`) for IDE jump-to-definition fidelity.
4. **Register** the subcommand name → module name in `kora_cli_pkg/main.py:_PHASE_2_CARVED`.
5. **Test**: add a smoke test in `packages/kora-cli/tests/test_smoke.py` that the dispatch works in a wheel-only venv.

**Phase 2 examples:**
- `migrate_hermes_home` (358 LOC, pure stdlib) — textbook full extraction
- `version` (~30 LOC, rewritten to use `importlib.metadata`) — the "refactor-then-extract" variant

---

### Style B — Dispatch shim

The handler's implementation **stays in `kora_cli/`**. The wheel ships a thin `commands/<name>.py` that lazy-imports `kora_cli.main:main` and routes through it. The pip install requires the Kora monorepo on the Python path — but the entry-point registration + the per-subcommand handler file in the wheel proves out the dispatch contract.

**Use when:**
- The handler imports from **several `kora_cli.*` helpers** (`config`, `colors`, `auth`, `models`, etc.) that aren't yet extracted.
- The handler's **file is large** (>1000 LOC) and **mostly Kora-CLI-internal** in nature — extraction would force a multi-bucket carve of the helper surface alongside it.
- The handler is **operator-only** in practice (not a BYOA-shaped surface that benefits from standalone install).

**Mechanics:**

Write a ~20-LOC handler file:

```python
"""``kora <name>`` subcommand — KR-KORA-PIP-RESTRUCTURE-PHASE-N.

**Dispatch shim**. The <name> handler is X LOC and imports from
Y, Z, ... (kora_cli.* helpers). Full extraction deferred to a
follow-on bucket.
"""
from __future__ import annotations
import sys


def main(argv: list[str]) -> int:
    try:
        from kora_cli.main import main as _in_tree_main
    except ImportError as exc:
        sys.stderr.write(
            f"kora <name>: requires the Kora monorepo on the Python "
            f"path (in-tree ``kora_cli`` not importable: {exc}). "
            f"Dispatch-shim handler — future bucket carves the implementation.\n"
        )
        return 2
    sys.argv = ["kora", "<name>", *argv]
    return int(_in_tree_main() or 0)


__all__ = ["main"]
```

Register in `_PHASE_2_CARVED` the same as Style A.

**Phase 2 examples:**
- `doctor` (1986 LOC, 5+ kora_cli.* deps)
- `setup` (3557 LOC, deepest helper surface)
- `status` (570 LOC, auth/colors/models deps)
- `login`, `logout` (inline in `kora_cli/main.py`, route through `kora_cli.auth`)

---

## Picking the style for a new handler

Use this decision tree:

1. **Run `grep -E '^(from|import) ' kora_cli/<handler>.py | grep -vE 'stdlib...'`** — get the non-stdlib imports.
2. **If zero `kora_cli.*` imports**: Style A. Go.
3. **If 1-2 `kora_cli.*` imports AND those modules are themselves extractable in this bucket**: Style A, co-extract the helpers.
4. **If 1-2 `kora_cli.*` imports AND a quick refactor can drop them** (e.g., swap `kora_cli.__version__` for `importlib.metadata`): Style A with refactor. Document in the handler docstring.
5. **Otherwise**: Style B. Defer the full carve to a follow-on bucket. Note the deferral in the dispatch-shim docstring (cite LOC + dep surface).

The threshold between (4) and (5) is a judgment call. Rule of thumb: if the refactor would take more than 10 minutes, downgrade to Style B and let a future bucket do the deeper carve when there's appetite for the helper-surface work.

---

## Helper-surface carves

Several `kora_cli/` helpers are imported by many handlers (`config`, `colors`, `auth`, `env_loader`, `models`, `nous_subscription`). Carving these enables many Style A extractions to follow:

| Helper | LOC | Imported by (count, ballpark) | Phase 2 status |
|---|---|---|---|
| `kora_cli/colors.py` | small | ~all subcommand handlers | candidate for Phase 2B |
| `kora_cli/config.py` | medium | ~all | candidate for Phase 2B |
| `kora_cli/env_loader.py` | small | doctor, setup, status | candidate for Phase 2B |
| `kora_cli/auth.py` | large | login, logout, status, several MCP/provider commands | candidate for Phase 2C (heavier) |
| `kora_cli/models.py` | medium | doctor, status, setup | candidate for Phase 2B |
| `kora_constants.py` (repo root) | small | doctor, setup, migrate-hermes-home | already-clean; co-extract opportunistically |

The recommended carve order: helpers first (Phase 2B = colors + config + env_loader + models), then handlers that depend on those (Phase 2C = doctor + status + setup), then the auth-coupled set (Phase 2D = login + logout + auth itself).

---

## Pre-dispatch routing in `cli_entrypoint`

The wheel's `cli_entrypoint` does a light pre-dispatch:

1. Look at `argv[1]` (the first positional arg, skipping flags).
2. If it's in `_PHASE_2_CARVED`, lazy-import `kora_cli_pkg.commands.<name>` and call its `main(argv)`.
3. Otherwise fall back to `kora_cli.main:main()` (the in-tree monolith).

When a subcommand graduates from Style B to Style A (a future bucket fully extracts the implementation), the `_PHASE_2_CARVED` entry stays as-is — only the file at `kora_cli_pkg/commands/<name>.py` changes its body from "dispatch shim" to "full implementation". No churn in the routing layer.

When **all** subcommands are carved, the fallback to `kora_cli.main:main()` is deleted and the monolith goes away. That's the long-arc Phase 2 endgame.

---

## What this pattern does NOT solve

- **Argparse argument parsing for carved subcommands**: each handler's `main(argv)` is responsible for parsing its own args (typically via a top-of-file `argparse.ArgumentParser`). Style A handlers OWN their argparse; Style B handlers don't (they delegate to `kora_cli.main`'s existing argparse table).
- **Global flags** (`-m`, `--provider`, etc.): these still go through `kora_cli.main`'s top-level argparse. If a carved Style A handler needs to see a global flag, the dispatch in `_first_positional` would need extension — flag through to a future bucket if it comes up.
- **Interactive default**: bare `kora` (no subcommand) falls through to `kora_cli.main:main`, which routes to interactive chat. The wheel doesn't carve interactive chat.

These limits are intentional for Phase 2. The endgame Phase removes the fallback altogether and the monolith goes with it.
