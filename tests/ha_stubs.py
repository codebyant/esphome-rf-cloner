"""Just enough of Home Assistant to import the integration's pure logic on the host.

The modules these tests cover - restore orchestration and reconciliation - contain no Home
Assistant behaviour of their own; they only borrow its types. Stubbing those types keeps the tests
runnable with a plain Python interpreter, the same way the C++ tests run without ESPHome.

Deliberately minimal: this is not a Home Assistant simulator. Anything that needs real config
entry, entity or device registry behaviour belongs in a live test against a running instance.
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import types
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "rf_cloner"

#: Name the component is loaded under, avoiding its real package __init__.
PACKAGE = "rf_cloner_under_test"


class HomeAssistantError(Exception):
    """Stand-in for Home Assistant's base error."""


class ServiceNotFound(HomeAssistantError):
    """Stand-in for a missing action."""


class ConfigEntryState:
    """Only the state the reconciler checks."""

    LOADED = "loaded"
    NOT_LOADED = "not_loaded"


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def install() -> types.ModuleType:
    """Stub Home Assistant, then load the component under `PACKAGE` and return it."""
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]

    class Store:
        """In-memory stand-in for Home Assistant's persistent Store."""

        def __init__(self, *args, **kwargs) -> None:
            self.data = None

        async def async_load(self):
            return self.data

        async def async_save(self, data) -> None:
            self.data = data

        async def async_remove(self) -> None:
            self.data = None

    class DataUpdateCoordinator:
        """Stand-in base; the tests drive coordinators directly instead."""

        def __init__(self, *args, **kwargs) -> None:
            self.data = None

        def __class_getitem__(cls, item):
            # The real class is generic over its data type.
            return cls

    modules = {
        "homeassistant": _module("homeassistant", __path__=[]),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions",
            HomeAssistantError=HomeAssistantError,
            ServiceNotFound=ServiceNotFound,
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object, callback=lambda fn: fn
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries",
            ConfigEntry=object,
            ConfigEntryState=ConfigEntryState,
            ConfigSubentry=_ConfigSubentry,
        ),
        "homeassistant.helpers": _module("homeassistant.helpers", __path__=[]),
        "homeassistant.helpers.entity_registry": _module(
            "homeassistant.helpers.entity_registry",
            async_get=lambda hass: hass.entity_registry,
            async_entries_for_config_entry=(
                lambda registry, entry_id: registry.entries_for(entry_id)
            ),
        ),
        "homeassistant.helpers.storage": _module("homeassistant.helpers.storage", Store=Store),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=DataUpdateCoordinator,
            UpdateFailed=HomeAssistantError,
        ),
        "homeassistant.util": _module("homeassistant.util", __path__=[]),
        "homeassistant.util.dt": _module(
            "homeassistant.util.dt",
            utcnow=lambda: dt.datetime.now(dt.UTC),
            parse_datetime=dt.datetime.fromisoformat,
        ),
    }
    modules["homeassistant.helpers"].entity_registry = modules[
        "homeassistant.helpers.entity_registry"
    ]
    modules["homeassistant.util"].dt = modules["homeassistant.util.dt"]
    sys.modules.update(modules)

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(COMPONENT)]
    sys.modules[PACKAGE] = package
    return package


def load(name: str) -> types.ModuleType:
    """Import one of the component's modules, stubbing Home Assistant first."""
    install()
    return importlib.import_module(f"{PACKAGE}.{name}")


class _ConfigSubentry:
    """Stand-in for a config subentry, with the fields the reconciler reads and writes."""

    _counter = 0

    def __init__(self, *, data, subentry_type, title, unique_id, subentry_id=None) -> None:
        type(self)._counter += 1
        self.data = dict(data)
        self.subentry_type = subentry_type
        self.title = title
        self.unique_id = unique_id
        self.subentry_id = subentry_id or f"sub{type(self)._counter:04d}"


class _RegistryEntry:
    """Stand-in for an entity registry record, with the fields the reconciler reads."""

    def __init__(self, entity_id: str, unique_id: str, config_entry_id: str) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id


class FakeEntityRegistry:
    """Just enough entity registry to observe which records the reconciler removes."""

    def __init__(self, recorder: "Recorder") -> None:
        self.recorder = recorder
        self.records: dict[str, _RegistryEntry] = {}

    def add(self, entity_id: str, unique_id: str, config_entry_id: str) -> None:
        """Seed a record, as a loaded platform would have."""
        self.records[entity_id] = _RegistryEntry(entity_id, unique_id, config_entry_id)

    def entries_for(self, config_entry_id: str) -> list[_RegistryEntry]:
        return [
            record
            for record in self.records.values()
            if record.config_entry_id == config_entry_id
        ]

    def async_remove(self, entity_id: str) -> None:
        self.recorder.calls.append(("remove_entity", entity_id))
        self.records.pop(entity_id, None)


class Recorder:
    """Records every mutation an object under test attempts, so a test can assert on silence."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __bool__(self) -> bool:
        return bool(self.calls)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]
