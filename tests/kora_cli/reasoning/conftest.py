"""Reasoning-tests fixtures — KR-TEST-STABILITY-ROUTE-THROUGH-MOCKS-AND-HERMES-HOME-MIGRATION.

# Background

The ``test_anthropic_engine*`` family was written when
:meth:`AnthropicReasoningEngine.respond` ran the BYPASS path
(direct ``client.messages.create`` → ResponseResult projection)
by default. Those tests mock at ``client.messages.create`` —
exactly where the bypass loop calls the SDK.

#178 introduced the gateway route-through path. #195 flipped it
to be the production-DEFAULT. The bypass stays available via
``KORA_REASONING_USE_GATEWAY=false`` as an incident-response
escape hatch.

After the default-flip, every bypass-path test fails because
``respond()`` now routes through ``_respond_via_gateway`` →
``AIAgent.run_conversation`` → conversation_loop, which uses
its own client (not the test's mock) and hits the real
Anthropic API → 401 → ``ResponseResult.error = "gateway_incomplete"``.

# Resolution

This conftest force-enables the bypass path for every test in
``tests/kora_cli/reasoning/`` so the existing mocks find their
target. The gateway path is exercised by dedicated tests under
``tests/kora_cli/reasoning/kora_hermes_plugin/`` (the plugin
hook unit tests) + the integration-level smoke tests at
``tests/integration/``.

# Why per-directory conftest (not per-file fixture)

5 test files; 75+ test functions. A per-directory fixture is
DRYer and self-documenting — a future contributor adding a 6th
``test_anthropic_engine_FOO.py`` file inherits the bypass-mode
default automatically. If they want to test the gateway path,
they monkeypatch the env back to the production default in
their own per-test fixture.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _force_bypass_path(monkeypatch):
    """Force the bypass path for every reasoning test. Tests
    exercising the gateway path override per-test via
    ``monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "true")``.
    """
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "false")
