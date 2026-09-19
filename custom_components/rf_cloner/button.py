"""One button per learned command, plus the bridge's own controls.

ESPHome fixes a node's entity list at compile time, so learned commands cannot be entities there.
They become entities here instead, keyed by each command's immutable id.

Where a button appears follows its RF target: a command assigned to a target is added under that
target's config subentry and lands on that target's device, and an unassigned command is added
under the config entry itself with no device. Moving a command between the two is done by
removing the entity object and adding a fresh one in the new place, because an entity's device
and subentry are fixed when it is added.

That does not disturb the entity registry record. Home Assistant matches the new entity to the
existing record by unique id and updates that record where it stands, so the entity id, the
history behind it and every customisation the user applied survive the move. The removal has to
complete before the replacement is added, though: while both exist the platform sees two entities
claiming one unique id and refuses the second.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .data import RfClonerConfigEntry
from .entity import RfBridgeEntity, RfCommandEntity
from .targets import RfTarget, assigned_target, command_icon

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RfClonerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the bridge's controls and a button per command."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    bridge_id = runtime.bridge_id
    reconciler = runtime.reconciler

    async_add_entities([CancelLearnButton(coordinator, bridge_id)])

    # Where each command's button currently is, as the subentry id it was added under - None for
    # unassigned. A command whose placement still matches is left strictly alone.
    placed: dict[int, str | None] = {}
    entities: dict[int, CommandButton] = {}
    lock = asyncio.Lock()

    async def _async_apply() -> None:
        """Bring the platform's buttons in line with the current organisation."""
        async with lock:
            status = coordinator.data
            wanted = reconciler.async_visible_commands()

            gone = [command_id for command_id in placed if command_id not in wanted]
            moved: list[tuple[int, RfTarget | None, str | None]] = []
            fresh: list[tuple[int, RfTarget | None, str | None]] = []

            for command_id in sorted(wanted):
                target = assigned_target(entry, command_id)
                subentry_id = None if target is None else target.subentry_id
                if command_id not in placed:
                    fresh.append((command_id, target, subentry_id))
                elif placed[command_id] != subentry_id:
                    moved.append((command_id, target, subentry_id))
                else:
                    button = entities.get(command_id)
                    if button is not None:
                        button.async_set_icon(command_icon(entry, command_id))

            # Every removal completes before any replacement is added, so no unique id is ever
            # claimed twice.
            for command_id in gone + [item[0] for item in moved]:
                placed.pop(command_id, None)
                button = entities.pop(command_id, None)
                if button is not None:
                    await button.async_remove()

            for command_id, target, subentry_id in moved + fresh:
                command = status.by_id.get(command_id) if status is not None else None
                if command is None:
                    continue
                button = CommandButton(
                    coordinator,
                    bridge_id,
                    command_id,
                    command.name,
                    target,
                    command_icon(entry, command_id),
                )
                entities[command_id] = button
                placed[command_id] = subentry_id
                async_add_entities([button], config_subentry_id=subentry_id)

    @callback
    def _async_sync() -> None:
        """Schedule a pass. Coalesced by the lock, so a burst of updates settles once."""
        entry.async_create_background_task(
            hass, _async_apply(), name=f"rf_cloner buttons {bridge_id}"
        )

    await _async_apply()
    entry.async_on_unload(reconciler.async_add_listener(_async_sync))
    entry.async_on_unload(coordinator.async_add_listener(_async_sync))


class CommandButton(RfCommandEntity, ButtonEntity):
    """Replays one learned command."""

    @callback
    def async_set_icon(self, icon: str) -> None:
        """Adopt a new integration-provided icon.

        The registry is updated as well as the state, because the platform only records an
        entity's original icon when the entity is added: without this the registry would keep
        showing the previous default until the next reload.

        Only `original_icon` is written. `icon` is where Home Assistant keeps the user's own
        choice, it takes precedence over this one when the state is rendered, and nothing here
        touches it.
        """
        if icon == self._attr_icon:
            return
        self._attr_icon = icon
        if self.hass is None:
            return
        if self.registry_entry is not None:
            er.async_get(self.hass).async_update_entity(
                self.entity_id, original_icon=icon
            )
        self.async_write_ha_state()

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
