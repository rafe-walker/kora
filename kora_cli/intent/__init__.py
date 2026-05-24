"""Kora intent-recognition module — KR-INTENT-EMAIL-TO-SEA-TICKET.

Houses operator-intent recognizers that consume inbound signals
(email today; future buckets may add Slack-DM and webhook
variants) and turn them into structured actions (Sea_Ticket
creation, scratchpad writes, etc.).

v1 ships regex-based recognition only — no LLM call — so this
module is always-on cheap and runs inside the email-inbound
handler's request path without a cost-rung consideration.
"""
