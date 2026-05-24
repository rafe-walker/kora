# kora-cli

Kora operator command-line interface — Phase 2 of the KR-KORA-PIP-RESTRUCTURE program.

Provides the `kora` console script + the per-subcommand handler surface for the 7 subcommands carved in Phase 2 (Option II per the operator's 2026-05-24 confirmation). Remaining subcommands continue dispatching through the in-tree `kora_cli.main` until later phases finish the carve (operator task #456 — Phase 2B/2C).

## What this is

The pip-installable kora-cli wheel. After install, the `kora` binary lands in your venv's `bin/` directory. The wheel ships:

- **`kora_cli_pkg.main:cli_entrypoint`** — light pre-dispatch routing
- **`kora_cli_pkg.commands.{migrate_hermes_home,version,doctor,status,setup,login,logout}`** — the 7 Phase 2-carved handler files (2 full extractions + 5 dispatch shims)
- **`PHASE-2-MIGRATION-PATTERN.md`** — reference doc Phase 2B/2C buckets apply mechanically

The 40 other subcommands (chat, gateway, daemon, promote, cron, mcp, …) continue dispatching through the in-tree `kora_cli.main:main` via the wheel's pre-dispatch fallback. That fallback requires the Kora monorepo on the Python path — `pip install kora-cli` standalone (without monorepo) provides ONLY the 7 carved subcommands.

## What this is NOT

- This is NOT a complete Kora install. The wheel ships a thin surface; the bulk of Kora's runtime + handlers live in the monorepo (`kora_cli/`, `agent/`, `plugins/`).
- This is NOT a substitute for the Kora monorepo as a development environment. Operators developing on Kora itself work in the monorepo + use the top-level `pyproject.toml`'s `kora = "kora_cli.main:main"` script.

## Installation

### Source-only path (today)

```bash
# 1. Clone Hermes (source-only Phase 1 dependency per §7.1 closure).
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent && pip install -e .

# 2. Install isokron-client + kora-runtime (Phase 1 packages).
cd /path/to/kora
pip install ./packages/isokron-client
pip install ./packages/kora-runtime

# 3. Install kora-cli against them.
pip install ./packages/kora-cli

# 4. Verify.
kora --help            # falls through to the in-tree main, requires monorepo on path
kora version           # one of the 2 fully-extracted handlers — runs standalone
kora migrate-hermes-home --help   # other fully-extracted handler
```

### Future (post-Hermes-on-PyPI)

```bash
pip install kora-cli  # transitively pulls kora-runtime + isokron-client + (someday) hermes-agent
```

## The 7 Phase 2-carved subcommands

| Subcommand | Style | LOC (wheel) | Notes |
|---|---|---|---|
| `migrate-hermes-home` | Full extraction | 358 | Pure stdlib; runs standalone from the wheel |
| `version` | Full extraction (rewritten) | ~30 | Uses `importlib.metadata` instead of in-tree constants |
| `doctor` | Dispatch shim | ~25 | Delegates to in-tree `kora_cli.doctor` (1986 LOC) |
| `status` | Dispatch shim | ~20 | Delegates to in-tree `kora_cli.status` (570 LOC) |
| `setup` | Dispatch shim | ~25 | Delegates to in-tree `kora_cli.setup` (3557 LOC) |
| `login` | Dispatch shim | ~20 | Delegates to in-tree `kora_cli.main` inline handler |
| `logout` | Dispatch shim | ~20 | Delegates to in-tree `kora_cli.main` inline handler |

See `PHASE-2-MIGRATION-PATTERN.md` for the decision criteria between full extraction vs dispatch shim, and the recommended Phase 2B/2C carve order for the remaining 40 subcommands.

## Console-script entry-point overlap with the top-level Kora pyproject

The top-level `kora` monorepo's `pyproject.toml` declares `kora = "kora_cli.main:main"`. This wheel's `pyproject.toml` declares `kora = "kora_cli_pkg.main:cli_entrypoint"`. **Both can coexist** — pip resolves whichever was installed last on entry-point overlap. The `cli_entrypoint` here falls through to `kora_cli.main:main` for uncarved subcommands, so the end behavior is identical regardless of which entry-point wins. Matches the Marvin POC (#204) pattern.

## License

MIT.
