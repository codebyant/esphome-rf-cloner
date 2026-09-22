"""Config, subentry, options and reconfigure flows.

The config entry represents a logical bridge, identified by its bridge_id, and points at the
ESPHome config entry that currently carries it. That indirection is what makes hardware
replacement expressible: the same logical bridge is re-pointed at a new node and restored onto it.

Each RF target is a subentry, which is what gives Home Assistant a native way to add, rename and
remove the equipment a bridge drives, and what lets each target own a device of its own.

Commands are not subentries. A command's entity has to be able to live on a target's device, and
a device belongs to exactly one subentry, so a command that was itself a subentry could never
join one. Commands are managed through the entry's options flow instead: learning, renaming,
choosing an icon, moving between targets and deleting are all steps of one menu.

Learning is the exception, and only in how it is reached. It is the thing a user does most, and
burying the most-used action behind the bridge's configure gear is how it shipped in 0.2.0. Home
Assistant's integration page renders one button per key of `async_get_supported_subentry_types`
and offers no other way to put a named action there, so learning is registered as a second key
whose flow ends in an abort and creates nothing. The steps themselves are shared with the options
flow rather than copied - see `LearnCommandSteps` - so the two entrances cannot drift apart.
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
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    AreaSelector,
    IconSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    ACTION_TYPE_LEARN_COMMAND,
    CONF_ACTION_PREFIX,
    CONF_AREA_ID,
    CONF_COMMAND_ID,
    CONF_ESPHOME_ENTRY_ID,
    CONF_ICON,
    CONF_NAME,
    CONF_TARGET_ID,
    CONF_TARGET_TYPE,
    DEFAULT_ACTION_PREFIX,
    DOMAIN,
    ENTRY_MINOR_VERSION,
    ENTRY_VERSION,
    ESPHOME_DOMAIN,
    SUBENTRY_TYPE_TARGET,
    TARGET_TYPE_GENERIC,
    TARGET_TYPES,
)
from .coordinator import LearnOutcome
from .data import RfClonerConfigEntry
from .devices import async_set_target_area, async_target_device_area
from .icons_suggested import suggest_icon
from .models import BridgeStatus, validate_command_name
from .restore import RestoreNotPossible, async_restore, check_restore_target
from .targets import (
    CommandMeta,
    RfTarget,
    assigned_target,
    command_meta,
    meta_for,
    new_target_id,
    options_with,
    targets,
    validate_target_type,
)
from .transport import BridgeTransport

_LOGGER = logging.getLogger(__name__)

# The sentinel the target picker uses for "no target". A config flow cannot offer None as a
# select option, so unassigned is spelled out.
UNASSIGNED = "__unassigned__"


@callback
def _esphome_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every ESPHome config entry, in the order a picker should list them."""
    return sorted(
        hass.config_entries.async_entries(ESPHOME_DOMAIN),
        key=lambda entry: entry.title.lower(),
    )


@callback
def _esphome_entry_selector(hass: HomeAssistant) -> SelectSelector:
    """A picker over the ESPHome nodes that exist, by config entry id.

    This is deliberately not `ConfigEntrySelector`, which is the selector the relationship
    actually calls for. Home Assistant's frontend has no initial value for a `config_entry`
    selector, and `computeInitialHaFormData` throws on a *required* one that carries no default
    - which kills the render of the whole step, leaving a dialog with nothing in it but its
    submit button. It is reached only from a config flow, so the selector works in an action's
    fields and in our reconfigure step (which supplies a default) while failing here.

    The value is still the ESPHome config entry id, so nothing downstream changes: what is
    stored, matched and re-pointed is the entry, never the node's name.
    """
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=entry.entry_id, label=entry.title)
                for entry in _esphome_entries(hass)
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _target_type_selector() -> SelectSelector:
    """A picker over the target types this version knows, translated in the frontend."""
    return SelectSelector(
        SelectSelectorConfig(
            options=list(TARGET_TYPES),
            translation_key="target_type",
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _target_selector(entry: ConfigEntry) -> SelectSelector:
    """A picker over this bridge's targets, plus Unassigned."""
    options = [SelectOptionDict(value=UNASSIGNED, label="Unassigned")]
    options.extend(
        SelectOptionDict(value=target.target_id, label=target.name)
        for target in sorted(targets(entry).values(), key=lambda item: item.name.lower())
    )
    return SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )


def _command_selector(entry: RfClonerConfigEntry, status: BridgeStatus) -> SelectSelector:
    """A picker over the commands the device currently reports, labelled by target."""
    options: list[SelectOptionDict] = []
    for command in sorted(status.commands, key=lambda item: item.name.lower()):
        target = assigned_target(entry, command.command_id)
        where = target.name if target is not None else "Unassigned"
        options.append(
            SelectOptionDict(
                value=str(command.command_id), label=f"{command.name} ({where})"
            )
        )
    return SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )


class RfClonerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Bind a logical bridge to the ESPHome node that carries it."""

    VERSION = ENTRY_VERSION
    MINOR_VERSION = ENTRY_MINOR_VERSION

    def __init__(self) -> None:
        """Start with no replacement under consideration."""
        self._replacement: tuple[str, str, BridgeStatus] | None = None

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """The actions the integration page offers for this bridge.

        Only the first is a subentry type in the ordinary sense. The second is registered here
        because this mapping is the only thing Home Assistant turns into a labelled button on the
        integration page, and learning a command deserves to be one; its flow creates no subentry.
        """
        return {
            SUBENTRY_TYPE_TARGET: TargetSubentryFlow,
            ACTION_TYPE_LEARN_COMMAND: LearnCommandFlow,
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Commands are managed through the options flow."""
        return RfClonerOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an ESPHome node and confirm it is running an rf_cloner bridge."""
        if not _esphome_entries(self.hass):
            # A picker with nothing in it is indistinguishable from a broken form, and there is
            # no useful answer the user could give, so say what is missing instead.
            return self.async_abort(reason="no_esphome_entries")

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
                    vol.Required(CONF_ESPHOME_ENTRY_ID): _esphome_entry_selector(
                        self.hass
                    ),
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
        choices = _esphome_entries(self.hass)
        if not choices:
            return self.async_abort(reason="no_esphome_entries")

        # The node this bridge is bound to may be the thing that went away - which is the whole
        # point of reconfiguring - so it is only offered as the starting point if it still exists.
        current = entry.data.get(CONF_ESPHOME_ENTRY_ID)
        if all(choice.entry_id != current for choice in choices):
            current = vol.UNDEFINED

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
                        CONF_ESPHOME_ENTRY_ID, default=current
                    ): _esphome_entry_selector(self.hass),
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
        """Confirm restoring this bridge's snapshot onto replacement hardware.

        The restore writes RF state only. Targets, assignments and icons are Home Assistant's own
        and are untouched by it, so the restored commands come back under the same targets they
        were organised into.
        """
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


class TargetSubentryFlow(ConfigSubentryFlow):
    """Create an RF target, or change one that already exists.

    Everything here is Home Assistant-side presentation: a name, a type and an area. No step in
    this flow calls the bridge, so creating, renaming or retyping a target never touches a
    waveform, a command id or the registry's revision.
    """

    @property
    def _entry(self) -> RfClonerConfigEntry:
        """The bridge this target belongs to."""
        return self._get_entry()

    def _current_area(self, target: RfTarget | None) -> str | None:
        """Where this target's device actually is, falling back to what was stored.

        The device is the truth. A user who moved it in Home Assistant should open this form and
        see where they put it, not where it started - otherwise the form shows one area, the
        device sits in another, and submitting the form appears to do nothing.
        """
        if target is None:
            return None
        entry = self._entry
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            return target.area_id
        current = async_target_device_area(self.hass, entry, runtime.bridge_id, target)
        return current if current is not None else target.area_id

    def _schema(self, target: RfTarget | None) -> vol.Schema:
        """The target form, pre-filled from `target` when one is being changed."""
        name_field = (
            vol.Required(CONF_NAME)
            if target is None
            else vol.Required(CONF_NAME, default=target.name)
        )
        area = self._current_area(target)
        area_field = (
            vol.Optional(CONF_AREA_ID)
            if area is None
            else vol.Optional(CONF_AREA_ID, default=area)
        )
        return vol.Schema(
            {
                name_field: TextSelector(),
                vol.Required(
                    CONF_TARGET_TYPE,
                    default=TARGET_TYPE_GENERIC if target is None else target.target_type,
                ): _target_type_selector(),
                area_field: AreaSelector(),
            }
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Name a new piece of equipment for this bridge to drive."""
        entry = self._entry
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input[CONF_NAME]).strip()
            if not name:
                errors[CONF_NAME] = "name_empty"
            elif any(
                target.name.casefold() == name.casefold()
                for target in targets(entry).values()
            ):
                errors[CONF_NAME] = "target_name_taken"
            else:
                target_id = new_target_id()
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_TARGET_ID: target_id,
                        CONF_TARGET_TYPE: validate_target_type(
                            user_input.get(CONF_TARGET_TYPE)
                        ),
                        CONF_AREA_ID: user_input.get(CONF_AREA_ID),
                    },
                    unique_id=f"{entry.unique_id}_target_{target_id}",
                )

        return self.async_show_form(
            step_id="user", data_schema=self._schema(None), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Rename an RF target, or change its type or area."""
        entry = self._entry
        subentry = entry.subentries.get(self._reconfigure_subentry_id)
        target = None if subentry is None else RfTarget.from_subentry(subentry)
        if subentry is None or target is None:
            return self.async_abort(reason="unknown_target")

        current_area = self._current_area(target)
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input[CONF_NAME]).strip()
            clash = any(
                other.name.casefold() == name.casefold()
                and other.target_id != target.target_id
                for other in targets(entry).values()
            )
            if not name:
                errors[CONF_NAME] = "name_empty"
            elif clash:
                errors[CONF_NAME] = "target_name_taken"
            else:
                chosen_area = user_input.get(CONF_AREA_ID)
                # Updated directly rather than through async_update_and_abort, which refuses to
                # run while the entry carries the update listener this integration needs.
                self.hass.config_entries.async_update_subentry(
                    entry,
                    subentry,
                    title=name,
                    data={
                        **subentry.data,
                        CONF_TARGET_TYPE: validate_target_type(
                            user_input.get(CONF_TARGET_TYPE)
                        ),
                        CONF_AREA_ID: chosen_area,
                    },
                )
                runtime = getattr(entry, "runtime_data", None)
                if runtime is not None and chosen_area != current_area:
                    # Compared against where the device actually is, not against what was stored
                    # when the target was made. Those differ as soon as the user moves the device
                    # themselves, and comparing against the stored value would then make an
                    # explicit "put it back" selection look like no change at all.
                    #
                    # Submitting the field as the form offered it still moves nothing, which is
                    # what keeps a rename from dragging the device out of the area the user chose.
                    async_set_target_area(
                        self.hass, entry, runtime.bridge_id, target, chosen_area
                    )
                return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure", data_schema=self._schema(target), errors=errors
        )


class LearnCommandSteps:
    """Naming a command, capturing it, and recording where it belongs.

    Mixed into both flows that can start a learn: the options flow's menu, and the `Learn command`
    action on the integration page. Everything that decides what actually happens lives here once
    - the name validation, the reservation that keeps a half-learned command out of sight, the
    capture, the placement and the icon - so the two entrances are the same learn reached two ways
    rather than two implementations of it.

    The host supplies `_entry`. Nothing else differs between them: both write the command's
    organisation to the *entry's* options, which is where a command's target and icon live
    regardless of which flow put them there.
    """

    hass: HomeAssistant

    def __init__(self) -> None:
        """Start with nothing named, chosen or captured."""
        super().__init__()
        self._name: str = ""
        self._icon: str | None = None
        self._target_id: str | None = None
        self._task: asyncio.Task[LearnOutcome] | None = None

    @property
    def _entry(self) -> RfClonerConfigEntry:
        """The bridge being learned onto."""
        raise NotImplementedError

    def _runtime(self):
        """This bridge's runtime data, or None when it is not loaded."""
        return getattr(self._entry, "runtime_data", None)

    async def async_step_learn_command(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name the command, choose where it belongs, then capture it."""
        entry = self._entry
        runtime = self._runtime()
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
                chosen = user_input.get(CONF_TARGET_ID, UNASSIGNED)
                self._target_id = None if chosen == UNASSIGNED else chosen
                icon = str(user_input.get(CONF_ICON, "")).strip()
                self._icon = icon or None
                return await self.async_step_learn()

        return self.async_show_form(
            step_id="learn_command",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): TextSelector(),
                    vol.Optional(
                        CONF_TARGET_ID, default=UNASSIGNED
                    ): _target_selector(entry),
                    vol.Optional(CONF_ICON): IconSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Arm the capture and wait for the device to report how it ended."""
        runtime = self._runtime()
        if runtime is None:
            return self.async_abort(reason="not_loaded")
        if self._task is None:
            # Claimed before the learn is armed, so a poll landing mid-capture cannot
            # materialise the command as unassigned before this flow can place it.
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
    ) -> ConfigFlowResult:
        """Record where the new command belongs, or explain why there is none."""
        entry = self._entry
        runtime = self._runtime()
        assert self._task is not None
        if runtime is None:
            return self.async_abort(reason="not_loaded")

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

        target = targets(entry).get(self._target_id or "")
        icon = self._icon or suggest_icon(
            outcome.name, target.target_type if target else TARGET_TYPE_GENERIC
        )
        self.hass.config_entries.async_update_entry(
            entry,
            options=options_with(
                entry,
                {
                    outcome.command_id: CommandMeta(
                        target_id=target.target_id if target else None, icon=icon
                    )
                },
            ),
        )
        # The command is placed, so the claim that kept it out of sight can go.
        runtime.reconciler.async_release(self._name)
        return self.async_abort(
            reason="learned",
            description_placeholders={
                "name": outcome.name,
                "target": target.name if target else "Unassigned",
            },
        )


class LearnCommandFlow(LearnCommandSteps, ConfigSubentryFlow):
    """The `Learn command` button on the integration page.

    Registered as a subentry type because that mapping is the only thing Home Assistant renders as
    a named action there, and learning is what a user of this integration does most - in 0.2.0 it
    was reachable only through the bridge's configure gear, two screens down, which is where it
    went unfound.

    It creates no subentry. A command cannot be one: its entity has to sit on the device of the
    target it drives, and an entity belongs to exactly one subentry, so a command that was itself
    a subentry could never join a target's device. The flow ends in an abort, and Home Assistant
    adds a subentry only for a create, so nothing named `learn_command` is ever written anywhere.
    A command learned from this button and a command learned from the options menu are the same
    record, with the same id, unique_id, entity and history.
    """

    @property
    def _entry(self) -> RfClonerConfigEntry:
        """The bridge this action was started from."""
        return self._get_entry()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Open on the learn form itself, with no menu in front of it.

        This is the whole point of the button: Home Assistant starts a subentry flow at
        `async_step_user`, so that step *is* the form, and the user lands on Name, RF device and
        Icon in one click. The step then renames itself to `learn_command` for the rest of the
        flow, which is what keeps its strings and the options flow's the same shape.

        Whatever Home Assistant initialised the flow with is discarded rather than submitted: a
        started action has nothing to say about what is being learned.
        """
        return await self.async_step_learn_command()


class RfClonerOptionsFlow(LearnCommandSteps, OptionsFlow):
    """Manage this bridge's learned commands.

    Deliberately a plain OptionsFlow rather than OptionsFlowWithReload: the entry carries an
    update listener, and the reloading variant is not allowed alongside one. Every step here
    writes what it changed and ends with an abort, so nothing overwrites the entry's options
    wholesale on the way out.
    """

    def __init__(self) -> None:
        """Start with nothing chosen."""
        super().__init__()
        self._command_id: int | None = None

    @property
    def _entry(self) -> RfClonerConfigEntry:
        """The bridge being managed."""
        return self.config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the things a user does to commands."""
        if self._runtime() is None:
            return self.async_abort(reason="not_loaded")
        return self.async_show_menu(
            step_id="init", menu_options=["learn_command", "manage_command"]
        )

    # Managing an existing command

    async def async_step_manage_command(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the command to work on."""
        entry = self._entry
        runtime = self._runtime()
        status = runtime.coordinator.data if runtime is not None else None
        if runtime is None or status is None:
            return self.async_abort(reason="not_loaded")
        if not status.commands:
            return self.async_abort(reason="no_commands")

        if user_input is not None:
            self._command_id = int(user_input[CONF_COMMAND_ID])
            return await self.async_step_command()

        return self.async_show_form(
            step_id="manage_command",
            data_schema=vol.Schema(
                {vol.Required(CONF_COMMAND_ID): _command_selector(entry, status)}
            ),
        )

    async def async_step_command(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rename, re-target, re-icon or delete one command.

        The name is the device's, so changing it is an RF mutation and goes to the bridge. The
        target and the icon are Home Assistant's, so changing either writes nothing but options.
        """
        entry = self._entry
        runtime = self._runtime()
        status = runtime.coordinator.data if runtime is not None else None
        if runtime is None or status is None or self._command_id is None:
            return self.async_abort(reason="not_loaded")
        command = status.by_id.get(self._command_id)
        if command is None:
            return self.async_abort(reason="unknown_command")

        meta = meta_for(entry, self._command_id)
        current_target = assigned_target(entry, self._command_id)
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("delete"):
                return await self.async_step_confirm_delete()

            name = str(user_input[CONF_NAME]).strip()
            problem = validate_command_name(name)
            taken_by = status.by_name.get(name)
            if problem is not None:
                errors[CONF_NAME] = problem
            elif taken_by is not None and taken_by.command_id != self._command_id:
                errors[CONF_NAME] = "name_taken"
            else:
                if name != command.name:
                    if not status.accepts_mutations:
                        return self.async_abort(reason="not_writable")
                    try:
                        await runtime.coordinator.async_rename(self._command_id, name)
                    except HomeAssistantError as err:
                        _LOGGER.error(
                            "Could not rename command %s: %s", self._command_id, err
                        )
                        errors["base"] = "rename_failed"

                if not errors:
                    chosen = user_input.get(CONF_TARGET_ID, UNASSIGNED)
                    target_id = None if chosen == UNASSIGNED else chosen
                    icon = str(user_input.get(CONF_ICON, "")).strip() or None
                    self.hass.config_entries.async_update_entry(
                        entry,
                        options=options_with(
                            entry,
                            {
                                self._command_id: CommandMeta(
                                    target_id=target_id, icon=icon
                                )
                            },
                        ),
                    )
                    return self.async_abort(reason="command_saved")

        suggested_icon = meta.icon or suggest_icon(
            command.name,
            current_target.target_type if current_target else TARGET_TYPE_GENERIC,
        )
        return self.async_show_form(
            step_id="command",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=command.name): TextSelector(),
                    vol.Optional(
                        CONF_TARGET_ID,
                        default=(
                            current_target.target_id if current_target else UNASSIGNED
                        ),
                    ): _target_selector(entry),
                    vol.Optional(CONF_ICON, default=suggested_icon): IconSelector(),
                    vol.Optional("delete", default=False): bool,
                }
            ),
            errors=errors,
            description_placeholders={"name": command.name},
        )

    async def async_step_confirm_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete one command from the bridge. This is the destructive path, and it is explicit."""
        entry = self._entry
        runtime = self._runtime()
        status = runtime.coordinator.data if runtime is not None else None
        if runtime is None or status is None or self._command_id is None:
            return self.async_abort(reason="not_loaded")
        command = status.by_id.get(self._command_id)
        if command is None:
            return self.async_abort(reason="unknown_command")

        if user_input is None:
            return self.async_show_form(
                step_id="confirm_delete",
                data_schema=vol.Schema({}),
                description_placeholders={"name": command.name},
            )

        if not status.accepts_mutations:
            return self.async_abort(reason="not_writable")
        try:
            await runtime.coordinator.async_delete(self._command_id, command.name)
        except HomeAssistantError as err:
            _LOGGER.error("Could not delete command %s: %s", self._command_id, err)
            return self.async_abort(
                reason="delete_failed", description_placeholders={"detail": str(err)}
            )
        # The reconcile that follows the delete drops the command's metadata; doing it here as
        # well would only race with it.
        return self.async_abort(
            reason="command_deleted", description_placeholders={"name": command.name}
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
