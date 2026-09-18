"""Runtime-learnable, persistent RF command registry for ESPHome.

Wraps a remote_receiver (capture) and a remote_transmitter (replay) with a named command store
backed by ESPHome preferences. See docs/architecture.md.
"""

import logging

from esphome import automation
import esphome.codegen as cg
from esphome.components import remote_base
import esphome.config_validation as cv
from esphome.const import (
    CONF_CARRIER_DUTY_PERCENT,
    CONF_FREQUENCY,
    CONF_ID,
    CONF_NAME,
    CONF_TIMEOUT,
    CONF_TOLERANCE,
    CONF_TRIGGER_ID,
)
import esphome.final_validate as fv
from esphome.types import ConfigType

CODEOWNERS = ["@seijinmark"]
# remote_base has no CONFIG_SCHEMA and cannot be listed as a dependency; these two auto-load it.
DEPENDENCIES = ["remote_receiver", "remote_transmitter"]
MULTI_CONF = True

_LOGGER = logging.getLogger(__name__)

rf_cloner_ns = cg.esphome_ns.namespace("rf_cloner")
RfCloner = rf_cloner_ns.class_("RfCloner", cg.Component, remote_base.RemoteReceiverListener)

LearnAction = rf_cloner_ns.class_("LearnAction", automation.Action, cg.Parented.template(RfCloner))
CancelAction = rf_cloner_ns.class_(
    "CancelAction", automation.Action, cg.Parented.template(RfCloner)
)
SendAction = rf_cloner_ns.class_("SendAction", automation.Action, cg.Parented.template(RfCloner))
SendByIdAction = rf_cloner_ns.class_(
    "SendByIdAction", automation.Action, cg.Parented.template(RfCloner)
)
RenameAction = rf_cloner_ns.class_(
    "RenameAction", automation.Action, cg.Parented.template(RfCloner)
)
DeleteAction = rf_cloner_ns.class_(
    "DeleteAction", automation.Action, cg.Parented.template(RfCloner)
)
DeleteByIdAction = rf_cloner_ns.class_(
    "DeleteByIdAction", automation.Action, cg.Parented.template(RfCloner)
)
ClearAllAction = rf_cloner_ns.class_(
    "ClearAllAction", automation.Action, cg.Parented.template(RfCloner)
)
FactoryResetAction = rf_cloner_ns.class_(
    "FactoryResetAction", automation.Action, cg.Parented.template(RfCloner)
)

LearnStartedTrigger = rf_cloner_ns.class_("LearnStartedTrigger", automation.Trigger.template(cg.std_string))
LearnSuccessTrigger = rf_cloner_ns.class_("LearnSuccessTrigger", automation.Trigger.template(cg.std_string))
LearnFailedTrigger = rf_cloner_ns.class_("LearnFailedTrigger", automation.Trigger.template(cg.std_string))
SendTrigger = rf_cloner_ns.class_("SendTrigger", automation.Trigger.template(cg.std_string))

CONF_RF_CLONER_ID = "rf_cloner_id"
CONF_MAX_COMMANDS = "max_commands"
CONF_MAX_PULSES = "max_pulses"
CONF_MIN_PULSES = "min_pulses"
CONF_MIN_PULSE_LENGTH = "min_pulse_length"
CONF_MAX_PULSE_LENGTH = "max_pulse_length"
CONF_MAX_STORAGE_BYTES = "max_storage_bytes"
CONF_MIN_REPEATS = "min_repeats"
CONF_FRAME_GAP_MIN = "frame_gap_min"
CONF_SETTLE_TIME = "settle_time"
CONF_LEARN_TIMEOUT = "learn_timeout"
CONF_DEFAULT_REPEAT_TIMES = "default_repeat_times"
CONF_DEFAULT_GAP = "default_gap"
CONF_MIN_GAP = "min_gap"
CONF_MAX_GAP = "max_gap"
CONF_COMMAND_ID = "command_id"
CONF_REPEAT_TIMES = "repeat_times"
CONF_GAP = "gap"
CONF_ON_LEARN_STARTED = "on_learn_started"
CONF_ON_LEARN_SUCCESS = "on_learn_success"
CONF_ON_LEARN_FAILED = "on_learn_failed"
CONF_ON_SEND = "on_send"


def _fnv1a_32(text: str) -> int:
    """Same hash the C++ side uses for its base keys, so the two stay conceptually in step."""
    value = 0x811C9DC5
    for byte in text.encode():
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value


# Mirrors the on-flash sizes in command_store.cpp: StoredHeader, StoredMeta, StoredSlot and the
# index CRC. CommandStore::index_overhead_bytes() is the authority; these must agree with it.
_HEADER_BYTES = 10
_META_BYTES = 28
_SLOT_BYTES = 44
_CRC_BYTES = 2


def _validate_budget(config):
    """Warn loudly at validation time rather than at the first failed learn."""
    index_bytes = (
        _HEADER_BYTES + _META_BYTES + config[CONF_MAX_COMMANDS] * _SLOT_BYTES + _CRC_BYTES
    )
    worst_case = index_bytes + config[CONF_MAX_COMMANDS] * config[CONF_MAX_PULSES] * 4
    if index_bytes > config[CONF_MAX_STORAGE_BYTES]:
        raise cv.Invalid(
            f"max_storage_bytes ({config[CONF_MAX_STORAGE_BYTES]}) is smaller than the "
            f"{index_bytes} bytes the command index alone needs for "
            f"max_commands: {config[CONF_MAX_COMMANDS]}"
        )
    if config[CONF_MIN_PULSES] > config[CONF_MAX_PULSES]:
        raise cv.Invalid("min_pulses must not exceed max_pulses")
    if config[CONF_MIN_PULSE_LENGTH] >= config[CONF_MAX_PULSE_LENGTH]:
        raise cv.Invalid("min_pulse_length must be shorter than max_pulse_length")
    if config[CONF_MIN_GAP] >= config[CONF_MAX_GAP]:
        raise cv.Invalid("min_gap must be shorter than max_gap")
    if config[CONF_FRAME_GAP_MIN] > config[CONF_MAX_GAP]:
        # Split gaps are always at least frame_gap_min, so a threshold above max_gap would make
        # every one of them fail the plausibility check.
        raise cv.Invalid("frame_gap_min must not exceed max_gap")
    if not config[CONF_MIN_GAP] <= config[CONF_DEFAULT_GAP] <= config[CONF_MAX_GAP]:
        raise cv.Invalid("default_gap must fall between min_gap and max_gap")
    if worst_case > config[CONF_MAX_STORAGE_BYTES]:
        # Not fatal: put() refuses with budget_exceeded before flash fills. Surfaced at build
        # time so the ceiling is known in advance.
        _LOGGER.info(
            "rf_cloner: max_commands x max_pulses could need %d bytes, above max_storage_bytes "
            "(%d). Learning will be refused once the budget is reached.",
            worst_case,
            config[CONF_MAX_STORAGE_BYTES],
        )
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(RfCloner),
            # Storage
            cv.Optional(CONF_MAX_COMMANDS, default=16): cv.int_range(min=1, max=255),
            cv.Optional(CONF_MAX_PULSES, default=128): cv.int_range(min=4, max=2048),
            cv.Optional(CONF_MAX_STORAGE_BYTES, default=12288): cv.int_range(min=64, max=65535),
            # Capture validation
            cv.Optional(CONF_MIN_PULSES, default=8): cv.int_range(min=2, max=2048),
            cv.Optional(CONF_MIN_PULSE_LENGTH, default="100us"): cv.positive_time_period_microseconds,
            cv.Optional(CONF_MAX_PULSE_LENGTH, default="100ms"): cv.positive_time_period_microseconds,
            cv.Optional(CONF_TOLERANCE, default="25%"): cv.percentage_int,
            cv.Optional(CONF_MIN_REPEATS, default=3): cv.int_range(min=2, max=32),
            # Above the longest space inside a frame, below the gap between frames.
            cv.Optional(CONF_FRAME_GAP_MIN, default="3ms"): cv.positive_time_period_microseconds,
            cv.Optional(CONF_SETTLE_TIME, default="800ms"): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_LEARN_TIMEOUT, default="15s"): cv.positive_time_period_milliseconds,
            # Replay defaults
            cv.Optional(CONF_DEFAULT_REPEAT_TIMES, default=20): cv.int_range(min=1, max=1000),
            cv.Optional(CONF_DEFAULT_GAP, default="10ms"): cv.positive_time_period_microseconds,
            cv.Optional(CONF_MIN_GAP, default="1ms"): cv.positive_time_period_microseconds,
            cv.Optional(CONF_MAX_GAP, default="200ms"): cv.positive_time_period_microseconds,
            # Metadata recorded with every capture
            cv.Optional(CONF_FREQUENCY, default="433.92MHz"): cv.frequency,
            # Triggers
            cv.Optional(CONF_ON_LEARN_STARTED): automation.validate_automation(
                {cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(LearnStartedTrigger)}
            ),
            cv.Optional(CONF_ON_LEARN_SUCCESS): automation.validate_automation(
                {cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(LearnSuccessTrigger)}
            ),
            cv.Optional(CONF_ON_LEARN_FAILED): automation.validate_automation(
                {cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(LearnFailedTrigger)}
            ),
            cv.Optional(CONF_ON_SEND): automation.validate_automation(
                {cv.GenerateID(CONF_TRIGGER_ID): cv.declare_id(SendTrigger)}
            ),
        }
    )
    .extend(remote_base.REMOTE_LISTENER_SCHEMA)
    .extend(remote_base.REMOTE_TRANSMITTABLE_SCHEMA)
    .extend(cv.COMPONENT_SCHEMA),
    _validate_budget,
)


def _final_validate(config: ConfigType) -> None:
    """RF hardware does its own modulation; an IR carrier duty cycle would corrupt the waveform."""
    full_config = fv.full_config.get()
    transmitter_id = config[remote_base.CONF_TRANSMITTER_ID]
    transmitter_path = full_config.get_path_for_id(transmitter_id)[:-1]
    transmitter_config = full_config.get_config_for_path(transmitter_path)

    duty_percent = transmitter_config.get(CONF_CARRIER_DUTY_PERCENT)
    if duty_percent is not None and duty_percent != 100:
        raise cv.Invalid(
            f"Transmitter '{transmitter_id}' must have '{CONF_CARRIER_DUTY_PERCENT}' set to 100% "
            "for RF transmission. The CC1101 handles modulation itself; applying a carrier duty "
            "cycle would corrupt the replayed waveform"
        )


FINAL_VALIDATE_SCHEMA = _final_validate


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    # Listener slots are counted during codegen; registering from C++ alone does not compile.
    await remote_base.register_listener(var, config)
    await remote_base.register_transmittable(var, config)

    cg.add(var.set_max_commands(config[CONF_MAX_COMMANDS]))
    cg.add(var.set_max_pulses(config[CONF_MAX_PULSES]))
    cg.add(var.set_max_storage_bytes(config[CONF_MAX_STORAGE_BYTES]))

    cg.add(var.set_min_pulses(config[CONF_MIN_PULSES]))
    cg.add(var.set_min_pulse_us(config[CONF_MIN_PULSE_LENGTH]))
    cg.add(var.set_max_pulse_us(config[CONF_MAX_PULSE_LENGTH]))
    cg.add(var.set_tolerance_percent(config[CONF_TOLERANCE]))
    cg.add(var.set_min_repeats(config[CONF_MIN_REPEATS]))
    cg.add(var.set_frame_gap_min_us(config[CONF_FRAME_GAP_MIN]))
    cg.add(var.set_settle_ms(config[CONF_SETTLE_TIME]))
    cg.add(var.set_learn_timeout_ms(config[CONF_LEARN_TIMEOUT]))

    cg.add(var.set_default_repeat_times(config[CONF_DEFAULT_REPEAT_TIMES]))
    cg.add(var.set_default_gap_us(config[CONF_DEFAULT_GAP]))
    cg.add(var.set_min_gap_us(config[CONF_MIN_GAP]))
    cg.add(var.set_max_gap_us(config[CONF_MAX_GAP]))
    cg.add(var.set_frequency_hz(int(config[CONF_FREQUENCY])))
    # Instances on one device share an NVS namespace; salt the keys so they cannot collide.
    cg.add(var.set_key_salt(_fnv1a_32(str(config[CONF_ID]))))

    for conf in config.get(CONF_ON_LEARN_STARTED, []):
        trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
        await automation.build_automation(trigger, [(cg.std_string, "x")], conf)
    for conf in config.get(CONF_ON_LEARN_SUCCESS, []):
        trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
        await automation.build_automation(trigger, [(cg.std_string, "x")], conf)
    for conf in config.get(CONF_ON_LEARN_FAILED, []):
        trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
        await automation.build_automation(trigger, [(cg.std_string, "x")], conf)
    for conf in config.get(CONF_ON_SEND, []):
        trigger = cg.new_Pvariable(conf[CONF_TRIGGER_ID], var)
        await automation.build_automation(trigger, [(cg.std_string, "x")], conf)


RF_CLONER_ACTION_SCHEMA = cv.Schema({cv.GenerateID(): cv.use_id(RfCloner)})

NAMED_ACTION_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(RfCloner),
        cv.Required(CONF_NAME): cv.templatable(cv.string_strict),
    }
)


@automation.register_action(
    "rf_cloner.learn",
    LearnAction,
    NAMED_ACTION_SCHEMA.extend(
        {cv.Optional(CONF_TIMEOUT): cv.templatable(cv.positive_time_period_milliseconds)}
    ),
    synchronous=True,
)
async def rf_cloner_learn_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    cg.add(var.set_name(await cg.templatable(config[CONF_NAME], args, cg.std_string)))
    if CONF_TIMEOUT in config:
        cg.add(var.set_timeout(await cg.templatable(config[CONF_TIMEOUT], args, cg.uint32)))
    return var


@automation.register_action(
    "rf_cloner.send",
    SendAction,
    NAMED_ACTION_SCHEMA.extend(
        {
            cv.Optional(CONF_REPEAT_TIMES): cv.templatable(cv.int_range(min=1, max=1000)),
            cv.Optional(CONF_GAP): cv.templatable(cv.positive_time_period_microseconds),
        }
    ),
    synchronous=True,
)
async def rf_cloner_send_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    cg.add(var.set_name(await cg.templatable(config[CONF_NAME], args, cg.std_string)))
    if CONF_REPEAT_TIMES in config:
        cg.add(var.set_repeat_times(await cg.templatable(config[CONF_REPEAT_TIMES], args, cg.uint16)))
    if CONF_GAP in config:
        cg.add(var.set_gap(await cg.templatable(config[CONF_GAP], args, cg.uint32)))
    return var


ID_ACTION_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(RfCloner),
        # Command ids are uint32 and never reused; 0 is reserved for an empty slot.
        cv.Required(CONF_COMMAND_ID): cv.templatable(cv.int_range(min=1, max=4294967294)),
    }
)


@automation.register_action(
    "rf_cloner.send_id",
    SendByIdAction,
    ID_ACTION_SCHEMA.extend(
        {
            cv.Optional(CONF_REPEAT_TIMES): cv.templatable(cv.int_range(min=1, max=1000)),
            cv.Optional(CONF_GAP): cv.templatable(cv.positive_time_period_microseconds),
        }
    ),
    synchronous=True,
)
async def rf_cloner_send_id_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    cg.add(var.set_command_id(await cg.templatable(config[CONF_COMMAND_ID], args, cg.uint32)))
    if CONF_REPEAT_TIMES in config:
        cg.add(var.set_repeat_times(await cg.templatable(config[CONF_REPEAT_TIMES], args, cg.uint16)))
    if CONF_GAP in config:
        cg.add(var.set_gap(await cg.templatable(config[CONF_GAP], args, cg.uint32)))
    return var


@automation.register_action(
    "rf_cloner.rename",
    RenameAction,
    ID_ACTION_SCHEMA.extend({cv.Required(CONF_NAME): cv.templatable(cv.string_strict)}),
    synchronous=True,
)
async def rf_cloner_rename_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    cg.add(var.set_command_id(await cg.templatable(config[CONF_COMMAND_ID], args, cg.uint32)))
    cg.add(var.set_name(await cg.templatable(config[CONF_NAME], args, cg.std_string)))
    return var


@automation.register_action(
    "rf_cloner.delete", DeleteAction, NAMED_ACTION_SCHEMA, synchronous=True
)
async def rf_cloner_delete_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    cg.add(var.set_name(await cg.templatable(config[CONF_NAME], args, cg.std_string)))
    return var


@automation.register_action(
    "rf_cloner.delete_id", DeleteByIdAction, ID_ACTION_SCHEMA, synchronous=True
)
async def rf_cloner_delete_id_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    cg.add(var.set_command_id(await cg.templatable(config[CONF_COMMAND_ID], args, cg.uint32)))
    return var


@automation.register_action(
    "rf_cloner.cancel",
    CancelAction,
    automation.maybe_simple_id(RF_CLONER_ACTION_SCHEMA),
    synchronous=True,
)
@automation.register_action(
    "rf_cloner.clear_all",
    ClearAllAction,
    automation.maybe_simple_id(RF_CLONER_ACTION_SCHEMA),
    synchronous=True,
)
@automation.register_action(
    "rf_cloner.factory_reset",
    FactoryResetAction,
    automation.maybe_simple_id(RF_CLONER_ACTION_SCHEMA),
    synchronous=True,
)
async def rf_cloner_simple_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    return var
