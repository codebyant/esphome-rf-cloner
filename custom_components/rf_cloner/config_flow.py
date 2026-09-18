"""Config, subentry and reconfigure flows.

The config entry represents a logical bridge, identified by its bridge_id, and points at the
ESPHome config entry that currently carries it. That indirection is what makes hardware
replacement expressible: the same logical bridge is re-pointed at a new node and restored onto it.

Each learned command is a subentry, which is what gives Home Assistant a native way to add, rename
and remove commands without a dashboard of its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    ConfigEntrySelector,
    ConfigEntrySelectorConfig,
    TextSelector,
)

from .const import (
    CONF_ACTION_PREFIX,
    CONF_COMMAND_ID,
    CONF_ESPHOME_ENTRY_ID,
    CONF_NAME,
    DEFAULT_ACTION_PREFIX,
    DOMAIN,
    ESPHOME_DOMAIN,
    SUBENTRY_TYPE_COMMAND,
)
from .coordinator import LearnOutcome
from .data import RfClonerConfigEntry
from .models import BridgeStatus, validate_command_name
from .reconcile import command_unique_id, subentry_command_id
from .restore import RestoreNotPossible, async_restore, check_restore_target
from .transport import BridgeTransport

_LOGGER = logging.getLogger(__name__)


def _esphome_entry_selector() -> ConfigEntrySelector:
    """A picker limited to ESPHome config entries."""
    return ConfigEntrySelector(ConfigEntrySelectorConfig(integration=ESPHOME_DOMAIN))


class RfClonerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Bind a logical bridge to the ESPHome node that carries it."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with no replacement under consideration."""
        self._replacement: tuple[str, str, BridgeStatus] | None = None

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Learned commands are managed as subentries."""
        return {SUBENTRY_TYPE_COMMAND: CommandSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an ESPHome node and confirm it is running an rf_cloner bridge."""
        errors: dict[str, str] = {}
        if user_input is not None:
            esphome_entry_id = user_input[CONF_ESPHOME_ENTRY_ID]
            action_prefix = user_input.get(CONF_ACTION_PREFIX, DEFAULT_ACTION_PREFIX)
            status, error = await _async_probe(self.hass, esphome_entry_id, action_prefix)
            if status is None:
                errors["base"] = error or "unknown"
            else:
                await self.async_set_unique_id(status.bridge_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_suggested_title(self.hass, esphome_entry_id),
                    data={
                        CONF_ESPHOME_ENTRY_ID: esphome_entry_id,
                        CONF_ACTION_PREFIX: action_prefix,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ESPHOME_ENTRY_ID): _esphome_entry_selector(),
                    vol.Optional(
                        CONF_ACTION_PREFIX, default=DEFAULT_ACTION_PREFIX
                    ): TextSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-point this bridge at a different ESPHome node.

        The ordinary case is the same bridge reached through a renamed or re-added node. The
        interesting case is replacement hardware, which reports an identity of its own; that is
        offered a restore rather than silently adopted.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            esphome_entry_id = user_input[CONF_ESPHOME_ENTRY_ID]
            action_prefix = user_input.get(CONF_ACTION_PREFIX, DEFAULT_ACTION_PREFIX)
            status, error = await _async_probe(self.hass, esphome_entry_id, action_prefix)
            if status is None:
                errors["base"] = error or "unknown"
            elif status.bridge_id == entry.unique_id:
                return self._rebind(entry, esphome_entry_id, action_prefix)
            else:
                self._replacement = (esphome_entry_id, action_prefix, status)
                return await self.async_step_replace()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ESPHOME_ENTRY_ID,
                        default=entry.data.get(CONF_ESPHOME_ENTRY_ID),
                    ): _esphome_entry_selector(),
                    vol.Optional(
                        CONF_ACTION_PREFIX,
                        default=entry.data.get(CONF_ACTION_PREFIX, DEFAULT_ACTION_PREFIX),
                    ): TextSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_replace(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm restoring this bridge's snapshot onto replacement hardware."""
        entry = self._get_reconfigure_entry()
        assert self._replacement is not None
        esphome_entry_id, action_prefix, status = self._replacement

        runtime = getattr(entry, "runtime_data", None)
        snapshot = runtime.snapshots.snapshot if runtime is not None else None
        if snapshot is None:
            return self.async_abort(reason="no_snapshot")

        try:
            check_restore_target(status, snapshot)
        except RestoreNotPossible as err:
            return self.async_abort(
                reason="restore_not_possible",
                description_placeholders={"detail": str(err)},
            )

        if user_input is None:
            return self.async_show_form(
                step_id="replace",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "bridge_id": snapshot.bridge_id,
                    "found_bridge_id": status.bridge_id,
                    "commands": str(len(snapshot.commands)),
                },
            )

        transport = BridgeTransport(self.hass, esphome_entry_id, action_prefix)
        try:
            await async_restore(transport, snapshot)
        except HomeAssistantError as err:
            _LOGGER.error("Restore onto %s failed: %s", status.bridge_id, err)
            return self.async_abort(
                reason="restore_failed", description_placeholders={"detail": str(err)}
            )

        return self._rebind(
            entry, esphome_entry_id, action_prefix, reason="restore_successful"
        )

    @callback
    def _rebind(
        self,
        entry: ConfigEntry,
        esphome_entry_id: str,
        action_prefix: str,
        reason: str = "reconfigure_successful",
    ) -> ConfigFlowResult:
        """Point the entry at a node and let its update listener schedule the reload."""
        self.hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ESPHOME_ENTRY_ID: esphome_entry_id,
                CONF_ACTION_PREFIX: action_prefix,
            },
        )
        return self.async_abort(reason=reason)


class CommandSubentryFlow(ConfigSubentryFlow):
    """Learn a new command, or rename one that already exists."""

    def __init__(self) -> None:
        """Start with nothing claimed."""
        self._name: str = ""
        self._task: asyncio.Task[LearnOutcome] | None = None

    @property
    def _entry(self) -> RfClonerConfigEntry:
        """The bridge this command belongs to."""
        return self._get_entry()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Name the command, then capture it."""
        entry = self._entry
        runtime = getattr(entry, "runtime_data", None)
        status = runtime.coordinator.data if runtime is not None else None
        if runtime is None or status is None:
            return self.async_abort(reason="not_loaded")
        if not status.accepts_mutations:
            return self.async_abort(reason="not_writable")
        if status.is_full:
            return self.async_abort(reason="registry_full")

        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input[CONF_NAME]).strip()
            problem = validate_command_name(name)
            if problem is not None:
                errors[CONF_NAME] = problem
            elif name in status.by_name:
                errors[CONF_NAME] = "name_taken"
            else:
                self._name = name
                return await self.async_step_learn()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NAME): TextSelector()}),
            errors=errors,
        )

    async def async_step_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Arm the capture and wait for the device to report how it ended."""
        runtime = self._entry.runtime_data
        if self._task is None:
            # Claimed before the learn is armed, so a poll landing mid-capture cannot adopt the
            # command before this flow has created its subentry.
            runtime.reconciler.async_reserve(self._name)
            self._task = self.hass.async_create_task(
                runtime.coordinator.async_learn(self._name)
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="learn",
                progress_action="learning",
                progress_task=self._task,
                description_placeholders={"name": self._name},
            )
        return self.async_show_progress_done(next_step_id="learned")

    async def async_step_learned(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Turn the capture's outcome into a subentry, or explain why there is none."""
        runtime = self._entry.runtime_data
        assert self._task is not None
        try:
            outcome = self._task.result()
        except HomeAssistantError as err:
            runtime.reconciler.async_release(self._name)
            return self.async_abort(
                reason="learn_failed", description_placeholders={"detail": str(err)}
            )

        if not outcome.succeeded or outcome.command_id is None:
            runtime.reconciler.async_release(self._name)
            return self.async_abort(
                reason="learn_failed",
                description_placeholders={"detail": outcome.detail or "unknown"},
            )

        return self.async_create_entry(
            title=outcome.name,
            data={CONF_COMMAND_ID: outcome.command_id},
            unique_id=command_unique_id(runtime.bridge_id, outcome.command_id),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Rename a command on the device. It keeps its id, so entities are unaffected."""
        entry = self._entry
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            return self.async_abort(reason="not_loaded")

        subentry = entry.subentries[self._reconfigure_subentry_id]
        command_id = subentry_command_id(subentry)
        if command_id is None:
            return self.async_abort(reason="unknown_command")

        status = runtime.coordinator.data
        if status is None or not status.accepts_mutations:
            return self.async_abort(reason="not_writable")

        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input[CONF_NAME]).strip()
            problem = validate_command_name(name)
            taken_by = status.by_name.get(name)
            if problem is not None:
                errors[CONF_NAME] = problem
            elif taken_by is not None and taken_by.command_id != command_id:
                errors[CONF_NAME] = "name_taken"
            else:
                try:
                    await runtime.coordinator.async_rename(command_id, name)
                except HomeAssistantError as err:
                    _LOGGER.error("Could not rename command %s: %s", command_id, err)
                    errors["base"] = "rename_failed"
                else:
                    # Updated directly rather than through async_update_and_abort, which refuses
                    # to run while the entry carries the update listener this integration needs.
                    self.hass.config_entries.async_update_subentry(
                        entry, subentry, title=name
                    )
                    return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME, default=subentry.title): TextSelector()}
            ),
            errors=errors,
        )


async def _async_probe(
    hass: HomeAssistant, esphome_entry_id: str, action_prefix: str
) -> tuple[BridgeStatus | None, str | None]:
    """Read a candidate node's status, returning an error key instead of raising."""
    if hass.config_entries.async_get_entry(esphome_entry_id) is None:
        return None, "esphome_entry_missing"
    transport = BridgeTransport(hass, esphome_entry_id, action_prefix)
    if not transport.supports("status"):
        return None, "no_bridge_actions"
    try:
        return await transport.async_status(), None
    except HomeAssistantError as err:
        _LOGGER.debug("Probe of %s failed: %s", esphome_entry_id, err)
        return None, "cannot_connect"


@callback
def _suggested_title(hass: HomeAssistant, esphome_entry_id: str) -> str:
    """Name the bridge after the node it was found on."""
    entry = hass.config_entries.async_get_entry(esphome_entry_id)
    return entry.title if entry is not None else "RF Bridge"
