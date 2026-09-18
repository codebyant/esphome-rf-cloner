"""Restoring a snapshot onto replacement hardware.

Restore is always explicit. It adopts the original bridge_id, writes every command back under the
id it held, and restores the id counter, so entity ids, dashboards, automations and history all
keep pointing at the same commands they did before the hardware changed.

The device refuses to begin a restore on anything but an empty registry, which is what stops this
from overwriting a live bridge; the checks here exist to fail early with a reason a user can act
on rather than on an opaque device-side refusal.
"""

from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError

from .const import REVISION_MAX
from .models import BridgeStatus
from .snapshot import Snapshot
from .transport import BridgeTransport

_LOGGER = logging.getLogger(__name__)


class RestoreNotPossible(HomeAssistantError):
    """The target bridge is not in a state that can accept a restore."""


def check_restore_target(status: BridgeStatus, snapshot: Snapshot) -> None:
    """Raise when `status` describes a bridge that must not be restored onto."""
    if status.read_only:
        raise RestoreNotPossible(f"the target is in read-only mode ({status.fault})")
    if status.restore_incomplete:
        raise RestoreNotPossible("the target already has an unfinished restore")
    if status.count:
        raise RestoreNotPossible(
            f"the target already holds {status.count} command(s); clear it first"
        )
    if len(snapshot.commands) > status.max_commands:
        raise RestoreNotPossible(
            f"the target holds {status.max_commands} commands and the snapshot has"
            f" {len(snapshot.commands)}"
        )
    oversized = [
        command.name
        for command in snapshot.commands
        if len(command.timings) > status.max_pulses
    ]
    if oversized:
        raise RestoreNotPossible(
            f"the target's {status.max_pulses}-pulse limit cannot hold: {', '.join(oversized)}"
        )
    if not 0 <= snapshot.revision <= REVISION_MAX:
        # Checked before anything is written, because the commit that carries this value is the
        # last step: discovering it there would leave the restore open. A revision this far along
        # cannot come from a healthy bridge, which bounds its own revisions by the same domain.
        raise RestoreNotPossible(
            f"the snapshot's revision {snapshot.revision} is outside the bridge's revision domain"
            f" (0 to {REVISION_MAX})"
        )


async def async_restore(transport: BridgeTransport, snapshot: Snapshot) -> None:
    """Write a snapshot onto the bridge `transport` speaks to.

    The three phases are the device's own protocol. An interruption between them leaves the device
    reporting restore_incomplete, which survives a reboot and keeps the half-restored registry
    from being mistaken for a healthy one.

    The commit carries the snapshot's revision, so the restored bridge continues the logical
    bridge's revision sequence instead of restarting it. A restarted sequence could republish a
    revision this integration had already recorded against the same bridge_id, and the snapshot
    would then skip a refresh it needed.
    """
    status = await transport.async_status()
    check_restore_target(status, snapshot)

    _LOGGER.info(
        "Restoring %s command(s) onto %s as bridge %s, continuing from revision %s",
        len(snapshot.commands),
        status.bridge_id,
        snapshot.bridge_id,
        snapshot.revision,
    )
    await transport.async_restore_begin(snapshot.bridge_id, snapshot.next_command_id)
    for command in snapshot.commands:
        await transport.async_import(command)
    await transport.async_restore_commit(snapshot.revision)

    result = await transport.async_status()
    if result.bridge_id != snapshot.bridge_id:
        raise HomeAssistantError(
            f"Restore finished but the bridge reports {result.bridge_id},"
            f" expected {snapshot.bridge_id}"
        )
    if result.restore_incomplete:
        raise HomeAssistantError("Restore did not commit; the bridge is still mid-restore")
    if result.count != len(snapshot.commands):
        raise HomeAssistantError(
            f"Restore wrote {result.count} of {len(snapshot.commands)} command(s)"
        )
    if result.revision <= snapshot.revision:
        # The committed revision must move the logical bridge's sequence forward; if it did not,
        # this firmware ignored the source revision and the snapshot could later skip a refresh.
        raise HomeAssistantError(
            f"Restore committed revision {result.revision}, which does not advance the snapshot's"
            f" revision {snapshot.revision}; the bridge's firmware may predate"
            " rf_restore_commit(source_revision)"
        )
