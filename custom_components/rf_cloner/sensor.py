"""Diagnostics for the bridge itself.

These mirror what rf_status reports about the registry as a whole, so storage pressure and the
outcome of the last operation are visible without opening a log.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .data import RfClonerConfigEntry
from .entity import RfBridgeEntity
from .models import BridgeStatus


@dataclass(frozen=True, kw_only=True)
class RfBridgeSensorDescription(SensorEntityDescription):
    """A sensor reading one field of the bridge's status."""

    value: Callable[[BridgeStatus], str | int]


SENSORS: tuple[RfBridgeSensorDescription, ...] = (
    RfBridgeSensorDescription(
        key="command_count",
        translation_key="command_count",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda status: status.count,
    ),
    RfBridgeSensorDescription(
        key="storage_used",
        translation_key="storage_used",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda status: status.used_bytes,
    ),
    RfBridgeSensorDescription(
        key="learn_state",
        translation_key="learn_state",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "armed", "captured", "failed", "unknown"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda status: status.state,
    ),
    RfBridgeSensorDescription(
        key="last_result",
        translation_key="last_result",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda status: status.last_result or "none",
    ),
    RfBridgeSensorDescription(
        key="revision",
        translation_key="revision",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda status: status.revision,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RfClonerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the bridge's diagnostic sensors."""
    coordinator = entry.runtime_data.coordinator
    bridge_id = entry.runtime_data.bridge_id
    async_add_entities(
        RfBridgeSensor(coordinator, bridge_id, description) for description in SENSORS
    )


class RfBridgeSensor(RfBridgeEntity, SensorEntity):
    """One field of the bridge's status."""

    entity_description: RfBridgeSensorDescription

    def __init__(self, coordinator, bridge_id: str, description) -> None:
        """Describe the reading."""
        super().__init__(coordinator, bridge_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | None:
        """The field's current value, or None before the first successful poll."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value(self.coordinator.data)
