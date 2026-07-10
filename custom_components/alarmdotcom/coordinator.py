"""Alarm.com coordinator — manages API connection and distributes updates to entities."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import _pyalarmdotcomajax as pyadc
from _pyalarmdotcomajax import AlarmBridge
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_MFA_TOKEN, DOMAIN, PLATFORMS

log = logging.getLogger(__name__)

# How often to do a full state poll as a safety net against missed websocket events.
POLLING_INTERVAL = timedelta(minutes=5)

WS_RECONNECT_DELAY = 30  # seconds
WS_MAX_RECONNECT_ATTEMPTS = 5
WS_HEARTBEAT_INTERVAL = 60  # seconds


class _AlarmBridgeWithHeartbeat(AlarmBridge):
    """AlarmBridge subclass that injects a WebSocket heartbeat.

    aiohttp will send a PING frame every WS_HEARTBEAT_INTERVAL seconds and
    close the connection if no PONG is received, triggering reconnection.
    This catches silent drops that the library's HTTP-based keep-alive misses.
    """

    @contextlib.asynccontextmanager
    async def ws_connect(self, url, **kwargs):
        if self._websession is None:
            raise pyadc.NotInitialized(
                "Cannot initiate WebSocket connection without an existing session."
            )
        kwargs.setdefault("heartbeat", WS_HEARTBEAT_INTERVAL)
        async with self._websession.ws_connect(url, **kwargs) as res:
            yield res


class AlarmCoordinator(DataUpdateCoordinator[AlarmBridge]):
    """Config-entry coordinator for Alarm.com.

    Inherits DataUpdateCoordinator so that:
    - The 5-minute safety-net poll is scheduled by the coordinator machinery
      rather than a manual async_track_time_interval call.
    - All entities can be CoordinatorEntity subclasses and receive push
      updates via async_set_updated_data() instead of per-device subscriptions.
    """

    config_entry: ConfigEntry

    def __init__(self, hass, config_entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            logger=log,
            name=DOMAIN,
            update_interval=POLLING_INTERVAL,
        )
        self.config_entry = config_entry
        self.api = _AlarmBridgeWithHeartbeat(
            username=config_entry.data[CONF_USERNAME],
            password=config_entry.data[CONF_PASSWORD],
            mfa_token=config_entry.data.get(CONF_MFA_TOKEN),
        )
        self.available: bool = True
        self._reconnect_attempts: int = 0
        self._reconnect_task: asyncio.Task | None = None
        self._options_unsub = None

    # ------------------------------------------------------------------
    # DataUpdateCoordinator interface
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> AlarmBridge:
        """Periodic safety refresh — called by coordinator on update_interval.

        The WebSocket push path is the primary update mechanism; this is
        only a backstop for missed events.

        Uses refresh_all_resources() rather than fetch_full_state() because
        fetch_full_state() routes through each controller's initialize(), which
        no-ops after the very first call and silently returns stale state.
        refresh_all_resources() calls each controller's _refresh() directly —
        the same path the WS reconnect uses — so it always re-fetches live data.
        """
        try:
            log.debug("Alarm.com: performing periodic full state refresh.")
            await self.api.refresh_all_resources()
        except pyadc.AuthenticationException:
            log.warning(
                "Alarm.com: periodic refresh failed — auth error. Will attempt reconnect."
            )
            await self._async_handle_ws_death()
            raise
        except Exception as err:
            log.warning("Alarm.com: periodic refresh failed: %s", err)
        return self.api

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Connect to Alarm.com, fetch initial state, and start WS monitoring."""
        setup_ok = False
        try:
            async with asyncio.timeout(10):
                await self.api.initialize()
            setup_ok = True
        except (
            TimeoutError,
            pyadc.UnexpectedResponse,
            pyadc.ServiceUnavailable,
        ) as err:
            raise ConfigEntryNotReady("Could not connect to Alarm.com.") from err
        except pyadc.AuthenticationException as err:
            raise ConfigEntryAuthFailed from err
        except Exception:
            log.exception("Unexpected error during Alarm.com initialization.")
            return
        finally:
            if not setup_ok:
                await self.api.close()

        await self.api.start_event_monitoring(self._ws_event_handler)

        # Set coordinator data so entities can read initial state during platform setup,
        # before any WS events have arrived.
        self.async_set_updated_data(self.api)

        device_registry = dr.async_get(self.hass)
        device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={(DOMAIN, str(self.api.active_system.id))},
            manufacturer="Alarm.com",
            name=self.api.active_system.name,
            entry_type=dr.DeviceEntryType.SERVICE,
            model="Security System",
        )

        self._options_unsub = self.config_entry.add_update_listener(_options_update_listener)
        self._reconnect_attempts = 0

    async def close(self) -> bool:
        """Shut down the coordinator and unload all platforms."""
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._options_unsub is not None:
            self._options_unsub()
            self._options_unsub = None

        await self.api.close()

        return await self.hass.config_entries.async_unload_platforms(
            self.config_entry,
            PLATFORMS,
        )

    # ------------------------------------------------------------------
    # WebSocket event handling
    # ------------------------------------------------------------------

    async def _ws_event_handler(self, message: pyadc.EventBrokerMessage) -> None:
        """Handle WebSocket events and push updates to all CoordinatorEntity listeners."""
        if isinstance(message, pyadc.ConnectionEvent):
            if message.current_state == pyadc.WebSocketState.DEAD:
                log.warning(
                    "Alarm.com websocket died. Will attempt reconnect in %s seconds"
                    " (attempt %d/%d).",
                    WS_RECONNECT_DELAY,
                    self._reconnect_attempts + 1,
                    WS_MAX_RECONNECT_ATTEMPTS,
                )
                self.available = False
                await self._async_handle_ws_death()
            elif message.current_state == pyadc.WebSocketState.CONNECTED:
                if not self.available:
                    log.info("Alarm.com websocket reconnected.")
                self.available = True
                self._reconnect_attempts = 0
            elif message.current_state not in (
                pyadc.WebSocketState.CONNECTED,
                pyadc.WebSocketState.CONNECTING,
            ):
                log.info("Alarm.com websocket state: %s", message.current_state)
            log.debug("Alarm.com websocket state: %s", message.current_state)

        # Notify all CoordinatorEntity listeners so entities update their state.
        self.async_set_updated_data(self.api)

    async def _async_handle_ws_death(self) -> None:
        """Schedule a reconnect attempt. No-op if one is already in progress."""
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = self.hass.async_create_task(
            self._async_reconnect_with_backoff()
        )

    async def _async_reconnect_with_backoff(self) -> None:
        """Attempt to reconnect with exponential backoff, then reload if exhausted."""
        while self._reconnect_attempts < WS_MAX_RECONNECT_ATTEMPTS:
            self._reconnect_attempts += 1
            delay = WS_RECONNECT_DELAY * self._reconnect_attempts
            log.info(
                "Alarm.com: reconnect attempt %d/%d in %d seconds...",
                self._reconnect_attempts,
                WS_MAX_RECONNECT_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)

            try:
                await self.api.close()
                async with asyncio.timeout(15):
                    await self.api.initialize()
                await self.api.start_event_monitoring(self._ws_event_handler)
                self.available = True
                self._reconnect_attempts = 0
                log.info("Alarm.com: reconnect successful.")
                return
            except pyadc.AuthenticationException:
                log.error(
                    "Alarm.com: reconnect failed — authentication error. Triggering reauth."
                )
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self.config_entry.entry_id)
                )
                return
            except Exception as err:
                log.warning(
                    "Alarm.com: reconnect attempt %d failed: %s",
                    self._reconnect_attempts,
                    err,
                )

        log.error(
            "Alarm.com: all %d reconnect attempts failed. Scheduling integration reload.",
            WS_MAX_RECONNECT_ATTEMPTS,
        )
        # async_schedule_reload is a sync @callback that schedules its own task
        # internally — do NOT wrap it in async_create_task (that would pass None
        # where a coroutine is expected and raise TypeError after the reload fires).
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)


async def _options_update_listener(hass, entry: ConfigEntry) -> None:
    """Reload the integration when config options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# Backward-compatibility alias so any existing import of AlarmHub still resolves.
AlarmHub = AlarmCoordinator
