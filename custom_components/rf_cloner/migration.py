"""Migration of a config entry from the 0.1 command subentries to 0.2 RF targets.

In 0.1 every learned command was a config subentry and its button entity belonged to that
subentry. In 0.2 a subentry is an RF target - a piece of equipment - and commands are organised
by metadata in the entry's options instead. The 0.1 subentries therefore have to go.

Removing a subentry is destructive: Home Assistant removes the devices and the entity registry
records that belong to it, taking their entity ids, areas, icons and history with them. So the
order below is load-bearing and is what the migration is really about:

1. detach every entity from the subentry that is about to be removed,
2. only then remove the subentry.

Done that way the removal finds nothing attached and deletes nothing. `tests/ha/test_migration.py`
holds both halves of that, including the counterfactual, so the ordering cannot be quietly
reversed later.

Nothing here touches the device: a command keeps its id, its name and its waveform, and the
bridge's revision does not move. An upgrade is a Home Assistant-side rearrangement and nothing
else.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import ENTRY_MINOR_VERSION, ENTRY_VERSION, SUBENTRY_TYPE_COMMAND

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring `entry` up to the current schema version.

    Idempotent by construction: each step is expressed as "make this true", not "apply this
    change", so running it again - after a restart, a reload, or an interrupted first attempt -
    finds nothing left to do.
    """
    if entry.version > ENTRY_VERSION:
        # A downgrade. The 0.2 shape means nothing to 0.1, and guessing would be worse than
        # refusing, so the entry is left alone and reported rather than rewritten.
        _LOGGER.error(
            "Config entry %s was written by a newer version of this integration"
            " (schema %s.%s, this is %s.%s); not migrating it backwards",
            entry.title,
            entry.version,
            entry.minor_version,
            ENTRY_VERSION,
            ENTRY_MINOR_VERSION,
        )
        return False

    if entry.version < 2:
        _async_migrate_to_targets(hass, entry)

    hass.config_entries.async_update_entry(
        entry, version=ENTRY_VERSION, minor_version=ENTRY_MINOR_VERSION
    )
    return True


def _async_migrate_to_targets(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Retire 0.1's command subentries, leaving every command present and unassigned.

    The entities are not recreated, renamed or re-keyed. Each one keeps the registry record it
    already had - the same entity id, unique id, area, icon and user customisations - and simply
    stops belonging to a subentry, which is exactly what an unassigned 0.2 command looks like.
    """
    registry = er.async_get(hass)
    command_subentry_ids = [
        subentry.subentry_id
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_COMMAND
    ]
    if not command_subentry_ids:
        return

    retiring = set(command_subentry_ids)
    detached = 0
    for record in er.async_entries_for_config_entry(registry, entry.entry_id):
        if record.config_subentry_id not in retiring:
            continue
        registry.async_update_entity(
            record.entity_id, config_subentry_id=None, device_id=None
        )
        detached += 1

    # Only now, with nothing left attached, is removing the subentry a bookkeeping change rather
    # than a deletion.
    for subentry_id in command_subentry_ids:
        hass.config_entries.async_remove_subentry(entry, subentry_id)

    _LOGGER.info(
        "Migrated %s to RF targets: retired %s command subentry/subentries and left %s"
        " command entity/entities unassigned",
        entry.title,
        len(command_subentry_ids),
        detached,
    )
