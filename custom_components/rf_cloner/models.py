"""Typed views over the payloads the bridge's actions return.

The device is the authority for all of this; nothing here is ever invented locally. Parsing is
deliberately tolerant about absent keys so a bridge running older firmware still yields a usable
status instead of failing the whole update.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Self

from .const import NAME_ALLOWED_EXTRA, NAME_MAX_LENGTH


@dataclass(frozen=True, slots=True)
class CommandInfo:
    """One row of the registry listing, without the waveform."""

    command_id: int
    name: str
    pulses: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build a row from one entry of rf_status's `commands` array."""
        return cls(
            command_id=int(payload["id"]),
            name=str(payload["name"]),
            pulses=int(payload.get("pulses", 0)),
        )


@dataclass(frozen=True, slots=True)
class BridgeStatus:
    """The canonical registry read, as returned by rf_status."""

    bridge_id: str
    revision: int
    next_command_id: int
    restore_incomplete: bool
    read_only: bool
    fault: str
    state: str
    last_result: str
    count: int
    max_commands: int
    max_pulses: int
    used_bytes: int
    capacity_bytes: int
    commands: tuple[CommandInfo, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build a status from an rf_status response."""
        return cls(
            bridge_id=str(payload["bridge_id"]),
            revision=int(payload["revision"]),
            next_command_id=int(payload["next_command_id"]),
            restore_incomplete=bool(payload.get("restore_incomplete", False)),
            read_only=bool(payload.get("read_only", False)),
            fault=str(payload.get("fault", "none")),
            state=str(payload.get("state", "unknown")),
            last_result=str(payload.get("last_result", "")),
            count=int(payload.get("count", 0)),
            max_commands=int(payload.get("max_commands", 0)),
            max_pulses=int(payload.get("max_pulses", 0)),
            used_bytes=int(payload.get("used_bytes", 0)),
            capacity_bytes=int(payload.get("capacity_bytes", 0)),
            commands=tuple(
                CommandInfo.from_payload(item) for item in payload.get("commands", ())
            ),
        )

    @property
    def by_id(self) -> dict[int, CommandInfo]:
        """Index the listing by the immutable command id."""
        return {command.command_id: command for command in self.commands}

    @property
    def by_name(self) -> dict[str, CommandInfo]:
        """Index the listing by the mutable name."""
        return {command.name: command for command in self.commands}

    @property
    def is_full(self) -> bool:
        """Whether another command would exceed the configured slot count."""
        return self.max_commands > 0 and self.count >= self.max_commands

    @property
    def accepts_mutations(self) -> bool:
        """Whether the device would currently accept a learn, rename or delete."""
        return not self.read_only and not self.restore_incomplete


@dataclass(frozen=True, slots=True)
class ExportedCommand:
    """The portable representation of one command, as returned by rf_export.

    Schema-versioned independently of the device's on-flash format, and carries everything needed
    to reproduce the command on replacement hardware.
    """

    schema: int
    bridge_id: str
    command_id: int
    name: str
    gap_us: int
    repeat_times: int
    frequency_hz: int
    modulation: int
    timings: tuple[int, ...] = field(default=())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self | None:
        """Build an export from an rf_export response, or None when the id is unknown."""
        if not payload.get("found"):
            return None
        return cls(
            schema=int(payload.get("schema", 1)),
            bridge_id=str(payload["bridge_id"]),
            command_id=int(payload["command_id"]),
            name=str(payload["name"]),
            gap_us=int(payload["gap_us"]),
            repeat_times=int(payload["repeat_times"]),
            frequency_hz=int(payload["frequency_hz"]),
            modulation=int(payload["modulation"]),
            timings=tuple(int(value) for value in payload.get("timings", ())),
        )

    def as_dict(self) -> dict[str, Any]:
        """Render back to the portable wire shape, so a snapshot round-trips byte for byte."""
        return {
            "schema": self.schema,
            "found": True,
            "bridge_id": self.bridge_id,
            "command_id": self.command_id,
            "name": self.name,
            "pulses": len(self.timings),
            "gap_us": self.gap_us,
            "repeat_times": self.repeat_times,
            "frequency_hz": self.frequency_hz,
            "modulation": self.modulation,
            "timings": list(self.timings),
        }


def validate_command_name(name: str) -> str | None:
    """Return a translation key when `name` is not one the device would store.

    Mirrors CommandStore::validate_name so a config flow can reject a name in the form rather
    than firing a learn that the device will refuse.
    """
    if not name:
        return "name_empty"
    if len(name) > NAME_MAX_LENGTH:
        return "name_too_long"
    if not all(char.isascii() and (char.isalnum() or char in NAME_ALLOWED_EXTRA) for char in name):
        return "name_invalid"
    return None
