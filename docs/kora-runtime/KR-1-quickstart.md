# Kora Quickstart — KR-1

This is the operator-facing onboarding for the Kora runtime as of KR-1 (fork + identity + path migration complete). Memory backend is still flat `MEMORY.md` / `USER.md` inherited from Hermes — **KR-2 swaps that for IsoKron**, which is when the typed-graph memory primitives become available. Until then, Kora boots and runs as a recognizable rebrand of Hermes with the Kora identity prompt, the Kora install dir, and the Kora console script. Rule-6 honest label: KR-1 does not yet wire the IsoKron substrate.

## What is Kora

Kora is Joshua's personal frontier-tier orchestration agent. Codename **Architect-of-Will**. Runs on Claude Opus 4.7 via Joshua's Anthropic Max plan. Not customer-facing. Forked from `NousResearch/hermes-agent` (MIT, commit `5e743559e`) and retargeted to the IsoKron typed-graph substrate. The runtime body (conversation loop, plugin SDK, provider/transport layer, messaging gateways, MCP client+server, cron scheduler, subagent primitives) is inherited from Hermes; the identity, the memory substrate, the orchestration layer, and the tool surface are Kora-specific.

KR-1 makes the fork real. KR-2 swaps the memory provider. KR-3 wires beads-pattern scratchpad consumption. KR-4 layers maestro-style orchestration. KR-5 curates the tool surface for Joshua's workflow. KR-6 wires Constitution pre-screen + capability matrix checks. KR-7 ships the full `SOUL.md` content + personality modes.

## Install

The fork is at `https://github.com/rafe-walker/kora` (private). Local clone:

```bash
mkdir -p ~/code
cd ~/code
git clone https://github.com/rafe-walker/kora.git kora-runtime
cd kora-runtime
```

Python tooling: `uv` (per Joshua's KR-1 ST1 directive — fastest, least global-state-invasive).

```bash
# One-time uv install
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# Sync deps. Curated extras list excludes matrix (libolm build issue
# on macOS), cloud-sandbox runtimes (modal/daytona/vercel), and
# tts-premium (elevenlabs, paid). See docs/kora-runtime/KR-1-baseline-recon.md §1.
uv sync --extra dev --extra messaging --extra mcp --extra acp \
        --extra web --extra cli --extra pty --extra google \
        --extra youtube --extra homeassistant --extra sms \
        --extra honcho --extra anthropic --extra voice --extra exa \
        --extra firecrawl --extra fal --extra edge-tts \
        --extra bedrock --extra slack
```

Python 3.11+ required (uv installs the right version into `.venv/` automatically).

## First-run

```bash
# Version check — should print "Kora 0.1.0 (Hermes-derived runtime; fork of NousResearch/hermes-agent@5e743559e)"
uv run kora --version

# If you previously used Hermes Agent and have ~/.hermes/ populated,
# migrate it. --check shows what would happen; --symlink is the
# lowest-friction path (~/.kora becomes a symlink to ~/.hermes).
uv run kora migrate-hermes-home --check
uv run kora migrate-hermes-home --symlink     # or --copy for deep copy

# Interactive setup wizard (model, providers, tools).
uv run kora setup --non-interactive    # or omit --non-interactive for the full wizard

# Start chatting.
uv run kora chat
```

If you don't migrate from `~/.hermes`, Kora will see the legacy directory exists and use it as a backwards-compat fallback — a one-time stderr message tells you to run `kora migrate-hermes-home`. Until KR-2 lands, both `~/.kora` and `~/.hermes` resolve to the same on-disk state if you use `--symlink`.

## Joshua-specific notes

* **Memory backend is still flat (Hermes-inherited).** KR-2 swaps in the IsoKron memory provider. Until then, Kora persists to `~/.kora/memories/MEMORY.md` and `~/.kora/memories/USER.md`. The `kronicle.agent_scratchpad_entries` table on the IsoKron substrate exists (Plan 02 landed) but the runtime does not yet consume it — that's KR-3.
* **PM-driven surfaces.** Both `kora mcp serve` (MCP stdio server) and the HTTP API server (`gateway/platforms/api_server.py` adapter) start cleanly. Both PMs (claude_pm on the Kora workspace + claude_pm on IsoKron) can drive Kora directly without going through messaging gateways. KR-6 wires capability + Constitution checks on these surfaces.
* **Identity sources of truth.** `agent/prompt_builder.py:134` holds `DEFAULT_AGENT_IDENTITY`; `~/.kora/SOUL.md` (or repo-root `SOUL.md`) overrides it. KR-7 fills the SOUL.md with the full operator-tunable identity + personality modes.
* **Role Charter authority boundary.** Kora reads `public.kora_role_charter` from IsoKron on every session start (per the identity prompt). KR-6 wires the actual read; today the prompt advertises it for forward compatibility.

## Surfaces verified at KR-1 ST4

* `kora --version` prints the Kora 0.1.0 / fork-of-Hermes@`5e743559e` line.
* `kora --help` shows `usage: kora` and the Kora-rebranded description.
* `kora chat --help` describes the surface as "interactive chat with Kora".
* `kora setup --help` describes the wizard as configuring "the Kora runtime".
* `kora mcp serve` starts on stdio, exits 0 on EOF (PM-driven MCP path).
* `kora gateway --help` lists `run/start/stop/restart/status/install/uninstall/list/setup/migrate-legacy`.
* HTTP API server (`APIServerAdapter`) imports + `check_api_server_requirements()` returns True.
* `kora migrate-hermes-home --check / --symlink / --copy` all work.
* `./hermes --version` BC wrapper runs `kora` underneath + emits a deprecation warning (suppressible via `KORA_HERMES_DEPRECATION_QUIET=1`).

## Operator pitfalls

* **Don't `pip install kora` from PyPI.** The package name `kora` on PyPI is not us. Local dev install only (`uv sync`) for KR-1.
* **Test runs under `-n auto` produce ~100-150 xdist failures** — pre-existing aiohttp+TestClient isolation issue from upstream, documented in KR-1 baseline-recon §3. Run affected tests serially for clean signal: `uv run pytest tests/agent/test_anthropic_adapter.py -o "addopts=-m 'not integration' --timeout=30 --timeout-method=signal"`.
* **`HERMES_*` env vars are still respected** for one BC cycle. At process start, `kora_bootstrap.init_kora_home_env()` mirrors any `HERMES_FOO` to `KORA_FOO` and emits a one-time stderr migration recommendation. KR-2 will remove the HERMES_* read path.
* **macOS Keychain.** If you have Claude Code authenticated, `tests/agent/test_anthropic_adapter.py::TestResolveAnthropicToken` will fail serially because `read_claude_code_credentials()` reads the Keychain before the mock-able file path. Pre-existing upstream issue; KR-2+ may add a Keychain mock fixture.

## What's next (KR-2 …)

| Bucket | Goal |
|---|---|
| **KR-2** | IsoKron memory provider — `plugins/memory/isokron/` implementing the `MemoryProvider` ABC. Replaces flat MEMORY.md / USER.md. The big architecture piece. |
| **KR-3** | Beads-pattern consumer — wire the runtime to consume `kronicle.agent_scratchpad_entries` (Plan 02 substrate already live). Adds `iso_node_*` / `iso_link_*` tools. |
| **KR-4** | Maestro orchestration layer — extract maestro's long-running task state machine + checkpoint/replay on top of `kanban_tools.py`, swap SQLite for IsoKron. |
| **KR-5** | Tool surface curation — prune the platform-specific bloat via config (Spotify, gaming, yuanbao, weixin). Enable messaging platforms Joshua actually wants. |
| **KR-6** | Capability + Constitution wiring — every Kora tool call hits `actorHasCapability('kora', cap)` + Constitution pre-screen middleware before commit. (Substrate side already landed via IsoKron K-1/K-2/K-6.) |
| **KR-7** | SOUL.md content + personality modes — Joshua-blessed Kora identity content, voice, escalation triggers; personality config block for Pull cockpit / Push Operator-Ping / Ambient Watch Brief surface modes. |
