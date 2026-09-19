"""Constants for the RF Cloner Bridge integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "rf_cloner"

ESPHOME_DOMAIN: Final = "esphome"
# The key ESPHome keeps in sync with the node's actual name on every successful connect.
ESPHOME_CONF_DEVICE_NAME: Final = "device_name"

CONF_ESPHOME_ENTRY_ID: Final = "esphome_entry_id"
CONF_ACTION_PREFIX: Final = "action_prefix"
CONF_BRIDGE_ID: Final = "bridge_id"
CONF_COMMAND_ID: Final = "command_id"
CONF_NAME: Final = "name"

# RF targets: the Home Assistant-side idea of the equipment a command actually drives.
CONF_TARGET_ID: Final = "target_id"
CONF_TARGET_TYPE: Final = "target_type"
CONF_AREA_ID: Final = "area_id"
CONF_ICON: Final = "icon"

# Matches packages/api-actions.yaml's rf_action_prefix default.
DEFAULT_ACTION_PREFIX: Final = "rf_"

# Subentry types. `command` is the 0.1 shape, kept only so a migration can recognise and retire
# it; nothing creates one any more.
SUBENTRY_TYPE_COMMAND: Final = "command"
SUBENTRY_TYPE_TARGET: Final = "target"

# The config entry's schema version. 1 is 0.1's flat command subentries; 2 is RF targets.
ENTRY_VERSION: Final = 2
ENTRY_MINOR_VERSION: Final = 1

# Where a command's Home Assistant-side organisation lives: entry.options, keyed by command id as
# a string, because options round-trip through JSON. Deliberately not in the subentries, so a
# target's removal cannot take a command's icon with it.
OPT_COMMANDS: Final = "commands"

UPDATE_INTERVAL: Final = timedelta(seconds=30)

# A learn is armed for learn_timeout_ms on the device (15 s by default). Poll a little longer so
# the flow reports the device's own timeout rather than racing it.
LEARN_POLL_INTERVAL: Final = 1.0
LEARN_TIMEOUT: Final = 30.0

# The device's revision domain, mirroring esphome::rf_cloner::REVISION_TERMINAL and REVISION_MAX.
#
# A revision crosses the ESPHome action boundary as a signed 32-bit integer, so the device bounds
# its own revisions by that range: every committed revision is therefore one this integration can
# send back during a replacement restore. REVISION_TERMINAL is reserved and never persisted.
REVISION_TERMINAL: Final = 2**31 - 1
REVISION_MAX: Final = REVISION_TERMINAL - 1

# The device's own name rules, mirrored so a flow can reject a name without a round trip.
NAME_MAX_LENGTH: Final = 23
NAME_ALLOWED_EXTRA: Final = "_-."

MANUFACTURER: Final = "esphome-rf-cloner"
MODEL: Final = "RF Cloner Bridge"

# What a target device is modelled as. Presentation only: a target is a group of buttons, and
# none of these turn it into a stateful entity domain.
TARGET_TYPE_GENERIC: Final = "generic"
TARGET_TYPES: Final = (
    TARGET_TYPE_GENERIC,
    "fan",
    "gate",
    "light",
    "cover",
    "tv",
    "air_conditioner",
    "other",
)

# The model string each target type gives its Home Assistant device, so the device page says what
# the target is rather than repeating the integration's name.
TARGET_TYPE_MODELS: Final[dict[str, str]] = {
    TARGET_TYPE_GENERIC: "RF target",
    "fan": "RF fan",
    "gate": "RF gate",
    "light": "RF light",
    "cover": "RF cover",
    "tv": "RF TV",
    "air_conditioner": "RF air conditioner",
    "other": "RF target",
}

# The icon a command falls back to when nothing better is known.
DEFAULT_COMMAND_ICON: Final = "mdi:remote"
