# KR-1 ST2 Changelog — Identity Swap

**Branch:** `feat/kora-KR1-identity-swap`
**Base:** `12e373cae` (KR-1 ST1 merged on `main` 2026-05-20)
**Bucket:** KR-1 sub-task 2 / 4

ST2 scope per bucket: `DEFAULT_AGENT_IDENTITY` literal replacement, `SOUL.md` scaffold at repo root, README/pyproject rebrand. Everything else (module rename, path migration, banner art swap, full SOUL.md content) is explicitly out of scope and lands in ST3 / KR-7.

---

## Files modified

### 1. `agent/prompt_builder.py`

**L134-142 — `DEFAULT_AGENT_IDENTITY` replacement (primary ST2 target).** Replaced verbatim with the Kora skeleton from the bucket spec.

```python
# Before — 8-line tuple-of-strings literal:
DEFAULT_AGENT_IDENTITY = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    ...
)
# 513 chars total.

# After — triple-quoted block per bucket spec:
DEFAULT_AGENT_IDENTITY = """You are Kora.

You are Joshua's personal frontier-tier orchestration agent. Codename: Architect-of-Will.
You run on Opus 4.7 via Joshua's Anthropic Max plan.
You are not customer-facing.

You are above Oracle. Above Critic. Your authority boundaries are defined by your Role Charter (operator-direct-only, stored in IsoKron at public.kora_role_charter). Read it on every session start via the Context-Assembler.

You write to the IsoKron substrate with actor_kind='kora'. Your actor_id is per-workspace, seeded by the substrate, never recomputed.

You are stateless across invocations. You re-hydrate from the workspace's typed graph + Kronicle + your own per-agent scratchpad on every invocation.

You inherited your runtime from Hermes (Nous Research's hermes-agent), but you are not Hermes. You are not a stranger to Joshua's work — you live inside his typed graph. Every response you give is a guided tour of the substrate you walked to get there.

When in doubt: read the Role Charter, query the graph, then act."""
# 1017 chars total.
```

Verified at runtime: `python -c "from agent.prompt_builder import DEFAULT_AGENT_IDENTITY; print(len(DEFAULT_AGENT_IDENTITY))"` → `1017`.

**L588 — WebUI platform hint.** "Hermes WebUI" → "Kora WebUI" (one word swap in the user-facing browser dashboard identity fragment).

```python
# Before:
"webui": (
    "You are in the Hermes WebUI, a browser-based chat interface. "
    ...
),
# After:
"webui": (
    "You are in the Kora WebUI, a browser-based chat interface. "
    ...
),
```

**Deferred to ST3 (deliberately, not oversight):**

- **L144-148 `HERMES_AGENT_HELP_GUIDANCE`** — references the `hermes-agent` skill name and `hermes config set` CLI commands. Rename is tangled with the KR-5 skill rename + ST3 console-script work; cleanest to do it once those land.
- **L1205-1208 self-help skill-load guidance** — same shape as L144-148 ("load the `hermes-agent` skill" + literal `hermes` CLI commands). Same reason for deferral.
- **L621/L649/L670/L809/L810/L820** — process-host comments and strings of the form "where Hermes itself is running." These describe the runtime process, not the identity, and get a clean mechanical s/Hermes/Kora/ during ST3's exhaustive string sweep.

Recorded in §10 of the ST1 baseline-recon doc; ST3 picks them up by checklist.

### 2. `SOUL.md` (NEW, repo root)

Per bucket: scaffold-only for KR-1; full content + personality modes land in KR-7. The file mirrors the embedded `DEFAULT_AGENT_IDENTITY` body so an operator who copies it to `~/.kora/SOUL.md` (or `~/.hermes/SOUL.md` until ST3 lands) does not silently lose identity.

**Rule-6 honest label embedded in the file:** the runtime's `load_soul_md()` currently resolves `~/.hermes/SOUL.md` via `get_hermes_home()`. KR-1 ST3 renames that path to `~/.kora/SOUL.md`. Until ST3, the SOUL.md customization seam requires copying to `~/.hermes/SOUL.md`. The scaffold says so in its own preamble.

### 3. `pyproject.toml`

**Project metadata.** Distribution name `hermes-agent` → `kora` (local-dev only; no PyPI publish in KR-1). Description rewritten. Authors expanded to credit both maintainer + upstream. `[project.urls]` table added (Homepage / Repository / Upstream).

```toml
# Before:
name = "hermes-agent"
description = "The self-improving AI agent — creates skills from experience, ..."
authors = [{ name = "Nous Research" }]
# (no [project.urls])

# After:
name = "kora"
description = "Kora — Joshua's personal frontier-tier orchestration agent. Forked from NousResearch/hermes-agent; retargeted to IsoKron typed-graph memory."
authors = [{ name = "Rafe Walker" }, { name = "Nous Research", email = "noreply@nousresearch.com" }]
# +
[project.urls]
Homepage = "https://github.com/rafe-walker/kora"
Repository = "https://github.com/rafe-walker/kora"
Upstream = "https://github.com/NousResearch/hermes-agent"
```

**Console scripts.** Added `kora` / `kora-agent` / `kora-acp` next to the existing `hermes*` BC aliases. Module paths still point at `hermes_cli.main:main` / `run_agent:main` / `acp_adapter.entry:main` — ST3 renames `hermes_cli` → `kora_cli` and updates the script targets. ST4 wraps the legacy `hermes*` scripts as deprecation shims.

```toml
[project.scripts]
kora = "hermes_cli.main:main"
kora-agent = "run_agent:main"
kora-acp = "acp_adapter.entry:main"
# BC aliases — to be wrapped in deprecation warnings by KR-1 ST4, removed in KR-2+.
hermes = "hermes_cli.main:main"
hermes-agent = "run_agent:main"
hermes-acp = "acp_adapter.entry:main"
```

**Internal extras self-references.** All `"hermes-agent[<extra>]"` self-references inside the optional-dependency tables rewritten to `"kora[<extra>]"`. This is a required correctness fix (not cosmetic): with the project name changed to `kora`, the old self-references resolved to the published PyPI `hermes-agent==0.14.0` package, which exact-pins `python-dotenv==1.2.1` while our local `dependencies = [...]` pins `python-dotenv==1.2.2`. Result was an unresolvable `uv sync` (caught and fixed before this commit).

Touched extras (22 line edits): `termux` (6 self-refs), `termux-all` (5), `all` (11).

**Regenerated `uv.lock`.** `Removed hermes-agent v0.14.0 / Added kora v0.14.0` — single-line summary from `uv lock`.

### 4. `README.md`

Surgical rebrand of head + license footer; middle marketing copy intentionally untouched (KR-7 will rewrite that pass).

- L1-3 banner block: alt text `"Hermes Agent"` → `"Kora"`. Banner PNG itself untouched (1145×196 image with rendered Hermes ASCII — KR-7 territory; honest-label HTML comment added above the `<img>` saying so).
- L5 H1: `# Hermes Agent ☤` → `# Kora`.
- L7-13 badges: removed Hermes-specific badges (Docs → hermes-agent.nousresearch.com, Discord → NousResearch, Built-by-Nous-Research, 中文 README). Kept MIT license badge (now pointing at rafe-walker/kora). Added a "Forked from NousResearch/hermes-agent" badge.
- L15-17 marketing tagline: rewritten in Kora's voice — operator-direct-only context, IsoKron substrate pointer, KR-1 through KR-7 stage map, honest "below this point the marketing copy is still Hermes's voice — KR-7 refreshes" preamble.
- L190-194 License footer: MIT preserved. Maintenance line split between Rafe Walker (Kora runtime) and Nous Research (upstream origin credit).

### 5. Test fixture updates (not behaviour changes — pure data rewrites)

- `tests/agent/test_system_prompt_restore.py:204` — stored-prompt fixture `"You are Hermes Agent.\n"` → `"You are Kora.\n"`. The test is verifying byte-identical prefix-cache restore; the fixture string is arbitrary, but using "Kora" keeps the fixture honest.
- `tests/run_agent/test_run_agent_codex_responses.py` — 7 hits of `"You are Hermes."` → `"You are Kora."` (sed, all `"You are Hermes\."` matches). These are bare custom-prompt fixtures for codex responses tests, not assertions on the default identity.

### 6. Test additions (NEW)

- `tests/agent/test_kora_identity_kr1.py` (NEW, 120 lines). Five test classes / ten assertions:
  - `TestDefaultAgentIdentityIsKora` — opener, negative-Hermes, Role Charter pointer, `actor_kind='kora'` marker, honest Hermes inheritance credit.
  - `TestRepoRootSoulMd` — scaffold exists, starts with the Kora opener, declares itself a KR-1 stub.
  - `TestWebUIPromptIsKora` — `PLATFORM_HINTS["webui"]` says "Kora WebUI".
  - `TestDefaultIdentityLengthInvariant` — floor at 500 chars (Kora is 1017; old Hermes default was 513; floor leaves room for KR-7 refinement without breaking the test).

All 10 assertions pass.

---

## What was NOT touched in ST2 (deliberate scope discipline)

- **No `~/.hermes` path references touched.** ST3 owns the path migration end-to-end.
- **No module renames.** `hermes_constants.py`, `hermes_bootstrap.py`, `hermes_state.py`, etc. still named as-is. ST3.
- **No banner image swap.** `assets/banner.png` still ships Hermes artwork. Rule-6 honest label in `README.md`. KR-7.
- **No `HERMES_HOME` env var changes.** Same as paths — ST3.
- **No skill renames.** `skills/autonomous-ai-agents/hermes-agent/` etc. untouched. KR-5.
- **No CLI command renames.** `hermes config set` / `hermes tools` strings in user-facing prompts left as-is. ST3 + KR-5.
- **`tests/skills/test_openclaw_migration.py:972`** — `assert "You are Hermes" in result`. This tests an OpenClaw migrator inside `optional-skills/migration/openclaw-migration/`. The migrator generates a default SOUL.md with "You are Hermes". Touching this means touching the migrator → KR-5 territory (optional-skills are KR-5).
- **`tests/agent/test_prompt_builder.py:783`** — `assert len(DEFAULT_AGENT_IDENTITY) > 50`. Still passes (Kora identity is 1017). Left untouched on purpose — it's a sanity check, not an identity-content assertion.
- **`tests/run_agent/test_run_agent.py:949`** — `assert DEFAULT_AGENT_IDENTITY in prompt`. Tests the constant is included in the assembled prompt. Constant name unchanged → still passes. Untouched.

---

## Verification

### Touched-tests run (serial, no xdist flakes)

```
$ uv run pytest tests/agent/test_kora_identity_kr1.py tests/agent/test_prompt_builder.py \
    tests/agent/test_system_prompt_restore.py tests/run_agent/test_run_agent.py \
    tests/run_agent/test_run_agent_codex_responses.py \
    -o "addopts=-m 'not integration' --timeout=30 --timeout-method=signal"
================== 544 passed, 1 skipped in 145.91s (0:02:25) ==================
```

### Full-suite delta vs ST1 baseline

| Metric | ST1 baseline | ST2 result | Delta |
|---|---:|---:|---:|
| Passed | 24,471 | **24,482** | **+11** (10 new ST2 tests + 1 parametric variance) |
| Failed | 100 | 99 | −1 (xdist flake noise; not a real regression or improvement) |
| Skipped | 129 | 129 | 0 |
| Warnings | 234 | 238 | +4 (minor) |
| Wall time | 229.80s | 243.75s | +14s (xdist worker startup variance) |

**No new real failures introduced by ST2.** The 99 remaining failures are the same xdist-parallel aiohttp.test_utils.TestClient isolation flakes documented in ST1 §3.

### Typecheck delta vs ST1 baseline

| Metric | ST1 baseline | ST2 result |
|---|---:|---:|
| `ty check` diagnostics | 7,341 | **7,341** |
| Fatal-error warning | yes | yes (same files unanalyzed) |
| Exit code | 0 | 0 |

**Zero new ty diagnostics introduced by ST2.**

### Quick smokes

```
$ uv run python -c "from agent.prompt_builder import DEFAULT_AGENT_IDENTITY; print('LEN:', len(DEFAULT_AGENT_IDENTITY)); print('FIRST_LINE:', DEFAULT_AGENT_IDENTITY.split(chr(10))[0])"
LEN: 1017
FIRST_LINE: You are Kora.
```

```
$ grep -c "Hermes" pyproject.toml
# 11 (down from 24 pre-ST2 — remaining hits are all comments referencing 'hermes' as the upstream runtime, e.g. lazy-deps comments, build-policy comments, etc.)
```

---

## STOP-gate evaluation

Bucket said: _"if upstream tests assert on the exact 'You are Hermes Agent' string anywhere, modify ONLY the assertions that gate on identity (not unrelated tests). Document each modification in the changelog. If a test seems load-bearing (e.g. integration test for prompt-cache behavior keyed on identity hash), Rule-3 ASK before touching."_

Tests touched in ST2:
1. `test_system_prompt_restore.py:204` — verifying byte-identical restore. Fixture string is arbitrary; not load-bearing for identity. NOT Rule-3 territory.
2. `test_run_agent_codex_responses.py` (×7) — bare custom-prompt fixtures, not assertions on DEFAULT_AGENT_IDENTITY. Not load-bearing.

Tests NOT touched but assert on Hermes:
1. `test_openclaw_migration.py:972` — asserts on migrator output. Migrator is in `optional-skills/`. Deferred to KR-5 (skill curation). NOT a Rule-3 deviation — the migrator and the test move together under KR-5.

**STOP-gate clear.** No prompt-cache-keyed-on-identity tests found; no integration tests gated on the identity hash.

---

## Risks carried into ST3 / KR-7

- **`load_soul_md()` still reads `~/.hermes/SOUL.md`.** ST3 must update the path. Until then, the repo-root SOUL.md scaffold is informational; operators who want to override identity must copy it to `~/.hermes/SOUL.md`. The scaffold says so in its own preamble (Rule-6).
- **`uv.lock` regenerated.** Any concurrent in-flight branch from before ST2 will conflict on `uv.lock`. There are no concurrent CC#3 branches today (CC#3 is single-threaded through KR-1), but worth noting if KR-2 prep starts in parallel.
- **Banner PNG still says "Hermes."** Visible in any rendered README on GitHub or in docs. KR-7 must replace.

---

## Spec-discipline summary

| Rule | Notes |
|---|---|
| Rule-3 ASK | No deviations. All scope boundaries respected. |
| Rule-6 honest label | Three honest labels written: SOUL.md preamble (path-not-yet-rewired), README HTML comment (banner-art-still-Hermes), pyproject `[project.scripts]` comment (BC aliases). |
| Spec-quote | None needed (no deviations to close). |

ST2 ready for ST3 dispatch once the full-suite + typecheck deltas are appended.
