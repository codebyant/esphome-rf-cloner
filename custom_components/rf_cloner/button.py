"""One button per learned command, plus the bridge's own controls.

ESPHome fixes a node's entity list at compile time, so learned commands cannot be entities there.
They become entities here instead, created from the command subentries the reconciler maintains
and keyed by each command's immutable id.

A command button belongs to its subentry, which is what gives it a native add and remove
lifecycle, but to no device: the bridge is the only device this integration owns.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .data import RfClonerConfigEntry
from .entity import RfBridgeEntity, RfCommandEntity
from .reconcile import command_subentries


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RfClonerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the bridge's controls and a button per command."""
    coordinator = entry.runtime_data.coordinator
    bridge_id = entry.runtime_data.bridge_id

    async_add_entities([CancelLearnButton(coordinator, bridge_id)])

    added: set[int] = set()

    @callback
    def _async_sync() -> None:
        """Give every command subentry a button, once.

        Driven by the subentries rather than by the status directly, because an entity can only
        join a subentry that already exists.
        """
        current = command_subentries(entry)
        # A command that was removed and came back should get a button again.
        added.intersection_update(current)
        for command_id, subentry in current.items():
            if command_id in added:
                continue
            added.add(command_id)
            async_add_entities(
                [CommandButton(coordinator, bridge_id, command_id, subentry.title)],
                config_subentry_id=subentry.subentry_id,
            )

    async def _async_entry_changed(
        _hass: HomeAssistant, _entry: RfClonerConfigEntry
    ) -> None:
        """Pick up subentries added since the last look."""
        _async_sync()

    _async_sync()
    entry.async_on_unload(coordinator.async_add_listener(_async_sync))
    entry.async_on_unload(entry.add_update_listener(_async_entry_changed))


class CommandButton(RfCommandEntity, ButtonEntity):
    """Replays one learned command."""

    _attr_icon = "mdi:remote"

    async def async_press(self) -> None:
        """Replay the command by its immutable id."""
        await self.coordinator.async_send(self._command_id)


class CancelLearnButton(RfBridgeEntity, ButtonEntity):
    """Disarms a capture that is waiting for a remote that will not be pressed."""

    _attr_icon = "mdi:cancel"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, bridge_id: str) -> None:
        """Describe the control."""
        super().__init__(coordinator, bridge_id, "cancel_learn")

    async def async_press(self) -> None:
        """Disarm the capture."""
        await self.coordinator.async_cancel()
