"""A stand-in bridge, exposed the way a real one is: as ESPHome actions.

The integration never opens a connection of its own - every call goes through the actions an
ESPHome config entry registers - so a fake bridge only has to register those same actions. That
keeps the tests exercising the real transport, the real coordinator and the real reconciler,
rather than a seam cut underneath them.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

ESPHOME_DOMAIN = "esphome"
NODE_NAME = "rf-bridge"
# Locally administered, and deliberately synthetic: nothing here should carry the address of
# a real board into a public repository.
NODE_MAC = "02:00:00:00:00:01"
BRIDGE_ID = "b7397c870440b9228377fbdb4ed95624"

# One real capture, shortened. The waveform's content does not matter to any of these tests; that
# it round-trips unchanged does.
TIMINGS = (350, 700, 350, 700, 700, 350, 350, 700)


class FakeBridge:
    """An in-memory RF registry behind the actions the integration calls."""

    def __init__(self, hass: HomeAssistant, bridge_id: str = BRIDGE_ID) -> None:
        """Register this bridge's actions under the node's name."""
        self.hass = hass
        self.bridge_id = bridge_id
        self.revision = 5
        self.next_command_id = 5
        self.commands: dict[int, str] = {}
        self.read_only = False
        self.restore_incomplete = False
        self.state = "idle"
        self.last_result = ""
        # Every action this bridge was asked to perform, so a test can assert on silence.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Cleared to make the next learn fail the way a timeout does.
        self.capture_succeeds = True
        self._register()

    # Bookkeeping

    @property
    def mutations(self) -> list[str]:
        """The names of the actions that would have changed the registry."""
        changing = {
            "learn",
            "rename",
            "delete",
            "delete_id",
            "clear",
            "import",
            "restore_begin",
            "restore_commit",
        }
        return [name for name, _ in self.calls if name in changing]

    def add(self, name: str) -> int:
        """Seed a command, as if it had been learned before Home Assistant ever looked."""
        command_id = self.next_command_id
        self.next_command_id += 1
        self.commands[command_id] = name
        self.revision += 1
        return command_id

    # The actions themselves

    def _service(self, action: str) -> str:
        return f"{NODE_NAME.replace('-', '_')}_rf_{action}"

    def _register(self) -> None:
        self._register_one("status", self._status, response=True)
        self._register_one("export", self._export, response=True)
        self._register_one("learn", self._learn)
        self._register_one("cancel", self._cancel)
        self._register_one("rename", self._rename)
        self._register_one("delete_id", self._delete_id)
        self._register_one("send_id", self._send_id)

    def _register_one(self, action: str, handler, *, response: bool = False) -> None:
        def _wrapped(call: ServiceCall):
            self.calls.append((action, dict(call.data)))
            return handler(call)

        self.hass.services.async_register(
            ESPHOME_DOMAIN,
            self._service(action),
            _wrapped,
            supports_response=(
                SupportsResponse.OPTIONAL if response else SupportsResponse.NONE
            ),
        )

    def _status(self, call: ServiceCall) -> dict[str, Any]:
        return {
            "bridge_id": self.bridge_id,
            "revision": self.revision,
            "next_command_id": self.next_command_id,
            "restore_incomplete": self.restore_incomplete,
            "read_only": self.read_only,
            "fault": "none",
            "state": self.state,
            "last_result": self.last_result,
            "count": len(self.commands),
            "max_commands": 16,
            "max_pulses": 256,
            "used_bytes": 1004,
            "capacity_bytes": 12288,
            "commands": [
                {"id": command_id, "name": name, "pulses": len(TIMINGS)}
                for command_id, name in sorted(self.commands.items())
            ],
        }

    def _export(self, call: ServiceCall) -> dict[str, Any]:
        command_id = int(call.data["command_id"])
        name = self.commands.get(command_id)
        if name is None:
            return {"found": False}
        return {
            "schema": 1,
            "found": True,
            "bridge_id": self.bridge_id,
            "command_id": command_id,
            "name": name,
            "pulses": len(TIMINGS),
            "gap_us": 9000,
            "repeat_times": 4,
            "frequency_hz": 433920000,
            "modulation": 0,
            "timings": list(TIMINGS),
        }

    def _learn(self, call: ServiceCall) -> None:
        """Capture immediately, so a learn never has to be waited out in a test."""
        if not self.capture_succeeds:
            self.state = "failed"
            self.last_result = "timeout"
            return
        self.add(str(call.data["name"]))
        self.state = "captured"
        self.last_result = "stored"

    def _cancel(self, call: ServiceCall) -> None:
        self.state = "idle"

    def _rename(self, call: ServiceCall) -> None:
        command_id = int(call.data["command_id"])
        if command_id in self.commands:
            self.commands[command_id] = str(call.data["name"])
            self.revision += 1

    def _delete_id(self, call: ServiceCall) -> None:
        command_id = int(call.data["command_id"])
        if self.commands.pop(command_id, None) is not None:
            self.revision += 1

    def _send_id(self, call: ServiceCall) -> None:
        return None


def async_add_esphome_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Register the ESPHome config entry and device the bridge is reached through."""
    entry = MockConfigEntry(
        domain=ESPHOME_DOMAIN,
        unique_id=NODE_MAC,
        data={"device_name": NODE_NAME, "host": "127.0.0.1"},
        title="RF Bridge",
    )
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(NODE_MAC))},
        name="RF Bridge",
        manufacturer="Espressif",
        model="esp32",
    )
    return entry
