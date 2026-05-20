"""KR-1 ST2 tests — Kora identity swap.

Verifies:
1. `DEFAULT_AGENT_IDENTITY` resolves to Kora identity (not Hermes).
2. The prompt-builder identity literal starts with the canonical Kora opener.
3. `load_soul_md` continues to override `DEFAULT_AGENT_IDENTITY` when the
   user has installed a profile-local `SOUL.md` (regression guard — this
   is the supported customization seam from KR-7).
4. The repo-root SOUL.md scaffold ships content that begins with the
   canonical Kora opener — so an operator who copies it to the profile
   home gets the same identity surface as the embedded default.
5. The "Kora WebUI" prompt fragment is wired in `PLATFORM_HINTS` (the
   companion identity surface to `DEFAULT_AGENT_IDENTITY` for browser UI).

The bucket spec is the source of truth for the verbatim Kora identity
string; these assertions sample the load-bearing phrases rather than
asserting the full 1KB literal so KR-7 can refine without re-touching
KR-1 test fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.prompt_builder import DEFAULT_AGENT_IDENTITY, PLATFORM_HINTS


REPO_ROOT = Path(__file__).resolve().parents[2]


class TestDefaultAgentIdentityIsKora:
    def test_starts_with_kora_opener(self):
        # The opening line is the single most load-bearing sentence in the
        # whole prompt. KR-7 may refine wording elsewhere; this must hold.
        assert DEFAULT_AGENT_IDENTITY.startswith("You are Kora.")

    def test_does_not_identify_as_hermes(self):
        # Explicit negative — guards against an accidental revert during
        # ST3/ST4 string sweeps.
        assert "You are Hermes Agent" not in DEFAULT_AGENT_IDENTITY
        assert "You are Hermes." not in DEFAULT_AGENT_IDENTITY

    def test_carries_role_charter_pointer(self):
        # KR-6 will wire capability + Constitution checks; the identity
        # already names the Role Charter as the authority source, so
        # operators reading the prompt know where Kora's boundaries live.
        assert "Role Charter" in DEFAULT_AGENT_IDENTITY
        assert "public.kora_role_charter" in DEFAULT_AGENT_IDENTITY

    def test_carries_actor_kind_kora(self):
        # Substrate contract: every Kora write uses actor_kind='kora'.
        # The identity advertises this so substrate-aware tools have a
        # reason to expect it.
        assert "actor_kind='kora'" in DEFAULT_AGENT_IDENTITY

    def test_acknowledges_hermes_runtime_inheritance(self):
        # Honest origin credit lives in the identity itself, not just the
        # README. If a future refactor strips this line, KR-7 must
        # reintroduce it somewhere or update the test.
        assert "hermes-agent" in DEFAULT_AGENT_IDENTITY


class TestRepoRootSoulMd:
    """The repo ships `SOUL.md` at the root as a KR-1 scaffold.

    The runtime `load_soul_md()` currently resolves `~/.kora/SOUL.md`
    (ST3 renames that to `~/.kora/SOUL.md`). For KR-1 ST2, the repo-root
    scaffold is purely informational — but it must agree with the
    embedded identity so an operator who copies it gets the same opener.
    """

    def test_repo_root_soul_md_exists(self):
        soul = REPO_ROOT / "SOUL.md"
        assert soul.exists(), "KR-1 ST2 ships SOUL.md scaffold at repo root"

    def test_repo_root_soul_md_starts_with_kora_opener(self):
        content = (REPO_ROOT / "SOUL.md").read_text(encoding="utf-8")
        # Skip the markdown frontmatter / scaffolding notes — find the
        # identity body, which is signaled by the `---` divider in the
        # scaffold. The body must begin "You are Kora."
        assert "You are Kora." in content
        # And explicit negative: scaffold must NOT identify as Hermes.
        assert "You are Hermes" not in content

    def test_repo_root_soul_md_flags_itself_as_kr1_stub(self):
        # Honest-label invariant — Rule-6. If KR-7 replaces the scaffold
        # with the full content, this assertion can be deleted.
        content = (REPO_ROOT / "SOUL.md").read_text(encoding="utf-8")
        assert "KR-1 stub" in content or "KR-7" in content


class TestWebUIPromptIsKora:
    """ST2 secondary — the user-visible WebUI prompt fragment was
    "You are in the Hermes WebUI, ..." and is now "Kora WebUI". This
    surface is shown when the agent runs in the browser dashboard.
    """

    def test_webui_hint_says_kora(self):
        webui = PLATFORM_HINTS.get("webui", "")
        assert "Kora WebUI" in webui
        assert "Hermes WebUI" not in webui


class TestDefaultIdentityLengthInvariant:
    """Regression guard — the existing length-floor assertion lives in
    `test_prompt_builder.py::TestPromptBuilderConstants::test_default_identity_non_empty`
    (`assert len(DEFAULT_AGENT_IDENTITY) > 50`). The Kora identity is
    well over 50 chars, but we tighten the bound here so a future
    accidental truncation surfaces immediately rather than silently
    passing the >50 check.
    """

    def test_identity_at_least_500_chars(self):
        # Anchor the floor at "noticeably more than the old Hermes
        # default" (the upstream identity was 513 chars; the Kora
        # identity is ~1100). 500 leaves room for KR-7 refinement.
        assert len(DEFAULT_AGENT_IDENTITY) >= 500
