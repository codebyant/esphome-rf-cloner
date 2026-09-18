"""The RF Cloner Bridge integration.

Presents an esphome-rf-cloner bridge as a Home Assistant device whose learned RF commands appear
as ordinary entities, and keeps a restorable replica of its registry.

The device owns the registry. Home Assistant reads it, mirrors it, and mutates it only when the
user asks for a mutation.
"""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_ACTION_PREFIX,
    CONF_ESPHOME_ENTRY_ID,
    DEFAULT_ACTION_PREFIX,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .coordinator import RfBridgeCoordinator
from .data import RfClonerConfigEntry, RfClonerRuntimeData
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

    _async_register_bridge_device(hass, entry, esphome_entry_id, bridge_id)

    snapshots = SnapshotManager(hass, bridge_id, transport)
    await snapshots.async_load()

    reconciler = CommandReconciler(hass, entry, coordinator)
    reconciler.async_snapshot_mirror()
    reconciler.async_reconcile(status)

    entry.runtime_data = RfClonerRuntimeData(
        bridge_id=bridge_id,
        transport=transport,
        coordinator=coordinator,
        reconciler=reconciler,
        snapshots=snapshots,
        bound_to=(esphome_entry_id, action_prefix),
    )

    @callback
    def _async_handle_status() -> None:
        """Mirror every successful poll into the subentries and the replica."""
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

    # Only now can a subentry disappearing mean the user deleted a command.
    reconciler.async_activate()
    # The first poll landed before the listener existed.
    _async_handle_status()
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: RfClonerConfigEntry) -> None:
    """React to a change of the config entry.

    Two kinds arrive here: a reconfigure that re-points the entry at a different node, which needs
    a reload, and a subentry the user removed, which is a request to delete that command.
    """
    runtime = entry.runtime_data
    bound_to = (
        entry.data[CONF_ESPHOME_ENTRY_ID],
        entry.data.get(CONF_ACTION_PREFIX, DEFAULT_ACTION_PREFIX),
    )
    if bound_to != runtime.bound_to:
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    await runtime.reconciler.async_handle_entry_update()


async def async_unload_entry(hass: HomeAssistant, entry: RfClonerConfigEntry) -> bool:
    """Tear one bridge down."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: RfClonerConfigEntry) -> None:
    """Discard the replica along with the entry that owned it."""
    if entry.unique_id is not None:
        await async_remove_snapshot(hass, entry.unique_id)


@callback
def _async_register_bridge_device(
    hass: HomeAssistant,
    entry: RfClonerConfigEntry,
    esphome_entry_id: str,
    bridge_id: str,
) -> None:
    """Create this bridge's own device, hung off the ESPHome node that carries it."""
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, bridge_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model=MODEL,
        serial_number=bridge_id,
    )
    esphome_device_id = _async_find_esphome_device(hass, esphome_entry_id)
    if esphome_device_id is not None and device.via_device_id != esphome_device_id:
        registry.async_update_device(device.id, via_device_id=esphome_device_id)


@callback
def _async_find_esphome_device(hass: HomeAssistant, esphome_entry_id: str) -> str | None:
    """Locate the node's main device so the bridge can be linked to it.

    ESPHome registers its main device by network MAC and gives it no identifiers, so it is found
    through the connection its config entry's unique id names. The lookup is scoped to that config
    entry, because a connection is only unique within one.
    """
    entry = hass.config_entries.async_get_entry(esphome_entry_id)
    if entry is None or entry.unique_id is None:
        return None
    device = dr.async_get(hass).async_get_device_by_connection(
        (dr.CONNECTION_NETWORK_MAC, dr.format_mac(entry.unique_id)), esphome_entry_id
    )
    return device.id if device is not None else None
