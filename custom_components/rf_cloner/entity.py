"""Shared entity plumbing.

The bridge is one device. Learned commands are entities of the config entry, each owned by its
own subentry but belonging to no device: a command is a stored waveform, not a piece of hardware,
and an entity can carry its own area assignment without one.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RfBridgeCoordinator
from .models import CommandInfo
from .reconcile import command_unique_id


class RfBridgeEntity(CoordinatorEntity[RfBridgeCoordinator]):
    """An entity describing the bridge itself."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: RfBridgeCoordinator, bridge_id: str, key: str
    ) -> None:
        """Attach to the bridge's own device."""
        super().__init__(coordinator)
        self._bridge_id = bridge_id
        self._attr_unique_id = f"{bridge_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, bridge_id)})

    @property
    def available(self) -> bool:
        """Available while the last poll succeeded."""
        return super().available and self.coordinator.transport.available


class RfCommandEntity(CoordinatorEntity[RfBridgeCoordinator]):
    """An entity describing one learned command.

    Keyed by the command's immutable id, so a rename moves the label and leaves the entity, its
    history and every automation referring to it alone.
    """

    # There is no device to take a name from, so the entity's own name is its full name.
    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: RfBridgeCoordinator,
        bridge_id: str,
        command_id: int,
        name: str,
    ) -> None:
        """Identify the command this entity replays."""
        super().__init__(coordinator)
        self._bridge_id = bridge_id
        self._command_id = command_id
        self._name = name
        self._attr_unique_id = command_unique_id(bridge_id, command_id)

    @property
    def command(self) -> CommandInfo | None:
        """This command as the device last reported it, or None once it is gone."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.by_id.get(self._command_id)

    @property
    def name(self) -> str:
        """The command's current name on the device.

        Read live rather than fixed at creation, so a rename made anywhere - this integration, a
        script, the bridge's own web page - is reflected here. Home Assistant writes the value
        back to the entity registry itself, and a name the user overrode locally still wins.
        """
        command = self.command
        if command is not None:
            self._name = command.name
        return self._name

    @property
    def available(self) -> bool:
        """Available while the device still reports this command."""
        return (
            super().available
            and self.coordinator.transport.available
            and self.command is not None
        )
