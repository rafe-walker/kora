"""Static map: Hermes tool name → required Kora capability (cap_*).

KR-P2-A ST1. Consumed by ``agent/constitution_pre_screen.py``.

The pre-screen middleware uses this map to determine which capability
must be granted for a tool call to be permitted under the active
Constitution + capability matrix. Tools NOT in the map cause the
pre-screen to return INCONCLUSIVE — the fail-CLOSED default per
``feedback_fail_closed_by_default_security_infra``: an unmapped tool
requires operator adjudication rather than silent allow.

# Capability namespace

The ``cap_*`` names below are the **infrastructure-tier** capability
namespace this bucket introduces. They are NOT yet present in the
substrate-side C2 capability matrix mirror (which holds Sea/policy-tier
caps like ``cap_sea_create``, ``cap_propose_policy_change``).

Today every infra-tier ``cap_*`` triggers ``KeyError`` in
``actor_has_capability``; the pre-screen catches that and returns
INCONCLUSIVE — so every infra-tier tool call escalates until a
follow-up bucket extends the C2 mirror to cover these caps. The
escalation IS the fail-CLOSED behavior; it intentionally trades
ergonomics for safety until the cap matrix catches up.

# Substrate kora__* tools

The four substrate-tier ``kora__*`` MCP tools (``kora__append_event``,
``kora__write_agent_scratchpad``, ``kora__create_relationlink``,
``kora__read_kora_capability_row``) are intentionally NOT in this
map. The pre-screen short-circuits any name starting with ``kora__``
with PASS — substrate-side dispatch is the authoritative capability
check for those (K-6 / K-7 / K-8 / K-9). Defense-in-depth note:
re-checking here would duplicate the substrate-side gate and risk
drift.

# MCP-registered tools (dynamic)

Upstream MCP servers register tools at runtime via
``tools/mcp_tool.py``. Their names are server-defined and unknown
at compile time, so this static map cannot cover them — they fall
into the unmapped bucket and escalate by default. Operators may add
entries for vetted MCP tools.
"""

from __future__ import annotations

from typing import Final, Mapping


class _UnknownToolSentinel:
    """Singleton sentinel returned from ``get_required_capability`` when
    a tool name is not in the static map.

    Distinct from ``None`` so callers can pattern-match the "unmapped"
    case without confusing it with a hypothetical pass-through entry.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — diagnostic only
        return "UNKNOWN_TOOL_SENTINEL"


UNKNOWN_TOOL_SENTINEL: Final = _UnknownToolSentinel()


TOOL_CAPABILITY_MAP: Final[Mapping[str, str]] = {
    # File operations
    "read_file":    "cap_local_file_io",
    "write_file":   "cap_local_file_io",
    "search_files": "cap_local_file_io",
    "patch":        "cap_local_file_io",
    # Shell + code execution
    "terminal":     "cap_local_shell_exec",
    "execute_code": "cap_local_shell_exec",
    "process":      "cap_local_shell_exec",
    # Browser automation
    "browser_back":       "cap_browser_automation",
    "browser_cdp":        "cap_browser_automation",
    "browser_click":      "cap_browser_automation",
    "browser_console":    "cap_browser_automation",
    "browser_dialog":     "cap_browser_automation",
    "browser_get_images": "cap_browser_automation",
    "browser_navigate":   "cap_browser_automation",
    "browser_press":      "cap_browser_automation",
    "browser_scroll":     "cap_browser_automation",
    "browser_snapshot":   "cap_browser_automation",
    "browser_type":       "cap_browser_automation",
    "browser_vision":     "cap_browser_automation",
    # Computer use (full desktop control)
    "computer_use": "cap_computer_use",
    # Web + search
    "web_search":  "cap_web_fetch",
    "web_extract": "cap_web_fetch",
    "x_search":    "cap_web_fetch",
    # Voice + audio
    "text_to_speech": "cap_voice_io",
    # Vision + image + video
    "image_generate": "cap_media_generate",
    "vision_analyze": "cap_media_generate",
    "video_generate": "cap_media_generate",
    "video_analyze":  "cap_media_generate",
    # Skills
    "skill_manage": "cap_skill_invoke",
    "skill_view":   "cap_skill_invoke",
    "skills_list":  "cap_skill_invoke",
    # Memory + sessions
    "memory":         "cap_memory_io",
    "session_search": "cap_memory_io",
    # Agent coordination
    "delegate_task": "cap_agent_coord",
    "clarify":       "cap_agent_coord",
    # Communication (outbound)
    "send_message":    "cap_outbound_message",
    "discord":         "cap_outbound_message",
    "discord_admin":   "cap_outbound_message",
    "yb_send_dm":      "cap_outbound_message",
    "yb_send_sticker": "cap_outbound_message",
    # Project mgmt + tasks
    "kanban_block":     "cap_local_task_mgmt",
    "kanban_comment":   "cap_local_task_mgmt",
    "kanban_complete":  "cap_local_task_mgmt",
    "kanban_create":    "cap_local_task_mgmt",
    "kanban_heartbeat": "cap_local_task_mgmt",
    "kanban_link":      "cap_local_task_mgmt",
    "kanban_list":      "cap_local_task_mgmt",
    "kanban_show":      "cap_local_task_mgmt",
    "kanban_unblock":   "cap_local_task_mgmt",
    "todo":             "cap_local_task_mgmt",
    "cronjob":          "cap_local_task_mgmt",
    # Specialized — Yuanbao (read-side)
    "yb_query_group_info":    "cap_yuanbao_read",
    "yb_query_group_members": "cap_yuanbao_read",
    "yb_search_sticker":      "cap_yuanbao_read",
    # Specialized — HomeAssistant
    "ha_call_service":  "cap_homeassistant_control",
    "ha_get_state":     "cap_homeassistant_read",
    "ha_list_entities": "cap_homeassistant_read",
    "ha_list_services": "cap_homeassistant_read",
    # Specialized — Feishu
    "feishu_doc_read":                   "cap_feishu_io",
    "feishu_drive_add_comment":          "cap_feishu_io",
    "feishu_drive_list_comment_replies": "cap_feishu_io",
    "feishu_drive_list_comments":        "cap_feishu_io",
    "feishu_drive_reply_comment":        "cap_feishu_io",
    # Mixture-of-agents (ensemble inference)
    "mixture_of_agents": "cap_ensemble_inference",
}


def get_required_capability(
    tool_name: str,
) -> str | _UnknownToolSentinel:
    """Return the ``cap_*`` required by ``tool_name``, or
    :data:`UNKNOWN_TOOL_SENTINEL` if not statically known.

    Substrate-tier ``kora__*`` tools are intentionally NOT in the map;
    the pre-screen short-circuits before calling this helper for them.
    """
    return TOOL_CAPABILITY_MAP.get(tool_name, UNKNOWN_TOOL_SENTINEL)
