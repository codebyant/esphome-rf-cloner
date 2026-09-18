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

# Matches packages/api-actions.yaml's rf_action_prefix default.
DEFAULT_ACTION_PREFIX: Final = "rf_"

SUBENTRY_TYPE_COMMAND: Final = "command"

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
