import esphome.codegen as cg
from esphome.components import sensor
import esphome.config_validation as cv
from esphome.const import (
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
    UNIT_BYTES,
)
from esphome.types import ConfigType

from . import CONF_RF_CLONER_ID, RfCloner

DEPENDENCIES = ["rf_cloner"]

CONF_COMMAND_COUNT = "command_count"
CONF_STORAGE_USED = "storage_used"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_RF_CLONER_ID): cv.use_id(RfCloner),
        cv.Optional(CONF_COMMAND_COUNT): sensor.sensor_schema(
            icon="mdi:format-list-numbered",
            accuracy_decimals=0,
            state_class=STATE_CLASS_MEASUREMENT,
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
        cv.Optional(CONF_STORAGE_USED): sensor.sensor_schema(
            unit_of_measurement=UNIT_BYTES,
            icon="mdi:database",
            accuracy_decimals=0,
            state_class=STATE_CLASS_MEASUREMENT,
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
    }
)


async def to_code(config: ConfigType) -> None:
    parent = await cg.get_variable(config[CONF_RF_CLONER_ID])

    if count_config := config.get(CONF_COMMAND_COUNT):
        sens = await sensor.new_sensor(count_config)
        cg.add(parent.set_command_count_sensor(sens))

    if used_config := config.get(CONF_STORAGE_USED):
        sens = await sensor.new_sensor(used_config)
        cg.add(parent.set_storage_used_sensor(sens))
