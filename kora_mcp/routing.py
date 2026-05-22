"""Tool-name routing helpers (KR-MCP-1 ST1).

The pool addresses tools at endpoint ``<prefix>`` as
``<prefix>__<tool_name>``. The agent loop's tool-dispatcher uses
:func:`parse_qualified_tool_name` to split a qualified name into
``(prefix, tool_name)`` before routing into the pool.

Why ``__`` (double underscore): MCP tools commonly use snake_case
(``create_issue``, ``list_repos``). A single underscore would
collide with tool names that happen to start with a prefix-like
substring. The double-underscore convention matches the existing
``kora__*`` substrate-tool family and is reserved-by-convention
across the runtime.
"""

from __future__ import annotations


class InvalidQualifiedToolName(ValueError):
    """Raised when a tool name doesn't contain the required ``__``
    prefix separator."""


_SEPARATOR = "__"


def parse_qualified_tool_name(qualified: str) -> tuple[str, str]:
    """Split a qualified tool name into ``(prefix, tool_name)``.

    Examples:

      - ``"github__create_issue"`` → ``("github", "create_issue")``
      - ``"cloudflare__d1_database_query"`` →
        ``("cloudflare", "d1_database_query")``
      - ``"foo__bar__baz"`` → ``("foo", "bar__baz")`` (split on
        FIRST occurrence; the rest of the name can contain the
        separator)

    Raises:
        InvalidQualifiedToolName: if ``qualified`` contains no
            ``__`` separator, or the prefix or tool_name is empty.
    """
    if not isinstance(qualified, str):
        raise InvalidQualifiedToolName(
            f"qualified tool name must be a string; got {type(qualified).__name__}"
        )
    if _SEPARATOR not in qualified:
        raise InvalidQualifiedToolName(
            f"qualified tool name {qualified!r} must contain "
            f"{_SEPARATOR!r} separator (format: '<prefix>__<tool_name>')"
        )
    prefix, _, tool_name = qualified.partition(_SEPARATOR)
    if not prefix:
        raise InvalidQualifiedToolName(
            f"qualified tool name {qualified!r} has empty prefix"
        )
    if not tool_name:
        raise InvalidQualifiedToolName(
            f"qualified tool name {qualified!r} has empty tool_name"
        )
    return (prefix, tool_name)


def qualify_tool_name(prefix: str, tool_name: str) -> str:
    """Inverse of :func:`parse_qualified_tool_name` — useful for
    tests + the operator-UI's tool-listing surface.

    Example: ``qualify_tool_name("github", "create_issue")`` →
    ``"github__create_issue"``.
    """
    if not prefix:
        raise InvalidQualifiedToolName("prefix must be non-empty")
    if not tool_name:
        raise InvalidQualifiedToolName("tool_name must be non-empty")
    return f"{prefix}{_SEPARATOR}{tool_name}"
