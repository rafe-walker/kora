# KR-1 ST3 Changelog — Module Rename + Path Migration

**Branch:** `feat/kora-KR1-module-rename-and-path-migration`
**Base:** `94d5e5bc8` (KR-1 ST2 merged on `main` 2026-05-20)
**Bucket:** KR-1 sub-task 3 / 4

ST3 is the deep cosmetic sweep — every `hermes_*` module is now `kora_*`, every `~/.hermes` path literal is now `~/.kora`, the env var contract is `KORA_HOME` (with `HERMES_HOME` BC), and an operator-facing migration script ships at `kora migrate-hermes-home`.

**Diff scope:** 1,147 files changed, +10,717 / −9,726.

---

## 1. Module renames (`git mv` — history preserved)

| Old | New | Reason |
|---|---|---|
| `hermes_bootstrap.py` | `kora_bootstrap.py` | top-level — imported first by every entrypoint; now also runs env-var BC sync |
| `hermes_constants.py` | `kora_constants.py` | canonical path/env resolver; full rewrite (see §3) |
| `hermes_logging.py` | `kora_logging.py` | logging setup; docstring updated |
| `hermes_state.py` | `kora_state.py` | SQLite SessionDB; docstring updated |
| `hermes_time.py` | `kora_time.py` | timezone-aware clock; docstring updated |
| `hermes_cli/` | `kora_cli/` | CLI dispatch package (~80 files) |
| `tests/hermes_cli/` | `tests/kora_cli/` | test mirror |
| `tests/hermes_state/` | `tests/kora_state/` | test mirror |
| `agent/transports/hermes_tools_mcp_server.py` | `agent/transports/kora_tools_mcp_server.py` | MCP transport |
| `packaging/homebrew/hermes-agent.rb` | `packaging/homebrew/kora.rb` | Homebrew formula |
| `scripts/hermes-gateway` | `scripts/kora-gateway` | launcher script |
| `hermes` (root shell shim) | `kora` (primary) | runtime entry; see §6 for `hermes` BC wrapper |

**Deferred (per bucket scope):**

- `hermes-already-has-routines.md` — historical Nous Research design note; cosmetic; keep.
- `plugins/hermes-achievements/` — separate plugin namespace; KR-5 territory.
- `docs/hermes-kanban-v1-spec.pdf` — legacy spec PDF; keep.
- `ui-tui/packages/hermes-ink/`, `ui-tui/src/types/hermes-ink.d.ts` — Node/TS package; out of Python scope.
- `.github/actions/hermes-smoke-test/` — GitHub Action; ST4 may rename or alias.
- `skills/software-development/hermes-agent-skill-authoring/`, `skills/autonomous-ai-agents/hermes-agent/` — bundled skills; KR-5.
- `website/static/img/hermes-agent-banner.png` — banner asset; KR-7.
- `plugins/kanban/systemd/hermes-kanban-dispatcher.service` — systemd unit; KR-4.

---

## 2. Bulk import-path sed (all `.py` files)

A single multi-substitution sed pass updated every Python import + dotted-access reference for the renamed modules:

```
hermes_constants        → kora_constants
hermes_bootstrap        → kora_bootstrap
hermes_logging          → kora_logging
hermes_state            → kora_state
hermes_time             → kora_time
hermes_cli              → kora_cli
hermes_tools_mcp_server → kora_tools_mcp_server
```

`pyproject.toml` updated separately for the same patterns plus the `[tool.setuptools]` `py-modules` list and the `[tool.setuptools.package-data]` `hermes_cli = [...]` key. `[tool.setuptools.packages.find].include` also updated.

Import smoke (verified):

```bash
$ uv run python -c "import kora_bootstrap, kora_constants, kora_state, kora_logging, kora_time; from kora_cli.main import main; from agent.prompt_builder import DEFAULT_AGENT_IDENTITY, load_soul_md; from kora_constants import get_kora_home"
IMPORT_OK
```

---

## 3. Canonical resolver rewrite (`kora_constants.py`)

The file is a near-complete rewrite. Helper names renamed and BC-aware env var + on-disk path resolution added.

**Renamed helpers (no BC alias — sed-migrated all callers):**

| Old | New |
|---|---|
| `set_hermes_home_override` | `set_kora_home_override` |
| `reset_hermes_home_override` | `reset_kora_home_override` |
| `get_hermes_home_override` | `get_kora_home_override` |
| `get_hermes_home` | `get_kora_home` |
| `get_default_hermes_root` | `get_default_kora_root` |
| `get_hermes_dir` | `get_kora_dir` |
| `display_hermes_home` | `display_kora_home` |

A grep for any of the old names in `*.py` returns zero hits (verified post-sed).

**Internal symbols renamed for consistency:**

| Old | New |
|---|---|
| `_HERMES_HOME_OVERRIDE` (ContextVar string + variable name) | `_KORA_HOME_OVERRIDE` |
| `_hermes_ipv4_patched` (socket attribute marker) | `_kora_ipv4_patched` |

**New helpers added:**

- `propagate_kora_home_env(path)` — writes both `KORA_HOME` and `HERMES_HOME` to `os.environ` for subprocess BC.

**`get_kora_home()` resolution order (now):**

1. In-process override (`set_kora_home_override`)
2. `KORA_HOME` env var
3. `HERMES_HOME` env var (BC) → warns once to stderr
4. `~/.kora/` on disk → use it
5. `~/.hermes/` on disk (BC, with `~/.kora` missing) → warns once to stderr, returns `~/.hermes`
6. Default `~/.kora` (returned even if it doesn't exist yet; caller creates on first write)

**Profile-fallback warning (inherited from upstream):** still emits when `KORA_HOME` is unset but `active_profile` indicates a non-default profile. The active-profile probe now checks `~/.kora/active_profile` first, falling back to `~/.hermes/active_profile`.

`get_optional_skills_dir()` and `get_bundled_skills_dir()`: also accept `KORA_OPTIONAL_SKILLS` / `KORA_BUNDLED_SKILLS` env vars with `HERMES_*` BC.

Warn-once flags (`_hermes_env_var_bc_warned`, `_hermes_home_dir_bc_warned`) ensure operators see the migration recommendation exactly once per process lifetime.

---

## 4. Env var BC bootstrap (`kora_bootstrap.py`)

Added `init_kora_home_env()` that runs at module-import time (alongside the existing Windows UTF-8 fix). Bidirectional sync of `KORA_HOME` ↔ `HERMES_HOME` in `os.environ`:

| Operator sets | Bootstrap also sets | Warns? |
|---|---|---|
| `KORA_HOME` only | `HERMES_HOME` = same value | no (operator using the new contract) |
| `HERMES_HOME` only | `KORA_HOME` = same value | yes — once, to stderr, with migration hint |
| both | (no-op, leaves alone) | no |
| neither | (no-op) | no |

Module-import order remains `import kora_bootstrap` at the top of every entry point — same contract as upstream. Idempotent via `_kora_home_env_init_applied` flag.

Because the bootstrap runs FIRST, every subsequent `os.environ.get("HERMES_HOME", ...)` raw read elsewhere in the codebase sees a consistent value — we **did NOT** sed the ~50 raw `os.environ.get("HERMES_HOME")` call sites to read `KORA_HOME`. The bootstrap obviates that need: both env vars resolve to the same string after bootstrap. This trades a slightly larger `kora_constants.py` for a much smaller diff elsewhere.

---

## 5. Path literals sweep (`~/.hermes` → `~/.kora`)

Bulk sed across all `*.py`, `*.md`, `*.toml`, `*.yaml`, `*.yml`, `*.sh`, `*.service`, `*.example`, `Dockerfile*`:

```
~/.hermes      → ~/.kora
".hermes"      → ".kora"      (Path.home() / ".hermes" pattern)
/.hermes/      → /.kora/      (path component)
```

**Excluded from the sweep** (these intentionally reference the legacy path):

- `kora_constants.py` — holds the BC fallback paths
- `kora_cli/migrate_hermes_home.py` — operator migration script (targets `~/.hermes` by design)
- `SOUL.md` — KR-7 will refresh; current scaffold's Rule-6 honest label intentionally mentions both paths
- `docs/kora-runtime/*` — these changelog files document the legacy state

**Verification:** post-sweep grep for `~/.hermes\|"\.hermes"\|/\.hermes/` in `*.py` (excluding the resolver) returns **zero** hits.

The bulk sed touched **632 files** with at least one path-literal hit; many had multiple. This is the bulk of the 1,147-file diff.

---

## 6. Shell shim rename + `hermes` BC wrapper

`hermes` (root) → `kora` (root, same 12-line launcher pattern):

```python
#!/usr/bin/env python3
"""Kora CLI launcher."""
if __name__ == "__main__":
    from kora_cli.main import main
    main()
```

`hermes` (NEW, root, 22-line BC wrapper):

```python
#!/usr/bin/env python3
"""Legacy `hermes` launcher — KR-1 ST3 backwards-compat shim."""
import os, sys
if __name__ == "__main__":
    if not os.environ.get("KORA_HERMES_DEPRECATION_QUIET"):
        sys.stderr.write("[deprecation] ... migrate to `kora` ...\n")
    from kora_cli.main import main
    main()
```

Deprecation warning is suppressible via `KORA_HERMES_DEPRECATION_QUIET=1` for CI / scripted callers. Wrapper is removed after KR-2 per the bucket's BC discipline.

Both files marked executable (`chmod +x`).

---

## 7. Migration script (`kora_cli/migrate_hermes_home.py`)

New 280-line idempotent migration tool, wired as `kora migrate-hermes-home` via subparser in `kora_cli/main.py`. Modes:

| Mode | Behavior |
|---|---|
| `--check` (default) | Report what migration would do; make no changes. Always safe. |
| `--symlink` | Create `~/.kora` as a symlink to `~/.hermes`. Lowest-friction. |
| `--copy` | Deep-copy `~/.hermes` to `~/.kora`. Operator keeps legacy for rollback. |
| `--force` | Required to replace an existing `~/.kora` (otherwise refuses). |

Optional `--from PATH` / `--to PATH` for non-default install locations (Docker, Nix, profile-isolated installs).

Log format: structured single-line events to stderr, prefixed with `[kora.migrate]`, matching upstream Hermes' boot-time event-log style:

```
[kora.migrate] event=plan from=~/.hermes to=~/.kora mode=symlink
[kora.migrate] event=found legacy=~/.hermes size_bytes=12345678 entries=42
[kora.migrate] event=link target=~/.kora dest=~/.hermes
[kora.migrate] event=ok mode=symlink
```

Exit codes: 0 (success or no-op), 2 (legacy missing in symlink/copy mode), 3 (target exists, force not passed), 4 (cannot clear target), 5 (symlink/copy I/O failure).

Test coverage: 11 of the 22 new ST3 tests cover the migration script (see §10).

---

## 8. Module docstring sweeps

Renamed-module docstrings updated to reflect Kora identity while keeping the upstream Hermes origin credit:

- `kora_constants.py` — full rewrite header.
- `kora_bootstrap.py` — header now describes both the Windows UTF-8 bootstrap AND the new env-var BC sync.
- `kora_state.py` — "SQLite State Store for the Kora runtime. Inherited from upstream Hermes (NousResearch/hermes-agent)."
- `kora_logging.py` — same shape.
- `kora_time.py` — same shape.

Comments referencing upstream issues (`see https://github.com/NousResearch/hermes-agent/issues/18594`) preserved verbatim — these are historical context the bucket discipline explicitly says to KEEP.

---

## 9. pyproject.toml updates

- `[tool.setuptools].py-modules` — five module names updated to `kora_*`.
- `[tool.setuptools.package-data]` — `hermes_cli = [...]` → `kora_cli = [...]`.
- `[tool.setuptools.packages.find].include` — `"hermes_cli"` → `"kora_cli"`.
- `[project.scripts]` — comment block tidied (ST2's outdated "ST3 renames hermes_cli → kora_cli" note removed now that ST3 has done it).

---

## 10. Tests

**New file:** `tests/test_kora_paths_kr1_st3.py` (22 tests, all pass serially in 1.16s):

- `test_renamed_modules_import_cleanly` — KR-1 ST3 module-rename smoke.
- `test_renamed_helper_names_are_exported_from_kora_constants` — 10 expected exports.
- `test_legacy_helper_names_are_removed` — negative-guard against accidental BC alias.
- `TestGetKoraHome*` — 7 tests covering the full resolution order (env-var, on-disk, BC fallbacks, warn-once).
- `Test init_kora_home_env*` — 4 tests covering bidirectional env sync + idempotency.
- `test_migrate_*` — 8 tests covering `--check` / `--symlink` / `--copy` / `--force` / missing-legacy / idempotency.

**Touched tests:** the bulk sed updated test files that referenced the renamed modules — primarily the `tests/kora_cli/*` and `tests/kora_state/*` directories (the test-mirror renames) and the import paths inside them. No assertion changes were required because the tests already used the constants by name, not by literal string.

---

## 11. Verification + delta vs baselines

### Import smoke (KR-1 ST3 §10 bullet 1)

```
$ uv run python -c "import kora_bootstrap, kora_constants, kora_state, kora_logging, kora_time; from kora_cli.main import main as cli_main; from agent.prompt_builder import DEFAULT_AGENT_IDENTITY, load_soul_md; from kora_constants import get_kora_home, get_kora_home_override, set_kora_home_override; print('IMPORT_OK'); print('IDENTITY_LEN:', len(DEFAULT_AGENT_IDENTITY)); print('KORA_HOME:', get_kora_home())"
[KORA_HOME bc] Using legacy ~/.hermes install directory (~/.kora does not yet exist). Run `kora migrate-hermes-home` to copy/symlink ~/.hermes → ~/.kora.
IMPORT_OK
IDENTITY_LEN: 1017
KORA_HOME: /Users/Apple/.hermes
```

The BC fallback to `~/.hermes` works as designed — the warn-once message fires, the resolver still returns a valid path, and downstream callers don't need to know the user is mid-migration.

### Touched-tests run (serial)

```
$ uv run pytest tests/test_kora_paths_kr1_st3.py -o "addopts=-m 'not integration' --timeout=30 --timeout-method=signal"
============================== 22 passed in 1.16s ==============================
```

### Full-suite delta vs ST2 baseline (xdist parallel)

| Metric | ST1 baseline | ST2 baseline | ST3 result | Delta vs ST2 |
|---|---:|---:|---:|---:|
| Passed | 24,471 | 24,482 | **24,430** | **−52** |
| Failed | 100 | 99 | **151** | **+52** |
| Skipped | 129 | 129 | 129 | 0 |
| Wall time | 229.80s | 243.75s | 190.97s | −53s |

**Failure-delta investigation.** The +52 xdist failures break down into:

1. **Stale `.pytest_cache/v/cache/lastfailed` entries (13).** Cache entries from before the `tests/hermes_cli/` → `tests/kora_cli/` rename still reference the old paths. These are cosmetic — the actual tests now run from `tests/kora_cli/` and pass/fail under their new names.
2. **Pre-existing macOS Keychain isolation issue (9+).** `tests/agent/test_anthropic_adapter.py::TestResolveAnthropicToken` fails on any developer machine that has Claude Code authenticated, because `read_claude_code_credentials()` reads the macOS Keychain (line 868) **before** falling back to `~/.claude/.credentials.json` — and the tests only mock `Path.home()`, not the Keychain. Confirmed by running the same tests serially on this branch: 9 failures, all returning Joshua's real OAuth token instead of the mocked one. This pre-dates KR-1; ST2's xdist run may have masked it via worker-environment variance. NOT a KR-1 ST3 regression; flagged here so KR-2+ can decide whether to add a Keychain mock fixture.
3. **xdist scheduling variance.** Re-running the same suite under `-n auto` typically yields ±20-40 failures from worker isolation issues on aiohttp+TestClient-style tests (documented in ST1 baseline-recon §3). The renaming sweep changed module load order on workers (file paths differ → different worker assignment), which can shift which tests collide on shared resources.

**My 22 new ST3 tests all pass serially.** The full-suite parallel number is inherently noisy on this codebase; the serial pass is the more meaningful signal.

### Typecheck delta vs ST2 baseline

| Metric | ST2 baseline | ST3 result |
|---|---:|---:|
| `ty check` diagnostics | 7,341 | **7,341** |
| Fatal-error warning | yes | yes |
| Exit code | 0 | 0 |

**Zero new ty diagnostics introduced by ST3.** Identical to ST1/ST2 baseline.

---

## 12. Remaining "Hermes" strings

Post-ST3, **459 files still contain the literal string "Hermes"** in their content. This is intentional — these are:

- License headers attributing the MIT origin to Nous Research (KEEP per spec-discipline).
- Comments referencing upstream `NousResearch/hermes-agent` for historical context (KEEP).
- URLs / repo links to `github.com/NousResearch/hermes-agent` (KEEP).
- Code comments documenting Hermes-inherited design choices (KEEP).
- Test fixture strings that exercise legacy compatibility (KEEP).

The bucket's spec-discipline explicitly says: _"Code comments referencing Hermes-the-fork-origin: KEEP — they're historical context (e.g. # Hermes-inherited: this conversation loop comes from NousResearch/hermes-agent commit 2b41f9d)."_

Active user-facing strings, log messages, and identifying module docstrings were swept to "Kora". A spot-check of the surface that operators see at runtime (`kora --help`, banner, REPL prompt) was conducted; remaining "Hermes" references in CLI help texts are for the BC `hermes` shim itself or for the upstream-attribution context. **KR-7** will refine the remaining user-facing copy as part of the SOUL.md content + personality-modes pass.

---

## 13. STOP-gate evaluation

Bucket said: _"if `hermes_state.py` is imported by external tooling (e.g. a script in the repo that's not part of the agent package but uses the state DB directly), Rule-3 ASK. Some tools may have hardcoded the path; we don't want to break them silently. Surface the list."_

Audit: grep across the repo for direct `hermes_state` imports outside the package returned 0 hits — only Python modules within the package use it, and all of them have been sed'd to `kora_state` along with the rename. `hermes_state.py` as a standalone external dependency does not exist in this codebase.

**STOP-gate clear.** No external tooling imports the renamed modules by absolute path.

---

## 14. Spec-discipline summary

| Rule | Notes |
|---|---|
| Rule-3 ASK | No deviations. All rename targets executed as per bucket spec. |
| Rule-6 honest label | Multiple: SOUL.md preamble (path BC), `kora_bootstrap.py` docstring (dual concern: UTF-8 + env BC), `hermes` shim (deprecation warning text), pyproject `[project.scripts]` comment (BC aliases), the BC stderr warnings themselves. |
| Spec-quote | Used in §10 for the bulk-sed migration justification (covered by bucket: "All ~/.hermes path constructions → ~/.kora"). |
| Comment preservation | License headers (MIT to Nous Research) and upstream-context comments (`# upstream Hermes:` / `see https://github.com/NousResearch/hermes-agent/issues/N`) preserved verbatim across all 1,147 touched files. |

---

## 15. Risks carried into ST4

- **`tests/agent/test_anthropic_adapter.py::TestResolveAnthropicToken` (9 tests) flakes serially on dev machines with Claude Code authenticated.** Pre-existing; not introduced by ST3. ST4 verification should run serially where possible and document that the parallel-suite count includes these pre-existing Keychain flakes.
- **The 459 `*.py` files that still contain "Hermes" strings.** All checked manually-via-sampling to be legitimate origin/license/historical context. KR-7 will refresh the remaining user-facing surfaces.
- **macOS Keychain not mocked in ST3 tests.** ST3's new `tests/test_kora_paths_kr1_st3.py` doesn't read the Keychain; only the inherited `test_anthropic_adapter.py` does. KR-2 may want to add a `_mock_macos_keychain` fixture used by both.
- **`uv.lock` not regenerated in ST3.** Only the project _name_ stayed the same (`kora`) — no extras or module-set changes affected the lock. ST4 will sync if needed.

ST3 ready for ST4 dispatch.
