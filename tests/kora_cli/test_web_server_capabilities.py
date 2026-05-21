"""Tests for the KR-P2-CAP-PANEL capabilities inspector endpoint.

Bucket §5 scenarios (10 total):
  1. GET /api/capabilities returns 200
  2. Top-level shape: groups list + substrate_tier list + 3 ints
  3. Each group has cap_name / verdict / tools[]
  4. verdict values ∈ documented set
  5. Substrate-tier contains exactly the 4 kora__* tools
  6. total_tools == len(TOOL_CAPABILITY_MAP) + 4
  7. Every TOOL_CAPABILITY_MAP entry appears in exactly one group
  8. Groups sorted alphabetical by cap_name
  9. unmapped_count >= 0 (today: usually >= 1 per D-krp2a-st1, but
     the test must still pass after KR-P2-N flips the mirror)
 10. Cron-regression sanity
"""

import pytest


_VALID_VERDICTS = {"granted", "denied", "unmapped_in_c2_mirror", "error"}
_EXPECTED_SUBSTRATE_TIER = {
    "kora__append_event",
    "kora__write_agent_scratchpad",
    "kora__create_relationlink",
    "kora__read_kora_capability_row",
}


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path", lambda: tmp_path / "config.yaml"
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path", lambda: tmp_path / ".env"
    )
    return tmp_path


# ---- 1. 200 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_capabilities()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_top_level_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_capabilities()
    assert set(result.keys()) == {
        "groups",
        "substrate_tier",
        "total_tools",
        "total_caps",
        "unmapped_count",
    }
    assert isinstance(result["groups"], list)
    assert isinstance(result["substrate_tier"], list)
    assert isinstance(result["total_tools"], int)
    assert isinstance(result["total_caps"], int)
    assert isinstance(result["unmapped_count"], int)


# ---- 3. Per-group shape --------------------------------------------------


@pytest.mark.asyncio
async def test_groups_have_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_capabilities()
    for grp in result["groups"]:
        assert set(grp.keys()) == {"cap_name", "verdict", "tools"}
        assert isinstance(grp["cap_name"], str) and grp["cap_name"]
        assert isinstance(grp["tools"], list)
        assert all(isinstance(t, str) and t for t in grp["tools"])


# ---- 4. verdict enum ----------------------------------------------------


@pytest.mark.asyncio
async def test_verdict_values_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_capabilities()
    for grp in result["groups"]:
        assert grp["verdict"] in _VALID_VERDICTS, (
            f"group {grp['cap_name']} has unknown verdict {grp['verdict']!r}"
        )


# ---- 5. substrate_tier == exactly the 4 kora__* ------------------------


@pytest.mark.asyncio
async def test_substrate_tier_contains_exactly_four_kora_tools(_isolate_config):
    """Pinned by bucket §3 contract. If a 5th kora__* tool ever ships,
    the substrate-tier list AND this test need updating in lockstep."""
    from kora_cli import web_server

    result = await web_server.get_capabilities()
    assert set(result["substrate_tier"]) == _EXPECTED_SUBSTRATE_TIER
    assert len(result["substrate_tier"]) == 4


# ---- 6. total_tools accounting -------------------------------------------


@pytest.mark.asyncio
async def test_total_tools_equals_map_size_plus_substrate(_isolate_config):
    from kora_cli import web_server
    from agent.tool_capability_map import TOOL_CAPABILITY_MAP

    result = await web_server.get_capabilities()
    assert result["total_tools"] == len(TOOL_CAPABILITY_MAP) + 4


# ---- 7. Every map entry in exactly one group ----------------------------


@pytest.mark.asyncio
async def test_every_map_entry_appears_in_exactly_one_group(_isolate_config):
    """No duplicates, no orphans. The endpoint groups by cap_name so
    every (tool, cap) pair must land in exactly one group's tools[]."""
    from kora_cli import web_server
    from agent.tool_capability_map import TOOL_CAPABILITY_MAP

    result = await web_server.get_capabilities()
    surfaced: dict[str, str] = {}  # tool_name → cap_name it appeared under
    for grp in result["groups"]:
        for tool in grp["tools"]:
            assert tool not in surfaced, (
                f"tool {tool!r} appears in both {surfaced[tool]!r} and {grp['cap_name']!r}"
            )
            surfaced[tool] = grp["cap_name"]

    # No orphans (every map entry surfaced)
    assert set(surfaced.keys()) == set(TOOL_CAPABILITY_MAP.keys())

    # Cap assignments match the source-of-truth
    for tool, expected_cap in TOOL_CAPABILITY_MAP.items():
        assert surfaced[tool] == expected_cap, (
            f"tool {tool!r} surfaced under {surfaced[tool]!r} but "
            f"TOOL_CAPABILITY_MAP says {expected_cap!r}"
        )


# ---- 8. Groups sorted alphabetical --------------------------------------


@pytest.mark.asyncio
async def test_groups_sorted_alphabetical_by_cap_name(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_capabilities()
    cap_names = [g["cap_name"] for g in result["groups"]]
    assert cap_names == sorted(cap_names)


# Bucket §3: tools within a group should also be sorted.
@pytest.mark.asyncio
async def test_tools_within_each_group_sorted(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_capabilities()
    for grp in result["groups"]:
        assert grp["tools"] == sorted(grp["tools"]), (
            f"group {grp['cap_name']} tools not sorted: {grp['tools']}"
        )


# ---- 9. unmapped_count >= 0 (today usually >= 1) ------------------------


@pytest.mark.asyncio
async def test_unmapped_count_non_negative_and_self_consistent(_isolate_config):
    """The bucket notes today's main has unmapped infra-tier caps
    (D-krp2a-st1), so we EXPECT >= 1 in current state. But after KR-P2-N
    closes the mirror gap, that count drops to 0 and this test should
    keep passing — assert non-negative + cross-check against the groups
    array rather than pinning a specific count."""
    from kora_cli import web_server

    result = await web_server.get_capabilities()
    assert result["unmapped_count"] >= 0

    counted_in_groups = sum(
        1 for g in result["groups"] if g["verdict"] == "unmapped_in_c2_mirror"
    )
    assert result["unmapped_count"] == counted_in_groups


# ---- 10. Cron-regression sanity -----------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_capabilities_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)


# Bonus: substrate_tier disjoint from TOOL_CAPABILITY_MAP (otherwise
# the test for "every map entry appears in exactly one group" gets
# noisy since substrate-tier tools would double-count).
@pytest.mark.asyncio
async def test_substrate_tier_disjoint_from_tool_capability_map(_isolate_config):
    from kora_cli import web_server
    from agent.tool_capability_map import TOOL_CAPABILITY_MAP

    result = await web_server.get_capabilities()
    overlap = set(result["substrate_tier"]) & set(TOOL_CAPABILITY_MAP.keys())
    assert overlap == set(), (
        f"substrate_tier overlaps with TOOL_CAPABILITY_MAP: {overlap}. "
        "Either remove from substrate_tier (already mapped) or remove from "
        "TOOL_CAPABILITY_MAP (substrate enforces, pre-screen short-circuits)."
    )
