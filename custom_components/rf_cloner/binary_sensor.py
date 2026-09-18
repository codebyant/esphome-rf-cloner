"""The two states that stop a bridge accepting changes.

Both are conditions the user has to resolve deliberately, so they are surfaced as problems rather
than left to be discovered when a learn is refused.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .data import RfClonerConfigEntry
from .entity import RfBridgeEntity
from .models import BridgeStatus


@dataclass(frozen=True, kw_only=True)
class RfBridgeBinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor reading one field of the bridge's status."""

    value: Callable[[BridgeStatus], bool]


BINARY_SENSORS: tuple[RfBridgeBinarySensorDescription, ...] = (
    RfBridgeBinarySensorDescription(
        key="read_only",
        translation_key="read_only",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda status: status.read_only,
    ),
    RfBridgeBinarySensorDescription(
        key="restore_incomplete",
        translation_key="restore_incomplete",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda status: status.restore_incomplete,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RfClonerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the bridge's problem indicators."""
    coordinator = entry.runtime_data.coordinator
    bridge_id = entry.runtime_data.bridge_id
    async_add_entities(
        RfBridgeBinarySensor(coordinator, bridge_id, description)
        for description in BINARY_SENSORS
    )


class RfBridgeBinarySensor(RfBridgeEntity, BinarySensorEntity):
    """One problem state of the bridge."""

    entity_description: RfBridgeBinarySensorDescription

    def __init__(self, coordinator, bridge_id: str, description) -> None:
        """Describe the indicator."""
        super().__init__(coordinator, bridge_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Whether the condition holds, or None before the first successful poll."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Name the storage fault behind a read-only bridge."""
        if self.entity_description.key != "read_only" or self.coordinator.data is None:
            return None
        return {"fault": self.coordinator.data.fault}
