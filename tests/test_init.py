"""
Tests for alarmdotcom's __init__.py: entry setup, auth-failure handling, and unload.

Contributes toward the Silver quality-scale "test-coverage" requirement.
Covers the core setup/teardown lifecycle plus runtime_data population —
the highest-value paths to keep green because a regression there makes the
whole integration silent (loaded but not actually working).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.alarmdotcom._pyalarmdotcomajax as pyadc
from custom_components.alarmdotcom.const import DOMAIN

VALID_DATA = {"username": "test@example.com", "password": "hunter2"}


@pytest.fixture
def mock_coordinator():
    """Build a mock AlarmCoordinator that initializes successfully."""
    coordinator = MagicMock()
    coordinator.initialize = AsyncMock(return_value=None)
    coordinator.close = AsyncMock(return_value=True)
    coordinator.api = MagicMock()
    coordinator.api.active_system = MagicMock()
    coordinator.api.active_system.id = "system-1"
    coordinator.api.active_system.name = "Test System"
    return coordinator


@pytest.fixture
def mock_auto_off_manager():
    """Build a mock AutoOffManager that skips storage I/O."""
    manager = MagicMock()
    manager.async_load = AsyncMock(return_value=None)
    manager.async_unload = AsyncMock(return_value=None)
    return manager


@pytest.fixture
def mock_camera_session():
    """Build a mock camera session that skips real login."""
    session = MagicMock()
    session.owns_session = False
    session.ajax_key = "mock-ajax-key"
    session.login = AsyncMock(return_value=None)
    session.close = AsyncMock(return_value=None)
    return session


async def test_setup_entry_success(
    hass: HomeAssistant, mock_coordinator, mock_auto_off_manager, mock_camera_session
) -> None:
    """A healthy setup: coordinator initializes, platforms load, entry ends up LOADED."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="12345", data=VALID_DATA)
    entry.add_to_hass(hass)

    with (
        patch("custom_components.alarmdotcom.AlarmCoordinator", return_value=mock_coordinator),
        patch("custom_components.alarmdotcom.AutoOffManager", return_value=mock_auto_off_manager),
        patch(
            "custom_components.alarmdotcom.AlarmCameraSession.from_alarm_bridge",
            return_value=mock_camera_session,
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert result is True
    assert entry.state == ConfigEntryState.LOADED
    mock_coordinator.initialize.assert_awaited_once()


async def test_setup_entry_populates_runtime_data(
    hass: HomeAssistant, mock_coordinator, mock_auto_off_manager, mock_camera_session
) -> None:
    """
    After a successful setup, config_entry.runtime_data must hold coordinator,
    auto_off_manager, and camera_session — this is the runtime-data quality-scale rule.
    """
    entry = MockConfigEntry(domain=DOMAIN, unique_id="12345", data=VALID_DATA)
    entry.add_to_hass(hass)

    with (
        patch("custom_components.alarmdotcom.AlarmCoordinator", return_value=mock_coordinator),
        patch("custom_components.alarmdotcom.AutoOffManager", return_value=mock_auto_off_manager),
        patch(
            "custom_components.alarmdotcom.AlarmCameraSession.from_alarm_bridge",
            return_value=mock_camera_session,
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.runtime_data.coordinator is mock_coordinator
    assert entry.runtime_data.auto_off_manager is mock_auto_off_manager
    assert entry.runtime_data.camera_session is mock_camera_session


async def test_setup_entry_auth_failure_triggers_reauth(
    hass: HomeAssistant, mock_coordinator, mock_auto_off_manager
) -> None:
    """
    If coordinator.initialize() raises an auth error, the entry must end up needing
    reauth (SETUP_ERROR), not a generic retry that gives the user no path forward.
    """
    entry = MockConfigEntry(domain=DOMAIN, unique_id="12345", data=VALID_DATA)
    entry.add_to_hass(hass)

    mock_coordinator.initialize = AsyncMock(side_effect=pyadc.AuthenticationFailed())

    with (
        patch("custom_components.alarmdotcom.AlarmCoordinator", return_value=mock_coordinator),
        patch("custom_components.alarmdotcom.AutoOffManager", return_value=mock_auto_off_manager),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state == ConfigEntryState.SETUP_ERROR


async def test_setup_entry_connection_failure_retries(
    hass: HomeAssistant, mock_coordinator, mock_auto_off_manager
) -> None:
    """
    A transient connection failure must map to ConfigEntryNotReady so HA retries
    automatically — important for Alarm.com being briefly unreachable at HA startup.
    """
    entry = MockConfigEntry(domain=DOMAIN, unique_id="12345", data=VALID_DATA)
    entry.add_to_hass(hass)

    mock_coordinator.initialize = AsyncMock(side_effect=TimeoutError())

    with (
        patch("custom_components.alarmdotcom.AlarmCoordinator", return_value=mock_coordinator),
        patch("custom_components.alarmdotcom.AutoOffManager", return_value=mock_auto_off_manager),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state == ConfigEntryState.SETUP_RETRY


async def test_unload_entry_closes_coordinator_and_camera_session(
    hass: HomeAssistant, mock_coordinator, mock_auto_off_manager, mock_camera_session
) -> None:
    """
    Unloading must close the coordinator (which owns the WS connection and platform
    unloading) and the camera session — leaking either keeps sockets open or blocks
    the next reload.
    """
    entry = MockConfigEntry(domain=DOMAIN, unique_id="12345", data=VALID_DATA)
    entry.add_to_hass(hass)

    with (
        patch("custom_components.alarmdotcom.AlarmCoordinator", return_value=mock_coordinator),
        patch("custom_components.alarmdotcom.AutoOffManager", return_value=mock_auto_off_manager),
        patch(
            "custom_components.alarmdotcom.AlarmCameraSession.from_alarm_bridge",
            return_value=mock_camera_session,
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert result is True
    mock_camera_session.close.assert_awaited_once()
    mock_auto_off_manager.async_unload.assert_awaited_once()
    mock_coordinator.close.assert_awaited_once()
