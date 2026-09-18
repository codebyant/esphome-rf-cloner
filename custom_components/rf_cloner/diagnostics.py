"""Diagnostics download for one bridge.

Carries the status the integration last read and a description of the replica. The replica's
waveforms are left out: they are large, and rf_cloner.export_snapshot is the way to get them.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .data import RfClonerConfigEntry
from .reconcile import command_subentries


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RfClonerConfigEntry
) -> dict[str, Any]:
    """Describe one bridge's current state."""
    runtime = entry.runtime_data
    status = runtime.coordinator.data
    snapshot = runtime.snapshots.snapshot
    return {
        "entry": {
            "unique_id": entry.unique_id,
            "esphome_entry_id": runtime.bound_to[0],
            "action_prefix": runtime.bound_to[1],
            "subentries": len(command_subentries(entry)),
        },
        "transport": {
            "node_name": runtime.transport.node_name,
            "available": runtime.transport.available,
            "supports_delete_id": runtime.transport.supports("delete_id"),
        },
        "status": None
        if status is None
        else {
            "bridge_id": status.bridge_id,
            "revision": status.revision,
            "next_command_id": status.next_command_id,
            "restore_incomplete": status.restore_incomplete,
            "read_only": status.read_only,
            "fault": status.fault,
            "state": status.state,
            "last_result": status.last_result,
            "count": status.count,
            "max_commands": status.max_commands,
            "max_pulses": status.max_pulses,
            "used_bytes": status.used_bytes,
            "capacity_bytes": status.capacity_bytes,
            "commands": [
                {"id": item.command_id, "name": item.name, "pulses": item.pulses}
                for item in status.commands
            ],
        },
        "snapshot": None
        if snapshot is None
        else {
            "bridge_id": snapshot.bridge_id,
            "revision": snapshot.revision,
            "next_command_id": snapshot.next_command_id,
            "captured_at": snapshot.captured_at.isoformat(),
            "commands": [
                {
                    "id": command.command_id,
                    "name": command.name,
                    "pulses": len(command.timings),
                }
                for command in snapshot.commands
            ],
        },
        "identity_mismatch": runtime.reconciler.identity_mismatch,
    }
