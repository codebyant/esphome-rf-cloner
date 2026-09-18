"""Host-side tests for the restore orchestration.

Restoring is the disaster-recovery path: it runs once, against hardware that holds nothing, and a
mistake in it is discovered at the worst possible moment. It cannot be rehearsed against a live
bridge without destroying that bridge's registry, so it is driven here against a fake transport
that enforces the firmware's own rules - restore_begin only on an empty registry, imports only
inside an open restore, and a commit that clears restore_incomplete.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402
from ha_stubs import HomeAssistantError  # noqa: E402

models = ha_stubs.load("models")
restore = ha_stubs.load("restore")
snapshot_module = ha_stubs.load("snapshot")

BridgeStatus = models.BridgeStatus
ExportedCommand = models.ExportedCommand
Snapshot = snapshot_module.Snapshot

BRIDGE_ID = "b7397c870440b9228377fbdb4ed95624"

const = ha_stubs.load("const")
#: The shared revision domain. Read from the integration so these tests track it.
REVISION_MAX = const.REVISION_MAX
REVISION_TERMINAL = const.REVISION_TERMINAL

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label} {detail}")


def status(**overrides) -> BridgeStatus:
    """An empty, healthy bridge, unless a test overrides part of it."""
    payload = {
        "bridge_id": "0" * 32,
        "revision": 1,
        "next_command_id": 1,
        "restore_incomplete": False,
        "read_only": False,
        "fault": "none",
        "state": "idle",
        "last_result": "",
        "count": 0,
        "max_commands": 16,
        "max_pulses": 128,
        "used_bytes": 0,
        "capacity_bytes": 12288,
        "commands": [],
    }
    payload.update(overrides)
    return BridgeStatus.from_payload(payload)


def snapshot(commands: int = 1, pulses: int = 65) -> Snapshot:
    """A replica holding `commands` commands, starting at command id 2."""
    return Snapshot(
        bridge_id=BRIDGE_ID,
        revision=14,
        next_command_id=4,
        captured_at=dt.datetime.now(dt.UTC),
        commands=tuple(
            ExportedCommand(
                schema=1,
                bridge_id=BRIDGE_ID,
                command_id=2 + index,
                name=f"cmd_{index}",
                gap_us=8669,
                repeat_times=20,
                frequency_hz=433920000,
                modulation=0,
                timings=tuple(range(-pulses, 0)),
            )
            for index in range(commands)
        ),
    )


class FakeBridge:
    """A stand-in for the firmware's registry, enforcing its restore rules."""

    def __init__(self, **status_overrides) -> None:
        self.bridge_id = "f" * 32
        self.next_command_id = 1
        self.revision = 1
        self.commands: dict[int, ExportedCommand] = {}
        self.restore_open = False
        self.committed = False
        self.calls: list[str] = []
        self.status_overrides = status_overrides
        self.fail_on: str | None = None

    async def async_status(self) -> BridgeStatus:
        self.calls.append("status")
        return status(
            bridge_id=self.bridge_id,
            next_command_id=self.next_command_id,
            revision=self.revision,
            restore_incomplete=self.restore_open,
            count=len(self.commands),
            commands=[
                {"id": c.command_id, "name": c.name, "pulses": len(c.timings)}
                for c in self.commands.values()
            ],
            **self.status_overrides,
        )

    def mutate(self) -> None:
        """An ordinary registry change, as a learn or a delete would be."""
        self.revision += 1

    def factory_reset(self) -> None:
        """Replacement hardware: a new identity and a revision sequence that starts over."""
        self.bridge_id = f"{self.revision:032x}"
        self.next_command_id = 1
        self.revision = 1
        self.commands.clear()
        self.restore_open = False
        self.committed = False

    async def async_restore_begin(self, bridge_id: str, next_command_id: int) -> None:
        self.calls.append("restore_begin")
        if self.fail_on == "restore_begin":
            raise HomeAssistantError("device refused the restore")
        assert not self.commands, "firmware refuses restore_begin on a non-empty registry"
        self.bridge_id = bridge_id
        self.next_command_id = next_command_id
        self.restore_open = True
        self.revision += 1

    async def async_import(self, command: ExportedCommand) -> None:
        self.calls.append(f"import:{command.command_id}")
        if self.fail_on == f"import:{command.command_id}":
            raise HomeAssistantError("device refused the import")
        assert self.restore_open, "firmware refuses imports outside a restore"
        self.commands[command.command_id] = command
        self.revision += 1

    async def async_restore_commit(self, source_revision: int) -> None:
        self.calls.append(f"restore_commit:{source_revision}")
        assert self.restore_open
        assert source_revision >= 0, "the device rejects a negative source revision"
        # Mirrors CommandStore::restore_commit: take the maximum first, then advance, so the
        # terminal value is refused instead of wrapped.
        base = max(source_revision, self.revision)
        if base >= REVISION_MAX:
            raise HomeAssistantError("revision_exhausted")
        self.revision = base + 1
        self.restore_open = False
        self.committed = True


async def test_target_checks() -> None:
    """A bridge that cannot safely receive a restore is refused before anything is written."""
    print("restore: unsuitable targets are refused")
    replica = snapshot()
    cases = [
        ("read-only storage", status(read_only=True, fault="payload_invalid")),
        ("a restore already open", status(restore_incomplete=True)),
        ("a registry that is not empty", status(count=1, commands=[{"id": 9, "name": "x", "pulses": 4}])),
        ("fewer slots than the replica needs", status(max_commands=0)),
        ("a pulse limit below the replica's waveform", status(max_pulses=8)),
    ]
    for label, target in cases:
        try:
            restore.check_restore_target(target, replica)
            check(label, False, "expected RestoreNotPossible")
        except restore.RestoreNotPossible as err:
            check(label, True, f"({err})")

    restore.check_restore_target(status(), replica)
    check("an empty healthy bridge is accepted", True)


async def test_happy_path() -> None:
    """A restore reinstates identity, ids, counter and waveforms exactly."""
    print("\nrestore: a replica is written back unchanged")
    bridge = FakeBridge()
    replica = snapshot(commands=2)
    await restore.async_restore(bridge, replica)

    check("the original bridge identity is adopted", bridge.bridge_id == BRIDGE_ID, bridge.bridge_id)
    check("the id counter is restored", bridge.next_command_id == 4, str(bridge.next_command_id))
    check("commands keep their original ids", sorted(bridge.commands) == [2, 3])
    check("the restore is committed", bridge.committed and not bridge.restore_open)
    check(
        "the protocol runs begin, imports, commit in order",
        [c for c in bridge.calls if c != "status"]
        == ["restore_begin", "import:2", "import:3", "restore_commit:14"],
        str(bridge.calls),
    )
    check(
        "the commit carries the snapshot's revision", "restore_commit:14" in bridge.calls
    )
    original = {c.command_id: c for c in replica.commands}
    check(
        "every signed timing survives",
        all(bridge.commands[i].timings == original[i].timings for i in (2, 3)),
    )
    check(
        "RF metadata survives",
        all(
            (
                bridge.commands[i].gap_us,
                bridge.commands[i].repeat_times,
                bridge.commands[i].frequency_hz,
                bridge.commands[i].modulation,
            )
            == (
                original[i].gap_us,
                original[i].repeat_times,
                original[i].frequency_hz,
                original[i].modulation,
            )
            for i in (2, 3)
        ),
    )


async def test_refuses_populated_target() -> None:
    """A bridge that already holds commands is never written to."""
    print("\nrestore: a populated bridge is left alone")
    bridge = FakeBridge()
    bridge.commands[7] = snapshot().commands[0]
    try:
        await restore.async_restore(bridge, snapshot())
        check("a populated target is refused", False, "expected RestoreNotPossible")
    except restore.RestoreNotPossible as err:
        check("a populated target is refused", True, f"({err})")
        check("nothing was written", "restore_begin" not in bridge.calls, str(bridge.calls))


async def test_midway_failure() -> None:
    """A device-side refusal is surfaced, and never reported as a success."""
    print("\nrestore: a failure part way through is surfaced")
    bridge = FakeBridge()
    bridge.fail_on = "import:3"
    try:
        await restore.async_restore(bridge, snapshot(commands=2))
        check("the failure propagates", False, "expected HomeAssistantError")
    except HomeAssistantError as err:
        check("the failure propagates", True, f"({err})")
        check("the restore stays open, so the device reports it", bridge.restore_open)
        check("it is never reported as committed", not bridge.committed)


async def test_incomplete_result_is_rejected() -> None:
    """A commit that leaves the wrong registry behind is treated as a failure."""
    print("\nrestore: a wrong end state is not accepted")
    bridge = FakeBridge()
    replica = snapshot(commands=2)

    async def lossy_import(command):
        # Mimic a device that accepts the call but stores nothing for one command.
        bridge.calls.append(f"import:{command.command_id}")
        if command.command_id != 3:
            bridge.commands[command.command_id] = command

    bridge.async_import = lossy_import
    try:
        await restore.async_restore(bridge, replica)
        check("a short write is detected", False, "expected HomeAssistantError")
    except HomeAssistantError as err:
        check("a short write is detected", True, f"({err})")


async def test_revision_continues_the_logical_sequence() -> None:
    """A restored bridge continues the replica's revision sequence instead of restarting it.

    Revision is the change token this integration keys its replica on, so a bridge that restarts
    numbering under an unchanged bridge_id could republish a revision already recorded, and the
    replica would then skip a refresh it needed.
    """
    print("\nrestore: the committed revision continues the logical sequence")
    bridge = FakeBridge()
    replica = snapshot()  # taken at revision 14
    check("replica is at revision 14", replica.revision == 14)

    await restore.async_restore(bridge, replica)
    check("restoring revision 14 commits revision 15", bridge.revision == 15, str(bridge.revision))

    bridge.mutate()
    check("the next ordinary mutation is revision 16", bridge.revision == 16, str(bridge.revision))


async def test_repeated_replacement_cannot_reuse_a_revision() -> None:
    """A second replacement, restoring the replica as it then stands, cannot repeat a revision."""
    print("\nrestore: a repeated replacement cannot republish a revision")
    bridge = FakeBridge()
    seen: list[int] = []

    replica = snapshot()
    await restore.async_restore(bridge, replica)
    seen.append(bridge.revision)
    bridge.mutate()
    seen.append(bridge.revision)

    # The replica is refreshed from the device, then the hardware is replaced again.
    for _ in range(3):
        current = Snapshot(
            bridge_id=replica.bridge_id,
            revision=bridge.revision,
            next_command_id=bridge.next_command_id,
            captured_at=replica.captured_at,
            commands=replica.commands,
        )
        bridge.factory_reset()
        check("replacement hardware restarts at revision 1", bridge.revision == 1)
        await restore.async_restore(bridge, current)
        check(
            f"committed revision {bridge.revision} is new",
            bridge.revision not in seen,
            f"already seen {seen}",
        )
        seen.append(bridge.revision)
        bridge.mutate()
        seen.append(bridge.revision)

    check("every revision published was distinct", len(seen) == len(set(seen)), str(seen))
    check("and strictly increasing", seen == sorted(seen), str(seen))


async def test_interrupted_restore_exposes_no_committed_revision() -> None:
    """An interrupted restore stays incomplete and never publishes the source's revision."""
    print("\nrestore: an interrupted restore publishes no committed revision")
    bridge = FakeBridge()
    bridge.fail_on = "import:2"
    try:
        await restore.async_restore(bridge, snapshot())
    except HomeAssistantError:
        pass
    check("the restore is still open", bridge.restore_open)
    check("it is not marked committed", not bridge.committed)
    check("no commit was attempted", not any(c.startswith("restore_commit") for c in bridge.calls))
    check(
        "the source revision was never published",
        bridge.revision != 15,
        f"revision is {bridge.revision}",
    )
    check("and the revision has not reached the source's", bridge.revision < 14)


async def test_firmware_ignoring_source_revision_is_rejected() -> None:
    """Firmware that restarts its own numbering is caught rather than silently trusted."""
    print("\nrestore: a bridge that does not advance the revision is rejected")

    class LegacyBridge(FakeBridge):
        async def async_restore_commit(self, source_revision: int) -> None:
            # Pre-change firmware: ignores the source and just advances locally.
            self.calls.append(f"restore_commit:{source_revision}")
            self.revision += 1
            self.restore_open = False
            self.committed = True

    bridge = LegacyBridge()
    try:
        await restore.async_restore(bridge, snapshot())
        check("the stale revision is detected", False, "expected HomeAssistantError")
    except HomeAssistantError as err:
        check("the stale revision is detected", True, f"({err})")


async def test_out_of_range_source_revision_is_refused() -> None:
    """A revision that cannot survive the wire is rejected before the device is touched.

    The action's argument is a signed 32-bit field, so a negative or oversized value would be
    narrowed on the way in and could land the device's revision near its ceiling.
    """
    print()
    print("restore: an unsendable source revision is refused before anything is written")
    transport = ha_stubs.load("transport")

    class Probe(FakeBridge):
        """Exposes the transport's own range check without a Home Assistant to call through."""

        async def commit(self, value: int) -> None:
            await transport.BridgeTransport.async_restore_commit(self, value)

        def service_name(self, action: str) -> str:
            return f"node_rf_{action}"

        async def _call(self, action, data=None, *, response=False):
            self.calls.append(f"sent:{action}:{data}")

    # REVISION_TERMINAL is the first value past the domain; the rest are further outside it.
    for bad in (-1, -14, REVISION_TERMINAL, 2**31, 2**32 - 1):
        probe = Probe()
        try:
            await probe.commit(bad)
            check(f"source_revision {bad} refused", False, "expected HomeAssistantError")
        except HomeAssistantError:
            check(f"source_revision {bad} refused", True)
            check(
                f"  nothing was sent for {bad}",
                not any(c.startswith("sent:") for c in probe.calls),
                str(probe.calls),
            )

    probe = Probe()
    await probe.commit(14)
    check("a valid source_revision is sent through", "sent:restore_commit:{'source_revision': 14}" in probe.calls, str(probe.calls))


async def test_unsendable_snapshot_revision_is_caught_before_writing() -> None:
    """check_restore_target refuses a replica whose revision could not be transmitted."""
    print()
    print("restore: a snapshot revision beyond the wire's range is refused up front")
    replica = Snapshot(
        bridge_id=BRIDGE_ID,
        revision=REVISION_MAX + 1,
        next_command_id=4,
        captured_at=dt.datetime.now(dt.UTC),
        commands=snapshot().commands,
    )
    bridge = FakeBridge()
    try:
        await restore.async_restore(bridge, replica)
        check("the unsendable revision is caught", False, "expected RestoreNotPossible")
    except restore.RestoreNotPossible as err:
        check("the unsendable revision is caught", True, f"({err})")
        check("nothing was written", "restore_begin" not in bridge.calls, str(bridge.calls))


async def test_revision_ceiling_is_refused() -> None:
    """A device already at the revision ceiling fails the commit and stays mid-restore.

    Reached through the device's own revision rather than the snapshot's: a snapshot revision that
    high could never be sent, because it exceeds the action's signed 32-bit argument.
    """
    print()
    print("restore: a device at the revision ceiling fails the commit")
    bridge = FakeBridge()
    bridge.revision = REVISION_MAX
    try:
        await restore.async_restore(bridge, snapshot())
        check("the commit is refused", False, "expected HomeAssistantError")
    except HomeAssistantError as err:
        check("the commit is refused", True, f"({err})")
        check("the restore stays open, so the device reports it", bridge.restore_open)
        check("it is not marked committed", not bridge.committed)
        check("no revision 0 was published", bridge.revision != 0)
        check("the revision never wrapped", bridge.revision >= REVISION_MAX)


async def test_every_committed_revision_is_representable() -> None:
    """No commit can produce a revision the transport could not carry back."""
    print()
    print("restore: every committed revision stays inside the transport's domain")
    check("the ceiling is below the terminal value", REVISION_MAX < REVISION_TERMINAL)
    check("and the terminal value is INT32_MAX", REVISION_TERMINAL == 2**31 - 1)

    # The highest commit the rule allows, driven through the real orchestrator.
    bridge = FakeBridge()
    bridge.revision = REVISION_MAX - 3
    replica = Snapshot(
        bridge_id=BRIDGE_ID,
        revision=REVISION_MAX - 1,
        next_command_id=4,
        captured_at=dt.datetime.now(dt.UTC),
        commands=snapshot().commands,
    )
    await restore.async_restore(bridge, replica)
    check("the highest successor is REVISION_MAX", bridge.revision == REVISION_MAX, str(bridge.revision))
    check("which the transport can send back", bridge.revision <= REVISION_MAX)

    # And that value is itself acceptable as a future source revision.
    transport = ha_stubs.load("transport")
    probe = FakeBridge()
    try:
        await transport.BridgeTransport.async_restore_commit(_Sendable(probe), bridge.revision)
        check("a snapshot taken at REVISION_MAX is sendable", True)
    except HomeAssistantError as err:
        check("a snapshot taken at REVISION_MAX is sendable", False, str(err))


class _Sendable:
    """Minimal stand-in exposing just what BridgeTransport.async_restore_commit touches."""

    def __init__(self, bridge: FakeBridge) -> None:
        self.bridge = bridge

    async def _call(self, action, data=None, *, response=False):
        self.bridge.calls.append(f"sent:{action}:{data}")


async def main() -> None:
    await test_target_checks()
    await test_happy_path()
    await test_refuses_populated_target()
    await test_midway_failure()
    await test_incomplete_result_is_rejected()
    await test_revision_continues_the_logical_sequence()
    await test_repeated_replacement_cannot_reuse_a_revision()
    await test_interrupted_restore_exposes_no_committed_revision()
    await test_firmware_ignoring_source_revision_is_rejected()
    await test_out_of_range_source_revision_is_refused()
    await test_unsendable_snapshot_revision_is_caught_before_writing()
    await test_revision_ceiling_is_refused()
    await test_every_committed_revision_is_representable()
    print(f"\n{PASSED} checks, {FAILED} failure(s)")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
