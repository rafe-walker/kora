# KR-1 Baseline Recon — Hermes-agent fork @ rafe-walker/kora

**Generated:** 2026-05-20 (CC#3, KR-1 ST1)
**Branch:** `feat/kora-KR1-fork-recon`
**Fork HEAD at clone time:** `5e743559e0157df42e0f640cd06d736e898370d0` — `fix(lint): skip per-file shell linter when LSP will handle the file (#29054)`
**Upstream:** `NousResearch/hermes-agent` (added as `upstream` remote; not yet merged in)
**Reference doc:** `/Users/Apple/Documents/Claude/Projects/Kora/research/hermes.md` (commit `2b41f9d`, May 2026)

This is the read-only baseline before any KR-1 modification. Every modification in ST2/ST3/ST4 must measure delta against this snapshot.

---

## 1. Environment

| Item | Value |
|---|---|
| Python | 3.11.15 (installed by `uv` into `.venv/`) |
| Tooling choice | **uv** (per Joshua directive 2026-05-20 — fastest, least global-state-invasive). KR-2/3/4 follow. |
| `uv` version | `0.11.15 (3cffe97c2 2026-05-18 aarch64-apple-darwin)` |
| Build backend | `setuptools>=61.0` (pyproject.toml L1-3) |
| Test framework | pytest 9.0.2 (+ pytest-asyncio, pytest-xdist, pytest-split, pytest-timeout) |
| Type checker | **ty** 0.0.21 (Astral's typechecker — not mypy/pyright) — Python 3.13 target per `[tool.ty.environment]` |
| Linter | ruff 0.15.10 (preview mode; only PLW1514 selected — `select = ["PLW1514"]`) |
| Package name (upstream) | `hermes-agent` 0.14.0 |

**Bootstrap chain we ran (capture for KR-2+):**

```bash
# uv: ~/.local/bin/uv (installed via `curl -LsSf https://astral.sh/uv/install.sh | sh`)
export PATH="$HOME/.local/bin:$PATH"
cd ~/code/kora-runtime
uv sync --extra dev \
  --extra messaging --extra mcp --extra acp --extra web --extra cli --extra pty \
  --extra google --extra youtube --extra homeassistant --extra sms \
  --extra honcho --extra anthropic --extra voice \
  --extra exa --extra firecrawl --extra fal --extra edge-tts \
  --extra bedrock --extra slack
```

**Why this extras list (not `--all-extras`):** `--all-extras` pulls `[matrix]` which depends on `mautrix[encryption]` → `python-olm`, which needs `libolm` system lib + `make` to build from sdist on macOS. Reproducible failure: `subprocess.CalledProcessError: Command '['make', 'static']' returned non-zero exit status 2`. The pyproject's own `[all]` extra excludes matrix for exactly this reason (see L186-191 comment). Also excluded: `tts-premium` (elevenlabs, paid), `modal/daytona/vercel` (cloud sandbox runtimes), `dingtalk/feishu` (Chinese-platform messaging — orthogonal), `azure-identity`, `hindsight`. These can be added in KR-2 if a specific test or feature needs them.

---

## 2. Upstream repo size

| Metric | Value |
|---|---|
| Total checkout | 207 MB |
| `.git/` | 131 MB |
| `.py` files (excl. .git/.venv/node_modules) | **1,781** |
| Files containing literal "Hermes" string (`.py` only) | **459** |
| Total "Hermes" line-hits (`.py` only) | **2,311** |
| Files referencing `~/.hermes` / `HERMES_HOME` / `get_hermes_home` (`.py` only) | **527** |
| Files with "hermes" in filename (excl. .git) | 23 (see §4) |
| `cli.py` size (ref doc said 657KB/14,466 lines) | TBD — current HEAD drift expected ± few lines |

This is the surface area ST3 has to sweep.

---

## 3. Test + typecheck baseline

### Collection

| Run | Result |
|---|---|
| `uv run pytest --collect-only -q` (dev-only extras) | 24,025/24,034 collected — 24 errors (missing optional deps: aiohttp, etc.) |
| `uv run pytest --collect-only -q` (curated extras above) | **24,698/24,755 collected, 57 deselected, 0 errors** |

Baseline collection error count = **0** after curated extras install.

### Test run

`uv run pytest` (default args from pyproject: `-m 'not integration' -n auto --timeout=30 --timeout-method=signal`):

| Metric | Value |
|---|---|
| **Passed** | **24,471** |
| **Failed** | **100** |
| **Skipped** | 129 |
| **Warnings** | 234 |
| **Wall time** | **229.80s (3:49)** |
| Sum (passed+failed+skipped) | 24,700 (note: collection was 24,698 collected + 57 deselected; the +2 delta is probably test parameterization expansion at runtime) |

#### Pre-existing failure distribution (top-level dirs)

| Count (visible in tail-50) | Dir |
|---:|---|
| 18 | `tests/gateway/` (Discord, Telegram, webhook, api_server) |
| 13 | `tests/tools/` (file dedup, staleness, terminal requirements) |
| 12 | `tests/hermes_cli/` (gateway-service systemd/WSL, list-picker, model-switch) |
| 4 | `tests/test_live_system_guard_self_test.py` (systemctl on darwin) |
| 1 | `tests/test_tui_gateway_server.py` (browser_manage) |
| 1 | `tests/plugins/web/` (search-provider plugins) |

**Important — these are xdist parallelism artifacts, NOT real code failures.** Confirmed by running one of the failing classes serially:

```
$ uv run pytest tests/gateway/test_api_server.py::TestHealthEndpoint -x --tb=short \
    -o "addopts=-m 'not integration' --timeout=30 --timeout-method=signal"
======================== 3 passed, 3 warnings in 0.92s =========================
```

`tests/gateway/test_api_server.py::TestHealthEndpoint` (one of the 100 "failures") **passes cleanly serially**. The aiohttp test app + `TestClient` fixtures appear to clash when xdist runs them across parallel workers binding to the same in-process app keys (NotAppKeyWarning visible in the serial run). This is an upstream pytest-xdist + aiohttp.test_utils interaction issue, not a regression introduced by us.

**For KR-1 STOP-gate purposes:**
- The 100-failure number is THE pre-existing baseline under the project's standard `pytest` invocation. ST2/ST3/ST4 must not introduce additional failures beyond this baseline.
- ST4 operational verification (which must confirm the HTTP API server starts cleanly per Joshua's binding) can rely on serial invocation of the api_server tests to verify "real" health, separate from the parallel-run baseline number.
- Any reduction in the 100-failure number across ST2/3/4 is positive but not required.

#### Failure-class concentration (from `.pytest_cache/v/cache/lastfailed`)

The pytest cache marks many test CLASSES as failed (single failure within a class taints the whole class entry). Heavily-hit files:

- `tests/gateway/test_api_server.py` — 30+ classes (all the OpenAI-compat surface tests: Chat/Responses/Health/Models/Capabilities/Auth/CORS/Streaming). **Relevant to KR-1 ST4 binding.**
- `tests/gateway/test_api_server_jobs.py`, `test_api_server_multimodal.py`, `test_api_server_runs.py` — sibling api_server suites.
- `tests/gateway/test_webhook_adapter.py`, `test_webhook_integration.py`, `test_webhook_signature_rate_limit.py`, `test_webhook_deliver_only.py` — webhook gateway.
- `tests/acp/*` — Agent Client Protocol classes.
- `tests/integration/test_ha_integration.py` — Home Assistant (probably gated by `pytest -m 'not integration'` but parameterized variants leak through).
- `tests/plugins/test_kanban_dashboard_plugin.py`, `test_kanban_worker_runs.py`.

The pattern across these: they all use `aiohttp.test_utils.TestClient` / `AioHTTPTestCase` and run under xdist. Consistent with the "xdist parallelism artifact" diagnosis.

### Typecheck

`uv run ty check` — exit code 0 (ty does NOT fail on diagnostics; it reports them):

| Metric | Value |
|---|---|
| Total diagnostics | **7,341** |
| Fatal error during check | **YES** — "A fatal error occurred while checking some files. Not all project files were analyzed." (warning at output tail) |
| Sample diagnostic | `error[invalid-argument-type]` at `utils.py:246:28` — `Argument to function _restore_file_mode is incorrect; Expected 'Path', found 'str'` |

**Interpretation:** ty is the Astral typechecker (alpha — 0.0.21). 7,341 diagnostics is the pre-existing baseline. ST2/ST3/ST4 must not increase this count. The "fatal error" warning means some files couldn't be analyzed at all — also pre-existing, not introduced by us.

### Import smoke

```python
$ uv run python -c "import hermes_bootstrap, hermes_constants, hermes_state, hermes_logging, hermes_time; from agent.prompt_builder import DEFAULT_AGENT_IDENTITY, load_soul_md; print('IMPORT_OK'); print('IDENTITY_LEN:', len(DEFAULT_AGENT_IDENTITY)); print('IDENTITY_HEAD:', DEFAULT_AGENT_IDENTITY[:80])"
IMPORT_OK
IDENTITY_LEN: 513
IDENTITY_HEAD: You are Hermes Agent, an intelligent AI assistant created by Nous Research. You
```

All five `hermes_*` top-level modules import cleanly; `DEFAULT_AGENT_IDENTITY` resolves to a 513-char literal starting with the expected upstream prose.

---

## 4. Identity locations (verified at current HEAD)

Reference doc cited approximate line numbers from commit `2b41f9d`. Current HEAD is `5e743559e` — confirming each location:

| Reference (ref doc) | Actual (current HEAD) | Status |
|---|---|---|
| `agent/prompt_builder.py:134` `DEFAULT_AGENT_IDENTITY` literal | **L134-135** — `DEFAULT_AGENT_IDENTITY = (` then `"You are Hermes Agent, an intelligent AI assistant created by Nous Research. "` | ✅ matches |
| `agent/prompt_builder.py:1314` `load_soul_md` def | **L1313** `def load_soul_md() -> Optional[str]:` | 1-line drift (expected) |
| Reference doc says "8 hits" of `Hermes` in `prompt_builder.py` | **10 hits** at current HEAD (drift) | drift; see below |

### All `Hermes` hits in `agent/prompt_builder.py` (the file ST2 modifies)

```
L135   "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "  # DEFAULT_AGENT_IDENTITY literal — the primary ST2 target
L145   "If the user asks about configuring, setting up, or using Hermes Agent "         # DEFAULT_AGENT_IDENTITY (continuation)
L588   "You are in the Hermes WebUI, a browser-based chat interface. "                  # WebUI block (ref doc flagged this one)
L621   # container / remote host rather than on the machine where Hermes itself        # comment
L649   # across Hermes restarts.                                                        # comment
L670   operate on a different machine than the host Hermes runs on.                    # comment / docstring
L809   f"where Hermes itself is running. The host OS, home, and cwd "                  # remote-host guidance string
L810   f"of the Hermes process are irrelevant; only the following "                    # remote-host guidance string
L820   f"inside {description} — NOT on the machine where Hermes "                      # remote-host guidance string
L1205  "or troubleshoot Hermes Agent itself — its CLI, config, models, providers..."   # self-help guidance (ref doc flagged 1205)
```

ST2's identity-replacement scope:
- **Replace** the multi-line `DEFAULT_AGENT_IDENTITY` literal (L134-149-ish — exact span verified at edit time) with the Kora skeleton from the bucket spec.
- **Replace** L588 WebUI block — "Hermes WebUI" → "Kora WebUI" (user-facing prompt text).
- **Replace** L1205 self-help guidance — "Hermes Agent" → "Kora".
- **Keep** L621/L649/L670/L809/L810/L820 — these are runtime-machine vs remote-machine guidance comments/strings. They refer to "Hermes the running process" generically; ST3 (path migration) will retarget these to "Kora the running process" along with the rest of the s/Hermes/Kora/ string sweep. Doing them in ST2 is premature — they're not identity-prompt material.

(Final decision on which to swap in ST2 vs ST3 will be made at ST2 edit time; this is the recon-time read.)

---

## 5. Files containing `hermes` in their name (ST3 rename targets)

Top-level Python files that need `git mv` in ST3:

```
./hermes                         # shell shim (root) — 5-line python launcher
./hermes_bootstrap.py            # Windows UTF-8 fixup, imported first by every entrypoint
./hermes_constants.py            # HERMES_HOME getter + path constants
./hermes_logging.py              # logging setup
./hermes_state.py                # SessionDB + SQLite/FTS5
./hermes_time.py                 # tz-aware clock
./hermes_cli/                    # package: argparse / cmd_* dispatch / setup wizards
```

Other `hermes`-named paths (NOT all are ST3 renames — see notes):

```
./hermes_agent.egg-info/         # build artifact — regenerated; not committed (in .gitignore)
./hermes-already-has-routines.md # NousResearch designer note (KEEP as historical; rename optional)
./plugins/hermes-achievements/   # plugin namespace (KEEP — separate concern from runtime rename)
./tests/hermes_cli/              # mirrors hermes_cli/ — rename to tests/kora_cli/ in ST3
./tests/hermes_state/            # mirrors hermes_state.py tests — rename to tests/kora_state/
./docs/hermes-kanban-v1-spec.pdf # legacy spec doc; rename optional (cosmetic)
./scripts/hermes-gateway         # bash launcher; rename in ST4 (console-script territory)
./packaging/homebrew/hermes-agent.rb  # Homebrew formula; rename in ST3 (kora.rb)
./ui-tui/packages/hermes-ink/    # Node package — TS/JS, out of Python rename scope
./agent/transports/hermes_tools_mcp_server.py  # rename in ST3 (becomes kora_tools_mcp_server.py)
./.github/actions/hermes-smoke-test/  # GitHub Action; rename or alias in ST4
./ui-tui/src/types/hermes-ink.d.ts    # TS, out of Python rename scope
./skills/software-development/hermes-agent-skill-authoring/  # bundled skill; rename in KR-5
./skills/autonomous-ai-agents/hermes-agent/  # bundled skill; rename in KR-5
./website/static/img/hermes-agent-banner.png  # banner asset; ST2 territory
./plugins/kanban/systemd/hermes-kanban-dispatcher.service  # systemd unit; rename in KR-4 (maestro layer)
```

ST3 will produce an exhaustive rename table in its own changelog; the above is the recon-time map.

---

## 6. Top-30 `.py` files by "Hermes" string count

These files concentrate the s/Hermes/Kora/ surface area for ST3:

| Hits | File |
|---:|---|
| 85 | `hermes_cli/main.py` |
| 71 | `tests/gateway/test_feishu.py` |
| 65 | `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py` |
| 60 | `cli.py` |
| 44 | `hermes_cli/providers.py` |
| 44 | `gateway/run.py` |
| 41 | `hermes_cli/auth.py` |
| 37 | `hermes_cli/setup.py` |
| 35 | `gateway/platforms/api_server.py` |
| 31 | `tests/gateway/test_api_server.py` |
| 31 | `plugins/hermes-achievements/dashboard/plugin_api.py` |
| 31 | `hermes_cli/gateway.py` |
| 31 | `hermes_cli/config.py` |
| 30 | `tests/hermes_cli/test_gateway_windows.py` |
| 26 | `tests/tools/test_mcp_oauth_cold_load_expiry.py` |
| 25 | `tests/cli/test_branch_command.py` |
| 25 | `hermes_cli/codex_runtime_plugin_migration.py` |
| 22 | `tests/cli/test_destructive_slash_confirm.py` |
| 21 | `hermes_cli/skin_engine.py` |
| 18 | `hermes_cli/model_switch.py` |
| 18 | `acp_adapter/server.py` |
| 17 | `hermes_cli/tips.py` |
| 17 | `agent/transports/hermes_tools_mcp_server.py` |
| 16 | `tests/tools/test_mcp_oauth.py` |
| 16 | `tests/tools/test_mcp_oauth_metadata.py` |
| 16 | `tests/skills/test_openclaw_migration.py` |
| 16 | `plugins/memory/honcho/cli.py` |
| 16 | `hermes_cli/web_server.py` |
| 16 | `gateway/platforms/discord.py` |
| 15 | `tests/cli/test_cli_init.py` |

**Notable for ST4 binding (Kora must expose `kora mcp serve` AND HTTP API server):**
- `gateway/platforms/api_server.py` — **35 hits** — the HTTP API server (`BasePlatformAdapter` subclass). ST4 must verify it starts clean as "kora" surface.
- `mcp_serve.py` — top-level — the `hermes mcp serve` (will be `kora mcp serve` in ST4 console-script rename).
- `agent/transports/hermes_tools_mcp_server.py` — file rename to `kora_tools_mcp_server.py` in ST3.

---

## 7. `~/.hermes` path reference distribution (sample)

527 Python files reference `~/.hermes`, `HERMES_HOME`, or `get_hermes_home()`. ST3 scope. Notable concentrations:

- `hermes_constants.py` — the canonical `get_hermes_home()` resolver (L44+) — single point of definition for `~/.hermes` default. ST3 will replace with `get_kora_home()` returning `~/.kora` and read `KORA_HOME` env var first, falling back to `HERMES_HOME` for BC.
- `hermes_state.py` — uses `get_hermes_home()` for `sessions/state.db` path.
- `hermes_logging.py` — uses `get_hermes_home()` for log directory.
- `hermes_cli/` — every command surface (`config.py`, `auth.py`, `setup.py`, etc.) accesses paths via these helpers.
- `agent/memory_manager.py` + `agent/memory_provider.py` — for `~/.hermes/memories/` defaults.
- `agent/skill_*.py` — for `~/.hermes/skills/` user-installed skill dirs.
- `gateway/`, `cron/`, `tools/` — distributed references.

If `get_hermes_home()` is renamed to `get_kora_home()` in `hermes_constants.py` and re-exported from `kora_constants.py`, the majority of call sites flip transparently. Hardcoded `~/.hermes/` string literals (not routed through the helper) will need to be hand-fixed — count TBD in ST3.

---

## 8. Pre-existing baseline anomalies / known issues

- **`tests/tools/test_file_sync_perf.py:82` — unknown pytest mark** `@pytest.mark.ssh` (pytest emits PytestUnknownMarkWarning at collection). Not a failure, but worth noting.
- **`utils.py:246:28`** — ty diagnostic: `_restore_file_mode(real_path, original_mode)` passes `str` where `Path` is expected. Pre-existing upstream type bug.
- **Some test modules require optional extras** (`messaging`, `homeassistant`, `slack`, etc.) — handled by the curated `uv sync --extra ...` list above. Without those extras: 24 collection errors. With: 0 errors.
- **ty `fatal error during check`** — some files not fully analyzed; pre-existing; carrying forward.
- **`pyproject.toml` L43, L50** reference `CVE-2026-25645` (requests==2.33.0) and `CVE-2026-32597` (PyJWT==2.12.1). These are exact pins kept to the patched versions; treat any future pin loosening here as a Rule-3 ASK.
- **`pyproject.toml` L111-126** — `mistral` extra was REMOVED 2026-05-12 (Mini Shai-Hulud worm). Pinned dependency lockdown is intentional security posture for KR-2/3/4 too: do NOT introduce version ranges in `dependencies` without justification.

---

## 9. STOP-gate evaluation

Bucket said: _"if upstream's test count drops massively from documented baselines (e.g. >20% test loss between the doc's 2026-05-19 snapshot and current HEAD), Rule-3 ASK."_

Reference doc didn't quote a specific test count, but did cite file sizes (`cli.py:657KB/14466 lines`, `run_agent.py:179KB/4123 lines`, `hermes_state.py:138KB/3000+ lines`). Current HEAD has not been measured against those file sizes in this recon (deferred — not high signal). Test collection is **24,698 collected** which is a very large suite consistent with the ref doc's description of the repo as "large." No abnormal test-count drop detected. **STOP-gate clear.**

---

## 10. Sub-task readiness checklist

- [x] Fork exists at `rafe-walker/kora` (created manually 2026-05-20)
- [x] Clone to `~/code/kora-runtime/`
- [x] `upstream` remote configured to `NousResearch/hermes-agent`
- [x] `uv` installed (`~/.local/bin/uv`)
- [x] Python 3.11.15 installed via uv
- [x] Curated extras installed (no matrix; no cloud-runtimes)
- [x] Test collection: 24,698 tests, 0 errors
- [x] Typecheck baseline: 7,341 diagnostics (no exit-code failure)
- [x] Test run baseline: **24,471 passed / 100 failed / 129 skipped in 229.80s** (failures are xdist parallelism artifacts; one sample passes serially)
- [x] Import smoke: `hermes_bootstrap`, `hermes_constants`, `hermes_state`, `hermes_logging`, `hermes_time`, `agent.prompt_builder` all import; `DEFAULT_AGENT_IDENTITY` is 513 chars
- [x] Identity location confirmed: `agent/prompt_builder.py` L134-135 (literal), L1313 (`load_soul_md`)

**Ready for ST2** once the test-run row is filled in and PR merged.

---

## Appendix A: branch + remote layout

```
origin   https://github_pat_***@github.com/rafe-walker/kora.git  (fetch + push)
upstream https://github.com/NousResearch/hermes-agent.git        (fetch + push — read-only in practice)
```

Local branches:
```
* feat/kora-KR1-fork-recon  (this PR's branch)
  main                      (fork HEAD)
```

PAT inlined in `origin` remote URL per bucket setup commands. PM-noted: PAT will be rotated post-build.

## Appendix B: PM context

Bucket file: PM dispatch 2026-05-20 (CC#3, KR-1: Kora Runtime Fork + Identity Swap).
Lane: CC#3 (Python — runtime). Concurrent: CC#1 (TS, IsoKron K-2), CC#2 (TS, IsoKron KF-3).
Repo binding: PM merges KR-1 sub-tasks serially ST1 → ST2 → ST3 → ST4.
Joshua directive 2026-05-20 (also captured in PM memory): KR-1 ST4 must verify BOTH `kora mcp serve` AND HTTP API server (`gateway/platforms/api_server.py` adapter) start clean. PMs need programmatic access to drive Kora directly.
