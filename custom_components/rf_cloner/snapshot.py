"""Home Assistant's restorable replica of a bridge's registry.

One snapshot per bridge, holding the full portable representation of every command - waveform and
RF metadata included - which is what a restore to replacement hardware needs.

Refreshes are driven by the device's revision, so a snapshot is rewritten only when the registry
actually changed. A refresh that cannot read every command is abandoned whole: the previous
snapshot stays exactly as it was, because a partial replica is worse than a slightly stale one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, Self

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .models import BridgeStatus, ExportedCommand
from .transport import BridgeTransport

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A complete, self-contained copy of one bridge's registry."""

    bridge_id: str
    revision: int
    next_command_id: int
    captured_at: datetime
    commands: tuple[ExportedCommand, ...]

    @classmethod
    def from_stored(cls, payload: dict[str, Any]) -> Self | None:
        """Rebuild a snapshot from storage, or None when it cannot be read."""
        try:
            captured_at = dt_util.parse_datetime(payload["captured_at"])
            if captured_at is None:
                return None
            commands = []
            for item in payload["commands"]:
                command = ExportedCommand.from_payload(item)
                if command is None:
                    return None
                commands.append(command)
            return cls(
                bridge_id=str(payload["bridge_id"]),
                revision=int(payload["revision"]),
                next_command_id=int(payload["next_command_id"]),
                captured_at=captured_at,
                commands=tuple(commands),
            )
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning("Discarding an unreadable snapshot")
            return None

    def as_stored(self) -> dict[str, Any]:
        """Render for storage."""
        return {
            "bridge_id": self.bridge_id,
            "revision": self.revision,
            "next_command_id": self.next_command_id,
            "captured_at": self.captured_at.isoformat(),
            "commands": [command.as_dict() for command in self.commands],
        }


class SnapshotManager:
    """Owns one bridge's snapshot: when it is refreshed, and what it contains."""

    def __init__(
        self, hass: HomeAssistant, bridge_id: str, transport: BridgeTransport
    ) -> None:
        """Bind a snapshot store to one bridge.

        Keyed by bridge_id rather than by config entry, so the replica belongs to the logical
        bridge: removing and re-adding the integration finds the same snapshot again.
        """
        self._hass = hass
        self._bridge_id = bridge_id
        self._transport = transport
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{bridge_id}.snapshot"
        )
        self._snapshot: Snapshot | None = None

    @property
    def snapshot(self) -> Snapshot | None:
        """The current replica, or None when there has never been a complete one."""
        return self._snapshot

    async def async_load(self) -> None:
        """Read the stored replica, if any."""
        stored = await self._store.async_load()
        if stored is not None:
            self._snapshot = Snapshot.from_stored(stored)

    async def async_refresh(self, status: BridgeStatus) -> None:
        """Rewrite the replica if, and only if, the registry has changed since the last one.

        Never raises: a bridge that cannot be exported right now keeps the replica it had.
        """
        if status.bridge_id != self._bridge_id:
            # Replacement or factory-reset hardware, reporting an identity that is not this
            # bridge's. Its registry says nothing about this bridge, and overwriting the replica
            # with it would destroy the only thing a restore could read from.
            _LOGGER.debug(
                "Not snapshotting %s over %s: the bridge reports a different identity",
                status.bridge_id,
                self._bridge_id,
            )
            return
        if status.restore_incomplete:
            # Mid-restore, the listing is not the final one.
            return
        current = self._snapshot
        if (
            current is not None
            and current.bridge_id == status.bridge_id
            and current.revision == status.revision
        ):
            return

        try:
            commands = await self._async_export_all(status)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Keeping the previous snapshot of %s: %s", status.bridge_id, err
            )
            return
        if commands is None:
            return

        snapshot = Snapshot(
            bridge_id=status.bridge_id,
            revision=status.revision,
            next_command_id=status.next_command_id,
            captured_at=dt_util.utcnow(),
            commands=commands,
        )
        self._snapshot = snapshot
        await self._store.async_save(snapshot.as_stored())
        _LOGGER.debug(
            "Snapshotted %s at revision %s: %s command(s)",
            status.bridge_id,
            status.revision,
            len(commands),
        )

    async def _async_export_all(
        self, status: BridgeStatus
    ) -> tuple[ExportedCommand, ...] | None:
        """Export every listed command, or None when any of them could not be read."""
        exported: list[ExportedCommand] = []
        for command in status.commands:
            result = await self._transport.async_export(command.command_id)
            if result is None:
                _LOGGER.warning(
                    "Keeping the previous snapshot of %s: command %s disappeared mid-export",
                    status.bridge_id,
                    command.command_id,
                )
                return None
            exported.append(result)
        return tuple(exported)


async def async_remove_snapshot(hass: HomeAssistant, bridge_id: str) -> None:
    """Discard a bridge's replica, without having to load the bridge first."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{bridge_id}.snapshot").async_remove()
