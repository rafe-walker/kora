"""KR-P2-L ST1 — process-wide active poller singleton tests."""

from __future__ import annotations

import logging

import pytest

from plugins.memory.isokron.active_poller import (
    clear_active_poller,
    get_active_poller,
    set_active_poller,
)


@pytest.fixture(autouse=True)
def _reset():
    clear_active_poller()
    yield
    clear_active_poller()


def test_get_returns_none_before_any_set():
    assert get_active_poller() is None


def test_set_then_get_returns_same_instance():
    poller = object()
    set_active_poller(poller)
    assert get_active_poller() is poller


def test_set_same_instance_twice_is_idempotent(caplog):
    poller = object()
    set_active_poller(poller)
    with caplog.at_level(logging.WARNING, logger="plugins.memory.isokron.active_poller"):
        set_active_poller(poller)
    # No WARNING for setting the same instance.
    assert not any("replacing" in r.message for r in caplog.records)


def test_set_different_instance_warns(caplog):
    set_active_poller(object())
    second = object()
    with caplog.at_level(logging.WARNING, logger="plugins.memory.isokron.active_poller"):
        set_active_poller(second)
    assert any("replacing" in r.message for r in caplog.records)
    assert get_active_poller() is second


def test_clear_resets_singleton():
    set_active_poller(object())
    assert get_active_poller() is not None
    clear_active_poller()
    assert get_active_poller() is None
