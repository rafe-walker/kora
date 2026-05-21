"""KR-P2-B: assert operator-facing identity strings derive from
``PlatformConfig.display_name`` rather than the hardcoded "Hermes" literal.

Covers:
- Slack handoff-thread seed (gateway/platforms/slack.py:create_handoff_thread)
- Email subject default across all three send paths
  (_send_email, _send_email_with_attachments, _send_email_with_attachment)
"""

import os
import sys
from email.message import Message
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _ensure_slack_mock():
    """Install mock slack modules so SlackAdapter can be imported."""
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return  # Real library installed

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)

    sys.modules.setdefault("aiohttp", MagicMock())


_ensure_slack_mock()

import gateway.platforms.slack as _slack_mod  # noqa: E402
_slack_mod.SLACK_AVAILABLE = True
from gateway.platforms.slack import SlackAdapter  # noqa: E402


def _build_slack_adapter(display_name: str) -> SlackAdapter:
    config = PlatformConfig(enabled=True, token="xoxb-fake", display_name=display_name)
    adapter = SlackAdapter(config)
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._app.client.chat_postMessage = AsyncMock(return_value={"ts": "1700000000.0001"})
    adapter._running = True
    return adapter


class TestSlackHandoffThreadIdentity:
    @pytest.mark.asyncio
    async def test_default_display_name_renders_kora_handoff(self):
        adapter = _build_slack_adapter(display_name="Kora")

        await adapter.create_handoff_thread("C123", "session-1")

        adapter._app.client.chat_postMessage.assert_awaited_once()
        kwargs = adapter._app.client.chat_postMessage.await_args.kwargs
        assert "Kora handoff" in kwargs["text"]
        assert "Hermes" not in kwargs["text"]

    @pytest.mark.asyncio
    async def test_override_display_name_propagates_into_seed(self):
        adapter = _build_slack_adapter(display_name="testkoraalpha")

        await adapter.create_handoff_thread("C123", "session-1")

        kwargs = adapter._app.client.chat_postMessage.await_args.kwargs
        assert "testkoraalpha handoff" in kwargs["text"]
        assert "Hermes" not in kwargs["text"]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _build_email_adapter(display_name: str):
    with patch.dict(os.environ, {
        "EMAIL_ADDRESS": "kora@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_IMAP_HOST": "imap.test.com",
        "EMAIL_IMAP_PORT": "993",
        "EMAIL_SMTP_HOST": "smtp.test.com",
        "EMAIL_SMTP_PORT": "587",
        "EMAIL_POLL_INTERVAL": "15",
    }):
        from gateway.platforms.email import EmailAdapter
        return EmailAdapter(PlatformConfig(enabled=True, display_name=display_name))


def _captured_subject(mock_smtp) -> str:
    mock_smtp.return_value.send_message.assert_called_once()
    sent_msg: Message = mock_smtp.return_value.send_message.call_args[0][0]
    return sent_msg["Subject"]


class TestEmailSubjectIdentity:
    """All 3 email send paths must derive the subject default from
    ``self.config.display_name`` when there is no thread context."""

    def test_send_email_default_subject_uses_display_name(self):
        import asyncio

        adapter = _build_email_adapter(display_name="Kora")

        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            asyncio.run(adapter.send("user@test.com", "hello"))

        subject = _captured_subject(mock_smtp)
        # No thread context → default subject path → "Re: Kora Agent"
        assert "Kora Agent" in subject
        assert "Hermes" not in subject

    def test_send_document_default_subject_uses_display_name(self):
        import asyncio
        import tempfile

        adapter = _build_email_adapter(display_name="Kora")

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"doc")
            tmp_path = f.name

        try:
            with patch("smtplib.SMTP") as mock_smtp:
                mock_smtp.return_value = MagicMock()
                asyncio.run(adapter.send_document("user@test.com", tmp_path, "caption"))

            subject = _captured_subject(mock_smtp)
            assert "Kora Agent" in subject
            assert "Hermes" not in subject
        finally:
            os.unlink(tmp_path)

    def test_send_multiple_images_default_subject_uses_display_name(self):
        import asyncio
        import tempfile

        adapter = _build_email_adapter(display_name="Kora")

        tmp_paths = []
        try:
            for _ in range(2):
                f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                f.write(b"\x89PNG\r\n\x1a\n")
                f.close()
                tmp_paths.append(f.name)

            with patch("smtplib.SMTP") as mock_smtp:
                mock_smtp.return_value = MagicMock()
                asyncio.run(adapter.send_multiple_images("user@test.com", tmp_paths, "caption"))

            subject = _captured_subject(mock_smtp)
            assert "Kora Agent" in subject
            assert "Hermes" not in subject
        finally:
            for p in tmp_paths:
                os.unlink(p)

    def test_overridden_display_name_propagates_into_subject(self):
        import asyncio

        adapter = _build_email_adapter(display_name="testkoraalpha")

        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            asyncio.run(adapter.send("user@test.com", "hello"))

        subject = _captured_subject(mock_smtp)
        assert "testkoraalpha Agent" in subject
        assert "Hermes" not in subject
        assert "Kora Agent" not in subject

    def test_thread_context_subject_wins_over_default(self):
        """When the thread already has a subject (replying to user), that
        subject is preserved — display_name only fills the default path."""
        import asyncio

        adapter = _build_email_adapter(display_name="Kora")
        adapter._thread_context["user@test.com"] = {
            "subject": "Help with Python",
            "message_id": "<m1@t>",
        }

        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value = MagicMock()
            asyncio.run(adapter.send("user@test.com", "hello"))

        subject = _captured_subject(mock_smtp)
        assert "Help with Python" in subject
        assert "Kora Agent" not in subject
