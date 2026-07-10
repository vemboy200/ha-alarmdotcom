"""
Tests for alarmdotcom service handlers: bypass_sensor and unbypass_sensor.

These are the highest-value custom actions this integration exposes — if
bypass/unbypass silently no-ops or raises the wrong exception type, the
user has no way to know without a lot of debugging. Covers the happy path
(valid bypassable sensor, auto-resolved partition) and both flavors of
invalid input (unknown sensor ID, sensor that doesn't support bypass).

Contributes toward the Silver quality-scale "test-coverage" requirement.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alarmdotcom.const import (
    ATTR_PARTITION_ID,
    ATTR_RESOURCE_ID,
    DOMAIN,
    SERVICE_BYPASS_SENSOR,
    SERVICE_UNBYPASS_SENSOR,
)

VALID_DATA = {"username": "test@example.com", "password": "hunter2"}
SENSOR_ID = "sensor-42"
PARTITION_ID = "partition-1"
SYSTEM_ID = "system-1"


@pytest.fixture
def bypassable_sensor():
    """A sensor that exists and supports bypass."""
    sensor = MagicMock()
    sensor.id = SENSOR_ID
    sensor.system_id = SYSTEM_ID
    sensor.attributes.supports_bypass = True
    sensor.attributes.supports_immediate_bypass = False
    return sensor


@pytest.fixture
def matching_partition():
    """A partition on the same system as the sensor."""
    partition = MagicMock()
    partition.id = PARTITION_ID
    partition.system_id = SYSTEM_ID
    return partition


@pytest.fixture
def mock_coordinator(bypassable_sensor, matching_partition):
    """AlarmCoordinator mock wired up for bypass service tests."""
    coordinator = MagicMock()
    coordinator.initialize = AsyncMock(return_value=None)
    coordinator.close = AsyncMock(return_value=True)

    coordinator.api = MagicMock()
    coordinator.api.sensors.get.return_value = bypassable_sensor
    coordinator.api.partitions.values.return_value = [matching_partition]
    coordinator.api.partitions.change_sensor_bypass = AsyncMock(return_value=None)
    return coordinator


@pytest.fixture
def mock_auto_off_manager():
    """AutoOffManager mock that skips file I/O."""
    manager = MagicMock()
    manager.async_load = AsyncMock(return_value=None)
    manager.async_unload = AsyncMock(return_value=None)
    return manager


async def _setup_entry(hass, mock_coordinator, mock_auto_off_manager):
    """Set up a single alarmdotcom config entry with all external I/O patched out."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="12345", data=VALID_DATA)
    entry.add_to_hass(hass)

    with (
        patch("custom_components.alarmdotcom.AlarmCoordinator", return_value=mock_coordinator),
        patch("custom_components.alarmdotcom.AutoOffManager", return_value=mock_auto_off_manager),
        patch(
            "custom_components.alarmdotcom.AlarmCameraSession.from_alarm_bridge",
            return_value=None,
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            return_value=True,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


async def test_bypass_sensor_calls_api(
    hass: HomeAssistant, mock_coordinator, mock_auto_off_manager
) -> None:
    """
    bypass_sensor on a valid, bypassable sensor calls the Alarm.com partition API
    with the sensor in bypass_ids and the auto-resolved partition ID.
    """
    await _setup_entry(hass, mock_coordinator, mock_auto_off_manager)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BYPASS_SENSOR,
        {ATTR_RESOURCE_ID: SENSOR_ID},
        blocking=True,
    )

    mock_coordinator.api.partitions.change_sensor_bypass.assert_awaited_once_with(
        PARTITION_ID,
        bypass_ids=[SENSOR_ID],
        unbypass_ids=None,
    )


async def test_unbypass_sensor_calls_api(
    hass: HomeAssistant, mock_coordinator, mock_auto_off_manager
) -> None:
    """
    unbypass_sensor on a valid, bypassable sensor calls the API with the sensor in
    unbypass_ids — not bypass_ids.
    """
    await _setup_entry(hass, mock_coordinator, mock_auto_off_manager)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_UNBYPASS_SENSOR,
        {ATTR_RESOURCE_ID: SENSOR_ID},
        blocking=True,
    )

    mock_coordinator.api.partitions.change_sensor_bypass.assert_awaited_once_with(
        PARTITION_ID,
        bypass_ids=None,
        unbypass_ids=[SENSOR_ID],
    )


async def test_bypass_sensor_explicit_partition(
    hass: HomeAssistant, mock_coordinator, mock_auto_off_manager
) -> None:
    """
    When partition_id is given explicitly, it should be passed through without
    any partition-lookup logic.
    """
    await _setup_entry(hass, mock_coordinator, mock_auto_off_manager)

    explicit_partition = "explicit-partition-99"
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BYPASS_SENSOR,
        {ATTR_RESOURCE_ID: SENSOR_ID, ATTR_PARTITION_ID: explicit_partition},
        blocking=True,
    )

    mock_coordinator.api.partitions.change_sensor_bypass.assert_awaited_once_with(
        explicit_partition,
        bypass_ids=[SENSOR_ID],
        unbypass_ids=None,
    )


async def test_bypass_sensor_unknown_id_raises(
    hass: HomeAssistant, mock_coordinator, mock_auto_off_manager
) -> None:
    """
    bypass_sensor with an unknown resource ID must raise ServiceValidationError, not
    silently succeed — the caller needs actionable feedback.
    """
    mock_coordinator.api.sensors.get.return_value = None

    await _setup_entry(hass, mock_coordinator, mock_auto_off_manager)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BYPASS_SENSOR,
            {ATTR_RESOURCE_ID: "nonexistent-sensor"},
            blocking=True,
        )

    mock_coordinator.api.partitions.change_sensor_bypass.assert_not_awaited()


async def test_bypass_sensor_unsupported_raises(
    hass: HomeAssistant, mock_coordinator, mock_auto_off_manager, bypassable_sensor
) -> None:
    """
    bypass_sensor on a sensor that doesn't support bypass must raise
    ServiceValidationError, not silently call the API and get a confusing remote error.
    """
    bypassable_sensor.attributes.supports_bypass = False
    bypassable_sensor.attributes.supports_immediate_bypass = False

    await _setup_entry(hass, mock_coordinator, mock_auto_off_manager)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BYPASS_SENSOR,
            {ATTR_RESOURCE_ID: SENSOR_ID},
            blocking=True,
        )

    mock_coordinator.api.partitions.change_sensor_bypass.assert_not_awaited()
