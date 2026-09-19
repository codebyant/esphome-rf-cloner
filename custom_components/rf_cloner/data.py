"""Runtime objects a loaded config entry carries."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .coordinator import RfBridgeCoordinator
from .reconcile import CommandReconciler
from .snapshot import SnapshotManager
from .transport import BridgeTransport


@dataclass(slots=True)
class RfClonerRuntimeData:
    """Everything one loaded bridge needs at runtime."""

    # The logical bridge's identity, taken from the config entry rather than from whatever the
    # hardware currently reports. The two differ exactly while a replacement is pending, and
    # entity and device identity must not follow the hardware across that window.
    bridge_id: str
    # The bridge's own Home Assistant device, which every target device hangs off.
    bridge_device_id: str
    transport: BridgeTransport
    coordinator: RfBridgeCoordinator
    reconciler: CommandReconciler
    snapshots: SnapshotManager
    # The (ESPHome entry, action prefix) this entry was loaded against. A reconfigure that
    # changes it needs a reload, which the entry's update listener schedules.
    bound_to: tuple[str, str]


type RfClonerConfigEntry = ConfigEntry[RfClonerRuntimeData]
