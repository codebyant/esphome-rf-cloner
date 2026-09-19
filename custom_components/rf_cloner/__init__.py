"""The RF Cloner Bridge integration.

Presents an esphome-rf-cloner bridge as a Home Assistant device whose learned RF commands appear
as ordinary entities, grouped under the equipment they actually drive, and keeps a restorable
replica of its registry.

The device owns the registry. Home Assistant reads it, mirrors it, and mutates it only when the
user asks for a mutation. How those commands are organised - which RF target each belongs to, and
what icon it carries - is Home Assistant's alone, and no part of it ever reaches the device.
"""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError

from .const import (
    CONF_ACTION_PREFIX,
    CONF_ESPHOME_ENTRY_ID,
    DEFAULT_ACTION_PREFIX,
    DOMAIN,
)
from .coordinator import RfBridgeCoordinator
from .data import RfClonerConfigEntry, RfClonerRuntimeData
from .devices import async_register_bridge_device, async_sync_target_devices
from .migration import async_migrate_entry  # noqa: F401  (Home Assistant looks it up here)
from .reconcile import CommandReconciler
from .services import async_setup_services
from .snapshot import SnapshotManager, async_remove_snapshot
from .transport import BridgeTransport

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the integration's own actions once."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RfClonerConfigEntry) -> bool:
    """Set up one bridge."""
    esphome_entry_id: str = entry.data[CONF_ESPHOME_ENTRY_ID]
    action_prefix: str = entry.data.get(CONF_ACTION_PREFIX, DEFAULT_ACTION_PREFIX)
    if hass.config_entries.async_get_entry(esphome_entry_id) is None:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="esphome_entry_missing",
        )

    transport = BridgeTransport(hass, esphome_entry_id, action_prefix)
    coordinator = RfBridgeCoordinator(hass, entry, transport)
    await coordinator.async_config_entry_first_refresh()

    status = coordinator.data
    if entry.unique_id is None:
        hass.config_entries.async_update_entry(entry, unique_id=status.bridge_id)
    # From here on the entry's unique id is the identity, so a bridge reporting someone else's -
    # replacement or factory-reset hardware - cannot drag the devices and entities with it.
    bridge_id = entry.unique_id or status.bridge_id

    bridge_device = async_register_bridge_device(
        hass, entry, esphome_entry_id, bridge_id
    )
    # Before the platforms, so a target's device exists when its buttons are added to it - and so
    # a target with nothing learned into it yet is still visible.
    async_sync_target_devices(hass, entry, bridge_id, bridge_device.id)

    snapshots = SnapshotManager(hass, bridge_id, transport)
    await snapshots.async_load()

    reconciler = CommandReconciler(hass, entry, coordinator)
    reconciler.async_snapshot_targets()
    reconciler.async_reconcile(status)

    entry.runtime_data = RfClonerRuntimeData(
        bridge_id=bridge_id,
        bridge_device_id=bridge_device.id,
        transport=transport,
        coordinator=coordinator,
        reconciler=reconciler,
        snapshots=snapshots,
        bound_to=(esphome_entry_id, action_prefix),
    )

    @callback
    def _async_handle_status() -> None:
        """Mirror every successful poll into the organisation and the replica."""
        current = coordinator.data
        if current is None:
            return
        reconciler.async_reconcile(current)
        # The snapshot manager refuses a status from a bridge that is not this one, so this does
        # not need to re-check the identity.
        entry.async_create_background_task(
            hass,
            snapshots.async_refresh(current),
            name=f"{DOMAIN} snapshot {bridge_id}",
        )

    entry.async_on_unload(coordinator.async_add_listener(_async_handle_status))
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    # Unload hooks run last-registered first, so registering this after the listeners makes it the
    # first thing to run on teardown: the reconciler stops attributing removals to the user before
    # any of the rest is taken apart.
    entry.async_on_unload(reconciler.async_deactivate)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Only now can a subentry disappearing mean the user removed a target.
    reconciler.async_activate()
    # The first poll landed before the listener existed.
    _async_handle_status()
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: RfClonerConfigEntry) -> None:
    """React to a change of the config entry.

    Three kinds arrive here: a reconfigure that re-points the entry at a different node, which
    needs a reload; a target subentry the user added, renamed or removed; and a change to the
    command organisation this integration wrote itself.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    bound_to = (
        entry.data[CONF_ESPHOME_ENTRY_ID],
        entry.data.get(CONF_ACTION_PREFIX, DEFAULT_ACTION_PREFIX),
    )
    if bound_to != runtime.bound_to:
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    # Picks up targets added or renamed in the UI. Removal is the reconciler's business, and a
    # removed target has no device left to sync.
    async_sync_target_devices(hass, entry, runtime.bridge_id, runtime.bridge_device_id)
    await runtime.reconciler.async_handle_entry_update()


async def async_unload_entry(hass: HomeAssistant, entry: RfClonerConfigEntry) -> bool:
    """Tear one bridge down."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: RfClonerConfigEntry) -> None:
    """Discard the replica along with the entry that owned it."""
    if entry.unique_id is not None:
        await async_remove_snapshot(hass, entry.unique_id)
