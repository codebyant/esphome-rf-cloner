"""Transport to the bridge, over Home Assistant's own ESPHome action layer.

The integration never opens its own connection to the node. Every call goes through the actions
the ESPHome config entry registers, which keeps a single client, a single reconnect policy and a
single source of truth about whether the node is reachable.

Action names are recomputed from the ESPHome config entry on every call rather than stored,
because ESPHome derives them from the node name and rewrites `device_name` in the entry whenever
the node reports a different one.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from .const import (
    ESPHOME_CONF_DEVICE_NAME,
    ESPHOME_DOMAIN,
    REVISION_MAX,
)
from .models import BridgeStatus, ExportedCommand

_LOGGER = logging.getLogger(__name__)


class BridgeUnavailable(HomeAssistantError):
    """The bridge's actions are not currently callable.

    Raised when the ESPHome config entry is gone or unloaded, or when the node is disconnected and
    ESPHome has therefore withdrawn its actions.
    """


class BridgeTransport:
    """Typed wrapper around one bridge's ESPHome actions."""

    def __init__(
        self, hass: HomeAssistant, esphome_entry_id: str, action_prefix: str
    ) -> None:
        """Bind to an existing ESPHome config entry."""
        self._hass = hass
        self._esphome_entry_id = esphome_entry_id
        self._action_prefix = action_prefix

    @property
    def esphome_entry_id(self) -> str:
        """The ESPHome config entry this transport speaks through."""
        return self._esphome_entry_id

    @property
    def node_name(self) -> str | None:
        """The node's current ESPHome name, or None when the entry is gone or not yet connected."""
        entry = self._hass.config_entries.async_get_entry(self._esphome_entry_id)
        if entry is None:
            return None
        name = entry.data.get(ESPHOME_CONF_DEVICE_NAME)
        return str(name) if name else None

    def service_name(self, action: str) -> str | None:
        """The Home Assistant service implementing `action`, or None when it cannot be resolved."""
        node = self.node_name
        if node is None:
            return None
        return f"{node.replace('-', '_')}_{self._action_prefix}{action}"

    def supports(self, action: str) -> bool:
        """Whether the node currently exposes `action`.

        False also means "not connected": ESPHome registers a node's actions on connect and
        removes them on disconnect.
        """
        service = self.service_name(action)
        return service is not None and self._hass.services.has_service(
            ESPHOME_DOMAIN, service
        )

    @property
    def available(self) -> bool:
        """Whether the bridge can be read right now."""
        return self.supports("status")

    async def _call(
        self,
        action: str,
        data: dict[str, Any] | None = None,
        *,
        response: bool = False,
    ) -> dict[str, Any] | None:
        """Invoke one action, translating an absent action into BridgeUnavailable."""
        service = self.service_name(action)
        if service is None:
            raise BridgeUnavailable(
                f"ESPHome config entry {self._esphome_entry_id} is unknown or has no device name"
            )
        try:
            result = await self._hass.services.async_call(
                ESPHOME_DOMAIN,
                service,
                data or {},
                blocking=True,
                return_response=response,
            )
        except ServiceNotFound as err:
            raise BridgeUnavailable(
                f"Action {ESPHOME_DOMAIN}.{service} is not registered; the node is most likely"
                " disconnected"
            ) from err
        if not response:
            return None
        if not isinstance(result, dict):
            raise HomeAssistantError(
                f"Action {ESPHOME_DOMAIN}.{service} returned {type(result).__name__},"
                " expected a mapping"
            )
        return result

    # Reads

    async def async_status(self) -> BridgeStatus:
        """Read the canonical registry view."""
        payload = await self._call("status", response=True)
        assert payload is not None
        return BridgeStatus.from_payload(payload)

    async def async_export(self, command_id: int) -> ExportedCommand | None:
        """Read one command's portable representation, or None when the id is unknown."""
        payload = await self._call("export", {"command_id": command_id}, response=True)
        assert payload is not None
        return ExportedCommand.from_payload(payload)

    # Mutations

    async def async_learn(self, name: str) -> None:
        """Arm a capture under `name`. Returns as soon as the device is armed."""
        await self._call("learn", {"name": name})

    async def async_cancel(self) -> None:
        """Disarm a capture in progress."""
        await self._call("cancel")

    async def async_rename(self, command_id: int, name: str) -> None:
        """Rename a command, keeping its id and waveform."""
        await self._call("rename", {"command_id": command_id, "name": name})

    async def async_delete(self, command_id: int, name: str) -> None:
        """Delete a command.

        Prefers deleting by immutable id. Firmware that predates that action is addressed by name
        instead, which is what the caller's last status read saw the command called.
        """
        if self.supports("delete_id"):
            await self._call("delete_id", {"command_id": command_id})
            return
        await self._call("delete", {"name": name})

    async def async_send(self, command_id: int) -> None:
        """Replay a command by its immutable id."""
        await self._call("send_id", {"command_id": command_id})

    async def async_clear(self) -> None:
        """Drop every command, keeping the bridge identity and the id counter."""
        await self._call("clear")

    # Restore

    async def async_restore_begin(self, bridge_id: str, next_command_id: int) -> None:
        """Adopt an external identity and open a restore."""
        await self._call(
            "restore_begin",
            {"bridge_id": bridge_id, "next_command_id": next_command_id},
        )

    async def async_import(self, command: ExportedCommand) -> None:
        """Write one exported command back under the id it held."""
        await self._call(
            "import",
            {
                "command_id": command.command_id,
                "name": command.name,
                "timings": list(command.timings),
                "gap_us": command.gap_us,
                "repeat_times": command.repeat_times,
                "frequency_hz": command.frequency_hz,
                "modulation": command.modulation,
            },
        )

    async def async_restore_commit(self, source_revision: int) -> None:
        """Close the restore, continuing the source registry's revision sequence.

        The device does not keep the source revision on flash, so the restorer supplies it. Without
        it a restored bridge would restart its revision numbering and could republish a revision
        this integration had already seen, which would make revision useless as a change token.

        The value is checked against the shared revision domain first. A negative or oversized
        number would otherwise be narrowed on the way in and land the device's revision near its
        ceiling.
        """
        if not 0 <= source_revision <= REVISION_MAX:
            raise HomeAssistantError(
                f"source_revision {source_revision} is outside the bridge's revision domain"
                f" (0 to {REVISION_MAX})"
            )
        await self._call("restore_commit", {"source_revision": source_revision})
