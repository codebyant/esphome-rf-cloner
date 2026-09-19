"""The Home Assistant devices this integration owns: the bridge, and one per RF target.

The bridge is the hub, hung off the ESPHome node that carries it. Each target is a separate
device hung off the bridge, because a target is a distinct piece of equipment the bridge happens
to reach - which is what a via-device link means.

A target device is created under its own config subentry, because Home Assistant gives a device
exactly one config entry and at most one subentry, and one target is one subentry. That is also
why a target device is created once and never moved between subentries: moving a device that way
makes Home Assistant remove the entities left behind in the old one.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, MANUFACTURER, MODEL
from .targets import RfTarget, target_model, targets

_LOGGER = logging.getLogger(__name__)


@callback
def async_register_bridge_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    esphome_entry_id: str,
    bridge_id: str,
) -> dr.DeviceEntry:
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
        device = registry.async_update_device(
            device.id, via_device_id=esphome_device_id
        )
        assert device is not None
    return device


@callback
def async_sync_target_devices(
    hass: HomeAssistant, entry: ConfigEntry, bridge_id: str, bridge_device_id: str
) -> None:
    """Give every defined target a device, and keep its presentation in step.

    Created here rather than left to the entity platform so that a target with no commands yet is
    still a device the user can see, name and place. A target is not a mistake just because
    nothing has been learned into it.
    """
    registry = dr.async_get(hass)
    for target in targets(entry).values():
        async_register_target_device(
            hass, entry, bridge_id, bridge_device_id, target, registry=registry
        )


@callback
def async_register_target_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    bridge_id: str,
    bridge_device_id: str,
    target: RfTarget,
    *,
    registry: dr.DeviceRegistry | None = None,
) -> dr.DeviceEntry:
    """Create or update one target's device.

    The name and model follow the target, so renaming a target or changing its type is reflected
    here. The area is applied only when the device is first created: after that the area is the
    user's, and a later move of theirs has to survive every reload.
    """
    registry = registry or dr.async_get(hass)
    identifier = (DOMAIN, target.device_identifier(bridge_id))
    is_new = (
        registry.async_get_device_by_identifier(identifier, entry.entry_id) is None
    )

    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=target.subentry_id,
        identifiers={identifier},
        name=target.name,
        manufacturer=MANUFACTURER,
        model=target_model(target.target_type),
        via_device_id=bridge_device_id,
    )
    if is_new and target.area_id is not None and device.area_id is None:
        updated = registry.async_update_device(device.id, area_id=target.area_id)
        if updated is not None:
            device = updated
    return device


@callback
def async_target_device_id(
    hass: HomeAssistant, entry: ConfigEntry, bridge_id: str, target: RfTarget
) -> str | None:
    """The device id of one target, or None when it has not been registered yet."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, target.device_identifier(bridge_id)), entry.entry_id
    )
    return None if device is None else device.id


@callback
def async_target_device_area(
    hass: HomeAssistant, entry: ConfigEntry, bridge_id: str, target: RfTarget
) -> str | None:
    """The area a target's device is actually in right now.

    Not the same as the area stored on the subentry: the user may have moved the device since,
    and that move is theirs. This is what the reconfigure form shows, so the form always reflects
    where the device really is rather than where it was first put.
    """
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, target.device_identifier(bridge_id)), entry.entry_id
    )
    return None if device is None else device.area_id


@callback
def async_set_target_area(
    hass: HomeAssistant,
    entry: ConfigEntry,
    bridge_id: str,
    target: RfTarget,
    area_id: str | None,
) -> None:
    """Move a target's device to `area_id` because the user asked for it explicitly.

    Separate from the registration above, and called only from the flow where the user changed
    the area field, because the two cases are not the same gesture. Re-registering a target on
    every reload must never move its device - the area is the user's once the device exists - but
    a user who opens the form and changes the area is asking for exactly that move.
    """
    device_id = async_target_device_id(hass, entry, bridge_id, target)
    if device_id is None:
        return
    dr.async_get(hass).async_update_device(device_id, area_id=area_id)


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
