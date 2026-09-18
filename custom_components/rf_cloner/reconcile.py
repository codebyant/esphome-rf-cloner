"""Keeps Home Assistant's command subentries in step with the device's registry.

The device is the authority. A subentry is a local mirror of one command, so this module only
ever follows what rf_status reported: it adopts commands it has not seen, drops mirrors whose
command is gone, and follows renames made anywhere.

It also turns the one gesture that flows the other way - a user deleting a subentry in the Home
Assistant UI - into a delete on the device.
"""

from __future__ import annotations

import logging
import time

from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_COMMAND_ID, SUBENTRY_TYPE_COMMAND
from .coordinator import RfBridgeCoordinator
from .models import BridgeStatus

_LOGGER = logging.getLogger(__name__)

# How long a learn flow may hold a command id before the poller adopts it anyway. Covers the
# gap between the device storing a command and the flow that learned it creating its subentry,
# and self-heals if that flow is abandoned in between.
RESERVATION_SECONDS = 60.0


def command_unique_id(bridge_id: str, command_id: int) -> str:
    """The stable identity of one command, used for both its subentry and its entities."""
    return f"{bridge_id}_{command_id}"


def subentry_command_id(subentry: ConfigSubentry) -> int | None:
    """The command id a subentry mirrors, or None when its data is unusable."""
    raw = subentry.data.get(CONF_COMMAND_ID)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@callback
def command_subentries(entry: ConfigEntry) -> dict[int, ConfigSubentry]:
    """Index this entry's command subentries by the command id each mirrors."""
    found: dict[int, ConfigSubentry] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_COMMAND:
            continue
        command_id = subentry_command_id(subentry)
        if command_id is not None:
            found[command_id] = subentry
    return found


class CommandReconciler:
    """Mirrors the device's command list into config subentries."""

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
        # Subentry ids this object removed itself. A removal the device already told us about must
        # not be echoed back to the device as a delete.
        self._removed_by_us: set[str] = set()
        # Command id per subentry id, as of the last time we looked. A subentry that disappears
        # from this map without us removing it was removed by the user, and that is the one case
        # where Home Assistant mutates the device.
        self._mirrored: dict[str, int] = {}
        # Command names a subentry flow is about to claim, with the time each claim lapses.
        self._reserved: dict[str, float] = {}
        # Only while this is set does a vanished subentry mean the user deleted a command.
        # It is off for the whole of setup and from the first moment of teardown, so no
        # lifecycle event can be mistaken for a deletion.
        self._active = False
        self._identity_mismatch = False

    @property
    def identity_mismatch(self) -> bool:
        """Whether the device is reporting an identity other than the one this entry adopted.

        True means replacement or factory-reset hardware. Reconciliation stops while it holds, so
        the local mirror survives to be restored from.
        """
        return self._identity_mismatch

    @callback
    def async_activate(self) -> None:
        """Start honouring user-initiated subentry removals.

        Called once the entry is fully set up, and only then: every subentry that exists at this
        point is recorded as already mirrored, so nothing created during setup looks like a
        change.
        """
        self.async_snapshot_mirror()
        self._active = True

    @callback
    def async_deactivate(self) -> None:
        """Stop honouring removals. Registered as an unload hook."""
        self._active = False

    @property
    def _accepts_user_removals(self) -> bool:
        """Whether a vanished subentry can currently be attributed to the user.

        Both conditions matter. The flag closes the window around setup and teardown, and the
        entry state closes the window around a reload, where the entry is briefly not loaded while
        this object is still referenced.
        """
        return self._active and self._entry.state is ConfigEntryState.LOADED

    @callback
    def async_reserve(self, name: str) -> None:
        """Claim a command name on behalf of a subentry flow that is about to learn it.

        A subentry flow has to create its own mirror to finish. Without this claim, a poll landing
        between the device storing the command and the flow completing would adopt it first, and
        the flow would then fail on the duplicate identity. Claimed before the learn is armed,
        because the command id does not exist until after it succeeds.
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
    def async_snapshot_mirror(self) -> None:
        """Record the current subentries, without acting on them."""
        self._mirrored = {
            subentry.subentry_id: command_id
            for command_id, subentry in command_subentries(self._entry).items()
        }

    @callback
    def async_reconcile(self, status: BridgeStatus) -> None:
        """Bring the subentries in line with `status`."""
        expected_bridge_id = self._entry.unique_id
        if expected_bridge_id is not None and status.bridge_id != expected_bridge_id:
            if not self._identity_mismatch:
                _LOGGER.warning(
                    "Bridge at %s reports identity %s but this entry is %s; leaving the stored"
                    " commands untouched so they can be restored",
                    self._entry.title,
                    status.bridge_id,
                    expected_bridge_id,
                )
            self._identity_mismatch = True
            return
        self._identity_mismatch = False
        # Proven equal to status.bridge_id by the check above; preferred because it is the
        # identity the entities and subentries are keyed by.
        bridge_id = expected_bridge_id or status.bridge_id

        if status.restore_incomplete:
            # The registry is mid-restore and its listing is not yet the final one.
            return

        existing = command_subentries(self._entry)
        reported = status.by_id

        for command_id, command in reported.items():
            subentry = existing.get(command_id)
            if subentry is None:
                if not self._async_is_reserved(command.name):
                    self._async_add(bridge_id, command_id, command.name)
            elif subentry.title != command.name:
                self._hass.config_entries.async_update_subentry(
                    self._entry, subentry, title=command.name
                )

        for command_id, subentry in existing.items():
            reported_command = reported.get(command_id)
            if reported_command is None:
                self._async_remove(subentry)
            else:
                # The flow that claimed this name has created its mirror; the claim is spent.
                self._reserved.pop(reported_command.name, None)

        self.async_snapshot_mirror()

    @callback
    def _async_add(self, bridge_id: str, command_id: int, name: str) -> None:
        """Adopt a command the device has and Home Assistant does not."""
        _LOGGER.debug("Adopting command %s (%s) from the device", command_id, name)
        self._hass.config_entries.async_add_subentry(
            self._entry,
            ConfigSubentry(
                data={CONF_COMMAND_ID: command_id},
                subentry_type=SUBENTRY_TYPE_COMMAND,
                title=name,
                unique_id=command_unique_id(bridge_id, command_id),
            ),
        )

    @callback
    def _async_remove(self, subentry: ConfigSubentry) -> None:
        """Drop a mirror whose command the device no longer has."""
        _LOGGER.debug("Dropping mirror of command %s; the device no longer has it", subentry.title)
        self._removed_by_us.add(subentry.subentry_id)
        self._hass.config_entries.async_remove_subentry(self._entry, subentry.subentry_id)

    async def async_handle_entry_update(self) -> None:
        """React to a subentry the user removed in the Home Assistant UI.

        Deleting a command is destructive and irreversible on the device, so a removal is pushed
        through only when it can be positively attributed to the user. Three things have to hold:
        the entry is loaded and fully set up, the removal was not one this object made while
        following the device, and the device still reports the command. Home Assistant core never
        removes a subentry on its own - unload, reload and entry removal all leave `subentries`
        untouched - so anything else is treated as bookkeeping and only updates the mirror.
        """
        if not self._accepts_user_removals:
            self.async_snapshot_mirror()
            return

        current = set(self._entry.subentries)
        for subentry_id, command_id in list(self._mirrored.items()):
            if subentry_id in current:
                continue
            if subentry_id in self._removed_by_us:
                self._removed_by_us.discard(subentry_id)
                self._mirrored.pop(subentry_id, None)
                continue
            self._mirrored.pop(subentry_id, None)
            await self._async_delete_on_device(command_id)
        self.async_snapshot_mirror()

    async def _async_delete_on_device(self, command_id: int) -> None:
        """Push a user-initiated deletion through to the device."""
        # Re-checked here because the loop that calls this awaits between removals, and a reload
        # or unload may have started in the meantime.
        if not self._accepts_user_removals:
            return
        status = self._coordinator.data
        command = status.by_id.get(command_id) if status else None
        if command is None:
            # Already gone from the device; the mirror was simply stale.
            return
        _LOGGER.info("Deleting command %s (%s) on the device", command_id, command.name)
        try:
            await self._coordinator.async_delete(command_id, command.name)
        except HomeAssistantError as err:
            _LOGGER.error("Could not delete command %s on the device: %s", command_id, err)
