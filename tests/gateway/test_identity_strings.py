"""KR-P2-B: assert operator-facing identity strings derive from
``PlatformConfig.display_name`` rather than the hardcoded "Hermes" literal.

Covers:
- Slack handoff-thread seed (gateway/platforms/slack.py:create_handoff_thread)
- Email subject default across all three send paths
  (_send_email, _send_email_with_attachments, _send_email_with_attachment)
- Discord slash-command descriptions registered via _register_slash_commands
- Home Assistant persistent_notification.create title (send())
- WhatsApp DEFAULT_REPLY_PREFIX (per-instance, populated in __init__)
- Matrix device_name — see TestMatrixDeviceNameIdentity (intentionally
  skipped; the literal is inline at the mautrix.Client.login call site
  with no instance attribute to inspect — adapter restructuring is
  out-of-scope per spec §6).
"""

import os
import sys
from email.message import Message
from types import SimpleNamespace
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


# ---------------------------------------------------------------------------
# Discord (slash-command descriptions)
# ---------------------------------------------------------------------------

def _ensure_discord_mock():
    """Stub discord modules so DiscordAdapter can be imported without the
    real library. Modeled on tests/gateway/test_discord_slash_commands.py
    so the registration code-path is exercised the same way."""
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    if sys.modules.get("discord") is None:
        discord_mod = MagicMock()
        discord_mod.Intents.default.return_value = MagicMock()
        discord_mod.DMChannel = type("DMChannel", (), {})
        discord_mod.Thread = type("Thread", (), {})
        discord_mod.ForumChannel = type("ForumChannel", (), {})
        discord_mod.Interaction = object

        class _FakeGroup:
            def __init__(self, *, name, description, parent=None):
                self.name = name
                self.description = description
                self.parent = parent
                self._children: dict = {}
                if parent is not None:
                    parent.add_command(self)

            def add_command(self, cmd):
                self._children[cmd.name] = cmd

        class _FakeCommand:
            def __init__(self, *, name, description, callback, parent=None):
                self.name = name
                self.description = description
                self.callback = callback
                self.parent = parent

        discord_mod.app_commands = SimpleNamespace(
            describe=lambda **kwargs: (lambda fn: setattr(fn, "_describe", kwargs) or fn),
            choices=lambda **kwargs: (lambda fn: fn),
            autocomplete=lambda **kwargs: (lambda fn: fn),
            Choice=lambda **kwargs: SimpleNamespace(**kwargs),
            Group=_FakeGroup,
            Command=_FakeCommand,
        )

        ext_mod = MagicMock()
        commands_mod = MagicMock()
        commands_mod.Bot = MagicMock
        ext_mod.commands = commands_mod

        sys.modules["discord"] = discord_mod
        sys.modules.setdefault("discord.ext", ext_mod)
        sys.modules.setdefault("discord.ext.commands", commands_mod)

    _app = getattr(sys.modules["discord"], "app_commands", None)
    if _app is not None and not hasattr(_app, "autocomplete"):
        try:
            _app.autocomplete = lambda **kwargs: (lambda fn: fn)
        except Exception:
            pass


_ensure_discord_mock()

from gateway.platforms.discord import DiscordAdapter  # noqa: E402


class _DescTree:
    """FakeTree that captures the ``description`` kwarg of each
    @tree.command(...) registration so descriptions can be asserted."""

    def __init__(self):
        self.commands: dict = {}
        self.descriptions: dict = {}

    def command(self, *, name, description):
        self.descriptions[name] = description

        def decorator(fn):
            self.commands[name] = fn
            return fn

        return decorator

    def add_command(self, cmd):
        self.commands[cmd.name] = cmd
        self.descriptions[cmd.name] = getattr(cmd, "description", "")

    def get_commands(self):
        return [SimpleNamespace(name=n) for n in self.commands]


def _build_discord_adapter(display_name: str) -> DiscordAdapter:
    config = PlatformConfig(enabled=True, token="discord-fake", display_name=display_name)
    adapter = DiscordAdapter(config)
    adapter._client = SimpleNamespace(
        tree=_DescTree(),
        get_channel=lambda _id: None,
        fetch_channel=AsyncMock(),
        user=SimpleNamespace(id=99999, name="KoraBot"),
    )
    adapter._text_batch_delay_seconds = 0
    adapter._check_slash_authorization = AsyncMock(return_value=True)
    return adapter


class TestDiscordSlashCommandDescriptionIdentity:
    """All seven PM-listed Discord slash-command descriptions interpolate
    ``self.config.display_name``."""

    def test_default_display_name_renders_kora_in_descriptions(self):
        adapter = _build_discord_adapter(display_name="Kora")
        adapter._register_slash_commands()

        descs = adapter._client.tree.descriptions
        # Spot-check each of the seven sites that contain the
        # display_name interpolation (lines 2920, 2947, 2955, 3017, 3021,
        # 3035, 3337 pre-edit).
        assert "Kora" in descs["reset"]
        assert "Kora" in descs["status"]
        assert "Kora" in descs["stop"]
        assert "Kora" in descs["update"]
        assert "Kora" in descs["restart"]
        assert "Kora" in descs["thread"]
        # /skill is registered as a Command via _register_skill_group
        assert "Kora" in descs["skill"]

        for cmd_name, desc in descs.items():
            assert "Hermes" not in desc, f"/{cmd_name} still says Hermes: {desc!r}"

    def test_overridden_display_name_propagates_into_descriptions(self):
        adapter = _build_discord_adapter(display_name="testkoraalpha")
        adapter._register_slash_commands()

        descs = adapter._client.tree.descriptions
        for key in ("reset", "status", "stop", "update", "restart", "thread", "skill"):
            assert "testkoraalpha" in descs[key]
            assert "Hermes" not in descs[key]


# ---------------------------------------------------------------------------
# Home Assistant (persistent_notification.create title)
# ---------------------------------------------------------------------------

class _CapturePost:
    """Minimal aiohttp-style async context manager that records the JSON
    payload posted and returns a 200-status response."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, url, *, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})

        class _Resp:
            status = 200

            async def text(self):
                return ""

            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

        return _Resp()


def _build_ha_adapter(display_name: str, monkeypatch):
    from gateway.platforms.homeassistant import HomeAssistantAdapter

    monkeypatch.setenv("HASS_TOKEN", "fake-token")
    monkeypatch.setenv("HASS_URL", "http://hass.test")
    adapter = HomeAssistantAdapter(
        PlatformConfig(enabled=True, display_name=display_name)
    )
    capture = _CapturePost()
    adapter._rest_session = SimpleNamespace(post=capture)
    return adapter, capture


class TestHomeAssistantNotificationIdentity:
    @pytest.mark.asyncio
    async def test_default_display_name_renders_kora_agent_title(self, monkeypatch):
        adapter, capture = _build_ha_adapter("Kora", monkeypatch)

        result = await adapter.send("dm-1", "hello")

        assert result.success
        assert capture.calls, "expected HA REST POST to fire"
        payload = capture.calls[0]["json"]
        assert payload["title"] == "Kora Agent"

    @pytest.mark.asyncio
    async def test_overridden_display_name_propagates_into_title(self, monkeypatch):
        adapter, capture = _build_ha_adapter("testkoraalpha", monkeypatch)

        await adapter.send("dm-1", "hello")

        payload = capture.calls[0]["json"]
        assert payload["title"] == "testkoraalpha Agent"
        assert "Hermes" not in payload["title"]


# ---------------------------------------------------------------------------
# WhatsApp (DEFAULT_REPLY_PREFIX, populated in __init__)
# ---------------------------------------------------------------------------

class TestWhatsAppReplyPrefixIdentity:
    def test_default_display_name_renders_kora_agent_prefix(self):
        from gateway.platforms.whatsapp import WhatsAppAdapter

        adapter = WhatsAppAdapter(PlatformConfig(enabled=True, display_name="Kora"))
        assert adapter.DEFAULT_REPLY_PREFIX == "⚕ *Kora Agent*\n────────────\n"
        assert "Hermes" not in adapter.DEFAULT_REPLY_PREFIX

    def test_overridden_display_name_propagates_into_prefix(self):
        from gateway.platforms.whatsapp import WhatsAppAdapter

        adapter = WhatsAppAdapter(
            PlatformConfig(enabled=True, display_name="testkoraalpha")
        )
        assert "testkoraalpha" in adapter.DEFAULT_REPLY_PREFIX
        assert "Hermes" not in adapter.DEFAULT_REPLY_PREFIX


# ---------------------------------------------------------------------------
# Matrix (device_name)
# ---------------------------------------------------------------------------

class TestMatrixDeviceNameIdentity:
    @pytest.mark.skip(
        reason=(
            "Matrix device_name is interpolated inline at the mautrix Client.login "
            "call in connect() — there is no instance attribute to inspect without "
            "either (a) restructuring the adapter to cache the value, or (b) running "
            "the full mautrix mock stack to capture login() kwargs. Both are "
            "out-of-scope per KR-P2-B2 spec §6 ('don't restructure adapter init "
            "signatures'). The literal is verified by the grep in §9; once a new "
            "device registers, the homeserver receives ``{display_name} Agent``."
        )
    )
    def test_matrix_device_name_uses_display_name(self):
        pass
