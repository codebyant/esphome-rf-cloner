"""RF targets: the Home Assistant-side model of the equipment a bridge actually drives.

A target is one piece of equipment - a ceiling fan, a gate, a projector - and the commands that
drive it. The device knows nothing about any of this: it stores waveforms under ids, and a target
is purely how Home Assistant chooses to present them. Nothing in this module ever reaches the RF
registry, so no operation here changes a waveform, a command id or the bridge's revision.

Two separate stores, for one reason each:

- A target is a config subentry, because a Home Assistant device belongs to exactly one config
  entry and at most one subentry, and one target is one device.
- A command's organisation - which target it belongs to, and its icon - lives in the config
  entry's options, because removing a subentry removes everything stored in it. A command must
  outlive the target it was assigned to, so its metadata cannot be kept there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self
import uuid

from homeassistant.config_entries import ConfigEntry, ConfigSubentry

from .const import (
    CONF_AREA_ID,
    CONF_ICON,
    CONF_TARGET_ID,
    CONF_TARGET_TYPE,
    DEFAULT_COMMAND_ICON,
    OPT_COMMANDS,
    SUBENTRY_TYPE_TARGET,
    TARGET_TYPE_GENERIC,
    TARGET_TYPE_MODELS,
    TARGET_TYPES,
)


def new_target_id() -> str:
    """Mint an identity for a target.

    Independent of the subentry that carries it and of the target's name, so renaming a target,
    moving it between areas or changing its type all leave its device identity alone.
    """
    return uuid.uuid4().hex


def target_device_identifier(bridge_id: str, target_id: str) -> str:
    """The stable device identity of one target, scoped to the bridge that owns it."""
    return f"{bridge_id}_target_{target_id}"


def validate_target_type(value: Any) -> str:
    """Narrow a stored type to one this version knows, falling back to the generic one."""
    return value if value in TARGET_TYPES else TARGET_TYPE_GENERIC


def target_model(target_type: str) -> str:
    """What a target of this type calls itself on its device page."""
    return TARGET_TYPE_MODELS.get(target_type, TARGET_TYPE_MODELS[TARGET_TYPE_GENERIC])


@dataclass(frozen=True, slots=True)
class RfTarget:
    """One target, as read from the subentry that stores it."""

    target_id: str
    subentry_id: str
    name: str
    target_type: str
    # The area chosen when the target was created. Applied once, when its device first appears;
    # a later move by the user is theirs and is never overwritten.
    area_id: str | None

    @classmethod
    def from_subentry(cls, subentry: ConfigSubentry) -> Self | None:
        """Read a target from a subentry, or None when the subentry is not a usable one."""
        if subentry.subentry_type != SUBENTRY_TYPE_TARGET:
            return None
        target_id = subentry.data.get(CONF_TARGET_ID)
        if not isinstance(target_id, str) or not target_id:
            return None
        area_id = subentry.data.get(CONF_AREA_ID)
        return cls(
            target_id=target_id,
            subentry_id=subentry.subentry_id,
            name=subentry.title,
            target_type=validate_target_type(subentry.data.get(CONF_TARGET_TYPE)),
            area_id=area_id if isinstance(area_id, str) and area_id else None,
        )

    def device_identifier(self, bridge_id: str) -> str:
        """This target's device identity under `bridge_id`."""
        return target_device_identifier(bridge_id, self.target_id)


def targets(entry: ConfigEntry) -> dict[str, RfTarget]:
    """Every target this entry defines, indexed by target id."""
    found: dict[str, RfTarget] = {}
    for subentry in entry.subentries.values():
        target = RfTarget.from_subentry(subentry)
        if target is not None:
            found[target.target_id] = target
    return found


@dataclass(frozen=True, slots=True)
class CommandMeta:
    """One command's Home Assistant-side organisation.

    Both fields are optional and both default to absent, which is what makes an unmigrated 0.1
    command and a command the user has never organised indistinguishable - as they should be.
    """

    target_id: str | None = None
    icon: str | None = None

    @property
    def is_empty(self) -> bool:
        """Whether this carries nothing worth storing."""
        return self.target_id is None and self.icon is None

    @classmethod
    def from_stored(cls, payload: Any) -> Self:
        """Read one command's metadata, tolerating anything a hand-edited store may hold."""
        if not isinstance(payload, dict):
            return cls()
        target_id = payload.get(CONF_TARGET_ID)
        icon = payload.get(CONF_ICON)
        return cls(
            target_id=target_id if isinstance(target_id, str) and target_id else None,
            icon=icon if isinstance(icon, str) and icon else None,
        )

    def as_stored(self) -> dict[str, str]:
        """Render for the options store, leaving absent fields out."""
        stored: dict[str, str] = {}
        if self.target_id is not None:
            stored[CONF_TARGET_ID] = self.target_id
        if self.icon is not None:
            stored[CONF_ICON] = self.icon
        return stored


def command_meta(entry: ConfigEntry) -> dict[int, CommandMeta]:
    """Every command's organisation, indexed by the immutable command id.

    Command ids are integers here and strings in the store, because options are persisted as JSON
    and JSON object keys are strings.
    """
    stored = entry.options.get(OPT_COMMANDS)
    if not isinstance(stored, dict):
        return {}
    found: dict[int, CommandMeta] = {}
    for key, value in stored.items():
        try:
            command_id = int(key)
        except (TypeError, ValueError):
            continue
        meta = CommandMeta.from_stored(value)
        if not meta.is_empty:
            found[command_id] = meta
    return found


def meta_for(entry: ConfigEntry, command_id: int) -> CommandMeta:
    """One command's organisation, or an empty one when it has never been organised."""
    return command_meta(entry).get(command_id, CommandMeta())


def assigned_target(entry: ConfigEntry, command_id: int) -> RfTarget | None:
    """The target a command belongs to, or None when it is unassigned.

    A target id naming a target that no longer exists reads as unassigned, so a target removed
    behind this integration's back cannot strand a command on a device that is gone.
    """
    target_id = meta_for(entry, command_id).target_id
    return None if target_id is None else targets(entry).get(target_id)


def commands_for_target(entry: ConfigEntry, target_id: str) -> set[int]:
    """The command ids currently assigned to one target."""
    return {
        command_id
        for command_id, meta in command_meta(entry).items()
        if meta.target_id == target_id
    }


def options_with(
    entry: ConfigEntry, updates: dict[int, CommandMeta]
) -> dict[str, Any]:
    """This entry's options with `updates` applied, ready for async_update_entry.

    An entry mapping to an empty CommandMeta is dropped rather than stored, so a command that has
    been unassigned and given no icon leaves nothing behind.
    """
    merged = dict(command_meta(entry))
    for command_id, meta in updates.items():
        if meta.is_empty:
            merged.pop(command_id, None)
        else:
            merged[command_id] = meta
    options = dict(entry.options)
    options[OPT_COMMANDS] = {
        str(command_id): meta.as_stored() for command_id, meta in sorted(merged.items())
    }
    return options


def options_pruned_to(
    entry: ConfigEntry, command_ids: set[int] | None
) -> dict[str, Any] | None:
    """This entry's options with metadata for absent commands and targets dropped.

    Returns None when nothing needed pruning, so a caller can skip a write - and, with it, the
    entry update that a write would broadcast.

    Two kinds of stale metadata are cleared: a command the device no longer has, and an
    assignment to a target that no longer exists. The second is what makes a deleted target's
    commands unassigned rather than stranded.

    `command_ids` is None when the caller has no trustworthy listing to prune against - before
    the first poll, mid-restore, or while the hardware is reporting someone else's identity. Only
    dangling target assignments are cleared then, because "the device listed no commands" and
    "we have not been told what the device has" must not look the same: treating the second as
    the first would discard every icon and assignment the user has set.
    """
    current = command_meta(entry)
    known_targets = targets(entry)
    updates: dict[int, CommandMeta] = {}
    for command_id, meta in current.items():
        if command_ids is not None and command_id not in command_ids:
            updates[command_id] = CommandMeta()
        elif meta.target_id is not None and meta.target_id not in known_targets:
            updates[command_id] = CommandMeta(target_id=None, icon=meta.icon)
    if not updates:
        return None
    return options_with(entry, updates)


def command_icon(entry: ConfigEntry, command_id: int) -> str:
    """The icon this integration offers for one command.

    Only ever the integration's suggestion: Home Assistant records it as the entity's original
    icon, and an icon the user set on the entity registry record wins over it.
    """
    return meta_for(entry, command_id).icon or DEFAULT_COMMAND_ICON
