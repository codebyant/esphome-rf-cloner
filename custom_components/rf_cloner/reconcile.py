"""Keeps Home Assistant's view of the bridge in step with the device's registry.

The device is the authority for which commands exist, what they are called and what they replay.
This module only ever follows what rf_status reported.

What it maintains on the Home Assistant side is the organisation around those commands:

- the set of command ids the button platform should have entities for,
- the target devices those entities belong to,
- and the command metadata in the entry's options, pruned of anything stale.

Two gestures flow the other way, and both are organisational rather than destructive. A target
subentry the user removed in the Home Assistant UI unassigns that target's commands; it does not
delete them from the device. Deleting an RF command is a separate, explicit act, and lives in the
options flow.

That is a deliberate change from 0.1, where a subentry was a command and removing one deleted it.
In 0.2 a subentry is a target - a piece of equipment - and removing a piece of equipment from
Home Assistant is not a reason to erase waveforms from the bridge.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import time

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .coordinator import RfBridgeCoordinator
from .models import BridgeStatus
from .targets import options_pruned_to, targets

_LOGGER = logging.getLogger(__name__)

# How long a learn flow may hold a command name before the poller adopts the command anyway.
# Covers the gap between the device storing a command and the flow that learned it recording the
# target it should land on, and self-heals if that flow is abandoned in between.
RESERVATION_SECONDS = 60.0


def command_id_from_unique_id(bridge_id: str, unique_id: str) -> int | None:
    """The command id a unique id names, or None when it names something else.

    The bridge's own entities are keyed the same way but with a word rather than a number, so a
    suffix that is not an integer is what tells them apart.
    """
    prefix = f"{bridge_id}_"
    if not unique_id.startswith(prefix):
        return None
    try:
        return int(unique_id[len(prefix) :])
    except ValueError:
        return None


def command_unique_id(bridge_id: str, command_id: int) -> str:
    """The stable identity of one command's entities.

    Unchanged since 0.1, and it has to stay that way: it is what keeps a user's dashboards,
    automations and history pointing at the same button across upgrades, renames, target moves
    and hardware replacement.
    """
    return f"{bridge_id}_{command_id}"


class CommandReconciler:
    """Mirrors the device's command list into Home Assistant's organisation."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: RfBridgeCoordinator,
    ) -> None:
        """Bind the mirror to one config entry."""
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        # Command names a learn flow is about to claim, with the time each claim lapses.
        self._reserved: dict[str, float] = {}
        self._active = False
        self._identity_mismatch = False
        # Called when the set of commands, or where they belong, has changed. The button
        # platform subscribes so it can add, move and drop entities.
        self._listeners: list[Callable[[], None]] = []
        # The target subentry ids seen at the last look, so a target the user removed can be
        # told apart from one this integration never had.
        self._known_targets: set[str] = set()

    @property
    def identity_mismatch(self) -> bool:
        """Whether the device is reporting an identity other than the one this entry adopted.

        True means replacement or factory-reset hardware. Reconciliation stops while it holds, so
        the local mirror and the user's organisation both survive to be restored onto.
        """
        return self._identity_mismatch

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to changes in the command set or its organisation."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _async_notify(self) -> None:
        """Tell the platforms to look again."""
        for listener in list(self._listeners):
            listener()

    @callback
    def async_activate(self) -> None:
        """Start honouring user-initiated subentry removals."""
        self.async_snapshot_targets()
        self._active = True

    @callback
    def async_deactivate(self) -> None:
        """Stop honouring removals. Registered as an unload hook."""
        self._active = False

    @property
    def _accepts_user_removals(self) -> bool:
        """Whether a vanished subentry can currently be attributed to the user.

        Both conditions matter. The flag closes the window around setup and teardown, and the
        entry state closes the window around a reload, where the entry is briefly not loaded
        while this object is still referenced.
        """
        return self._active and self._entry.state is ConfigEntryState.LOADED

    @callback
    def async_snapshot_targets(self) -> None:
        """Record the current target subentries, without acting on them."""
        self._known_targets = {
            target.subentry_id for target in targets(self._entry).values()
        }

    @callback
    def async_reserve(self, name: str) -> None:
        """Claim a command name on behalf of a learn flow that is about to capture it.

        Claimed before the learn is armed, because the command id does not exist until after it
        succeeds, and a poll landing mid-capture would otherwise materialise the command as
        unassigned before the flow could record the target the user picked.
        """
        self._reserved[name] = time.monotonic() + RESERVATION_SECONDS

    @callback
    def async_release(self, name: str) -> None:
        """Drop a claim whose flow ended without learning anything."""
        self._reserved.pop(name, None)

    @callback
    def _async_is_reserved(self, name: str) -> bool:
        """Whether a live claim is holding `name`."""
        lapses_at = self._reserved.get(name)
        if lapses_at is None:
            return False
        if time.monotonic() >= lapses_at:
            del self._reserved[name]
            return False
        return True

    @callback
    def async_listed_commands(self, status: BridgeStatus | None = None) -> set[int] | None:
        """Every command id the device last listed, or None when there is no usable listing.

        None is not an empty set. It means the device has not been read, is mid-restore, or is
        reporting someone else's identity - none of which is evidence that a command is gone.
        """
        status = status if status is not None else self._coordinator.data
        if status is None or self._identity_mismatch or status.restore_incomplete:
            return None
        return {command.command_id for command in status.commands}

    @callback
    def async_visible_commands(self, status: BridgeStatus | None = None) -> set[int]:
        """The command ids the platforms should currently have entities for.

        A command a learn flow is still holding is left out, so it appears once, already on the
        target the user chose, rather than appearing unassigned and moving a moment later.
        """
        status = status if status is not None else self._coordinator.data
        if status is None or self._identity_mismatch or status.restore_incomplete:
            return set()
        return {
            command.command_id
            for command in status.commands
            if not self._async_is_reserved(command.name)
        }

    @callback
    def async_reconcile(self, status: BridgeStatus) -> None:
        """Bring Home Assistant's organisation in line with `status`."""
        expected_bridge_id = self._entry.unique_id
        if expected_bridge_id is not None and status.bridge_id != expected_bridge_id:
            if not self._identity_mismatch:
                _LOGGER.warning(
                    "Bridge at %s reports identity %s but this entry is %s; leaving the stored"
                    " commands and their organisation untouched so they can be restored",
                    self._entry.title,
                    status.bridge_id,
                    expected_bridge_id,
                )
            self._identity_mismatch = True
            return
        self._identity_mismatch = False

        if status.restore_incomplete:
            # The registry is mid-restore and its listing is not yet the final one.
            return

        for command in status.commands:
            # A flow that claimed this name has had its command materialise; the claim is spent.
            self._reserved.pop(command.name, None)

        listed = self.async_listed_commands(status)
        # A command the device no longer lists is gone for good - deleted here, on the bridge's
        # own web page, or by a script. Its entity registry record has to go with it, or the
        # entity id it holds would be reserved forever by something that cannot come back.
        self._async_purge_removed_commands(listed)

        # Drop metadata for commands the device no longer has, and assignments to targets that
        # no longer exist. The second is what turns a removed target's commands into unassigned
        # ones rather than leaving them pointing at a device that is gone.
        pruned = options_pruned_to(self._entry, listed)
        if pruned is not None:
            self._hass.config_entries.async_update_entry(self._entry, options=pruned)

        self.async_snapshot_targets()
        self._async_notify()

    @callback
    def _async_purge_removed_commands(self, listed: set[int] | None) -> None:
        """Remove the entity registry records of commands the device no longer has.

        Only ever called with a listing the device actually produced: `listed` is None while the
        status cannot be trusted, and treating that as "the device has nothing" would delete
        every command's history.
        """
        if listed is None:
            return
        bridge_id = self._entry.unique_id
        if bridge_id is None:
            return
        registry = er.async_get(self._hass)
        for record in er.async_entries_for_config_entry(registry, self._entry.entry_id):
            command_id = command_id_from_unique_id(bridge_id, record.unique_id)
            if command_id is None or command_id in listed:
                continue
            _LOGGER.debug(
                "Removing %s; the bridge no longer has command %s",
                record.entity_id,
                command_id,
            )
            registry.async_remove(record.entity_id)

    async def async_handle_entry_update(self) -> None:
        """React to a change of the entry's subentries or options.

        A target subentry the user removed has already taken its device and its entities with it
        by the time this runs - Home Assistant clears a subentry's registry records before the
        update listeners are called, and there is no hook that runs in between. So the work here
        is to unassign that target's commands, which makes the next reconcile put their buttons
        back as unassigned entities. Home Assistant restores an entity's id, area, name and icon
        from its deleted record when it is re-created under the same unique id, so the buttons
        come back as themselves.

        No RF operation happens on this path. Removing a target is organisation, not deletion.
        """
        if not self._accepts_user_removals:
            self.async_snapshot_targets()
            return

        current = {target.subentry_id for target in targets(self._entry).values()}
        removed = self._known_targets - current
        if removed:
            _LOGGER.info(
                "%s target(s) removed from %s; their commands are now unassigned and remain"
                " stored on the bridge",
                len(removed),
                self._entry.title,
            )
        self.async_snapshot_targets()

        # Unlike a reconcile, which prunes against the read that has just arrived, this runs
        # against whatever the coordinator last held - and the update that triggered it is often
        # a learn recording the command it has just stored, which that read predates. Only ids the
        # read could have listed are pruned; anything newer waits for the next one.
        status = self._coordinator.data
        pruned = options_pruned_to(
            self._entry,
            self.async_listed_commands(status),
            listed_below=None if status is None else status.next_command_id,
        )
        if pruned is not None:
            self._hass.config_entries.async_update_entry(self._entry, options=pruned)
            # The update this schedules re-enters here, finds nothing left to prune and stops.
            return
        self._async_notify()
