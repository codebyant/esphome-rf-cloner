import esphome.codegen as cg
from esphome.components import text_sensor
import esphome.config_validation as cv
from esphome.const import CONF_STATE, ENTITY_CATEGORY_DIAGNOSTIC
from esphome.types import ConfigType

from . import CONF_RF_CLONER_ID, RfCloner

DEPENDENCIES = ["rf_cloner"]

CONF_LAST_RESULT = "last_result"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_RF_CLONER_ID): cv.use_id(RfCloner),
        cv.Optional(CONF_STATE): text_sensor.text_sensor_schema(
            icon="mdi:radio-tower",
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
        cv.Optional(CONF_LAST_RESULT): text_sensor.text_sensor_schema(
            icon="mdi:information-outline",
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
    }
)


async def to_code(config: ConfigType) -> None:
    parent = await cg.get_variable(config[CONF_RF_CLONER_ID])

    if state_config := config.get(CONF_STATE):
        sens = await text_sensor.new_text_sensor(state_config)
        cg.add(parent.set_state_text_sensor(sens))

    if last_result_config := config.get(CONF_LAST_RESULT):
        sens = await text_sensor.new_text_sensor(last_result_config)
        cg.add(parent.set_last_result_text_sensor(sens))
