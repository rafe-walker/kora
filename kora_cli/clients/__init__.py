"""Outbound API clients (KR-FEAT-SLACK-DM ST2, KR-FEAT-EMAIL future, ...).

Distinct from ``kora_cli/listeners/`` (inbound transport) and
``kora_cli/handlers/`` (per-source business logic). A ``clients/``
module is a thin async wrapper around an outbound HTTP/SDK call —
auth, timeout, retry, error mapping. Handlers compose clients.
"""
