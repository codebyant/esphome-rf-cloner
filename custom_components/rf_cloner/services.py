"""Actions for the cases the UI does not cover.

Learning and cancelling are here so automations and scripts can drive a capture; the subentry flow
remains the way a person does it. Exporting the snapshot is here because a replica that can only
be restored through this integration is not much of a backup.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import CONF_NAME, DOMAIN
from .data import RfClonerConfigEntry
from .models import validate_command_name

ATTR_CONFIG_ENTRY_ID = "config_entry_id"

SERVICE_LEARN = "learn"
SERVICE_CANCEL_LEARN = "cancel_learn"
SERVICE_EXPORT_SNAPSHOT = "export_snapshot"

_ENTRY_SCHEMA = vol.Schema({vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string})
_LEARN_SCHEMA = _ENTRY_SCHEMA.extend({vol.Required(CONF_NAME): cv.string})


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's actions."""
    hass.services.async_register(
        DOMAIN, SERVICE_LEARN, _async_learn, schema=_LEARN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_LEARN, _async_cancel_learn, schema=_ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_SNAPSHOT,
        _async_export_snapshot,
        schema=_ENTRY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def _resolve_entry(call: ServiceCall) -> RfClonerConfigEntry:
    """Find the loaded bridge an action was aimed at."""
    entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    entry = call.hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_entry",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry


async def _async_learn(call: ServiceCall) -> None:
    """Capture a command under a name, and wait for the outcome."""
    entry = _resolve_entry(call)
    name = str(call.data[CONF_NAME]).strip()
    problem = validate_command_name(name)
    if problem is not None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=problem,
            translation_placeholders={"name": name},
        )
    outcome = await entry.runtime_data.coordinator.async_learn(name)
    if not outcome.succeeded:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="learn_failed",
            translation_placeholders={"name": name, "detail": outcome.detail},
        )


async def _async_cancel_learn(call: ServiceCall) -> None:
    """Disarm a capture in progress."""
    entry = _resolve_entry(call)
    await entry.runtime_data.coordinator.async_cancel()


async def _async_export_snapshot(call: ServiceCall) -> ServiceResponse:
    """Return the current replica, in the bridge's own portable format."""
    entry = _resolve_entry(call)
    snapshot = entry.runtime_data.snapshots.snapshot
    if snapshot is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_snapshot",
            translation_placeholders={"title": entry.title},
        )
    payload: dict[str, Any] = snapshot.as_stored()
    return payload
