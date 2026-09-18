"""Polling of the bridge's canonical status.

rf_status is cheap by design - it carries no waveforms - so polling it is the whole
reconciliation input. Operations this integration initiates refresh immediately afterwards, so
the poll interval only bounds how long a change made elsewhere (the device's own web UI, a script,
a physical relearn) stays invisible.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LEARN_POLL_INTERVAL, LEARN_TIMEOUT, UPDATE_INTERVAL
from .models import BridgeStatus
from .transport import BridgeTransport

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LearnOutcome:
    """How a learn attempt ended, as observed through rf_status."""

    succeeded: bool
    command_id: int | None
    name: str
    # The device's own last_result string, e.g. "timeout" or "no_repeat_agreement".
    detail: str


class RfBridgeCoordinator(DataUpdateCoordinator[BridgeStatus]):
    """Keeps the last known registry state for one bridge."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        transport: BridgeTransport,
    ) -> None:
        """Set up polling against `transport`."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} {config_entry.title}",
            update_interval=UPDATE_INTERVAL,
        )
        self.transport = transport
        # Serialises the mutations this integration initiates. The device applies API actions one
        # at a time anyway; this keeps two Home Assistant flows from interleaving a learn with a
        # rename and then each reading the other's status.
        self._lock = asyncio.Lock()

    async def _async_update_data(self) -> BridgeStatus:
        """Read rf_status once."""
        try:
            return await self.transport.async_status()
        except HomeAssistantError as err:
            raise UpdateFailed(str(err)) from err

    async def async_learn(self, name: str) -> LearnOutcome:
        """Arm a capture and wait for the device to report how it ended.

        Polls rf_status rather than subscribing to anything: the learn window is short, and this
        keeps the integration on the one read it already depends on.
        """
        async with self._lock:
            await self.transport.async_learn(name)
            outcome = await self._async_await_learn(name)
        await self.async_request_refresh()
        return outcome

    async def _async_await_learn(self, name: str) -> LearnOutcome:
        """Poll until the device leaves the armed state, or we outlast its own timeout."""
        deadline = time.monotonic() + LEARN_TIMEOUT
        status = await self.transport.async_status()
        while status.state == "armed" and time.monotonic() < deadline:
            await asyncio.sleep(LEARN_POLL_INTERVAL)
            status = await self.transport.async_status()

        if status.state == "armed":
            # We gave up before the device did; leave nothing armed behind.
            await self.transport.async_cancel()
            return LearnOutcome(False, None, name, "timeout")

        command = status.by_name.get(name)
        if status.state == "captured" and command is not None:
            return LearnOutcome(True, command.command_id, name, status.last_result)
        return LearnOutcome(False, None, name, status.last_result or status.state)

    async def async_rename(self, command_id: int, name: str) -> None:
        """Rename a command and pick the change up immediately."""
        async with self._lock:
            await self.transport.async_rename(command_id, name)
        await self.async_request_refresh()

    async def async_delete(self, command_id: int, name: str) -> None:
        """Delete a command and pick the change up immediately."""
        async with self._lock:
            await self.transport.async_delete(command_id, name)
        await self.async_request_refresh()

    async def async_send(self, command_id: int) -> None:
        """Replay a command. Does not change the registry, so no refresh is needed."""
        async with self._lock:
            await self.transport.async_send(command_id)

    async def async_cancel(self) -> None:
        """Disarm a capture in progress."""
        async with self._lock:
            await self.transport.async_cancel()
        await self.async_request_refresh()
