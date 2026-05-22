"""Kora's reasoning surface — KR-FEAT-AI-RESPONSE-LOOP.

The reasoning layer takes an inbound message (Slack DM today; email
+ MCP-driven later) + thread context + Kora's operational state +
the cost-ladder rung, calls an LLM, and returns a response. Handler
modules compose ``ReasoningEngine.respond(...)``; the engine wraps
the SDK call + cost-ladder-aware model selection + audit.
"""
