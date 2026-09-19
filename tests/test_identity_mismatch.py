"""Host-side tests for the identity-mismatch safety invariant.

When a bridge reports an identity that is not the one its config entry adopted, the hardware is
either a replacement or has been factory reset. Its registry describes a different logical bridge,
so Home Assistant must treat everything it says as irrelevant: adopt none of it, keep the RF
targets and command organisation it already has, and above all keep the replica, because that
replica is the only thing a restore can read from.

These are the invariants the live replacement test depends on, so they are pinned here rather than
left to be re-proven by hand against real hardware.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402
from ha_stubs import (  # noqa: E402
    ConfigEntryState,
    FakeEntityRegistry,
    Recorder,
    _ConfigSubentry,
)

# The mismatch warning is the behaviour under test, not something to read here.
logging.disable(logging.WARNING)

models = ha_stubs.load("models")
reconcile = ha_stubs.load("reconcile")
snapshot_module = ha_stubs.load("snapshot")
targets_module = ha_stubs.load("targets")

BridgeStatus = models.BridgeStatus

OURS = "b7397c870440b9228377fbdb4ed95624"
FOREIGN = "0123456789abcdef0123456789abcdef"
TARGET_ID = "aa11bb22cc33dd44ee55ff6677889900"

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


def status(bridge_id: str, commands: list[tuple[int, str]], **overrides) -> BridgeStatus:
    """A healthy status for `bridge_id` listing `commands`."""
    payload = {
        "bridge_id": bridge_id,
        "revision": 14,
        "next_command_id": 4,
        "restore_incomplete": False,
        "read_only": False,
        "fault": "none",
        "state": "idle",
        "last_result": "",
        "count": len(commands),
        "max_commands": 16,
        "max_pulses": 128,
        "used_bytes": 1004,
        "capacity_bytes": 12288,
        "commands": [{"id": i, "name": n, "pulses": 65} for i, n in commands],
    }
    payload.update(overrides)
    return BridgeStatus.from_payload(payload)


class FakeConfigEntries:
    """Records every entry and subentry mutation, so a test can assert that none happened."""

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    def async_add_subentry(self, entry, subentry) -> None:
        self.recorder.calls.append(("add_subentry", subentry.title))
        entry.subentries[subentry.subentry_id] = subentry

    def async_remove_subentry(self, entry, subentry_id) -> None:
        self.recorder.calls.append(("remove_subentry", subentry_id))
        entry.subentries.pop(subentry_id, None)

    def async_update_subentry(self, entry, subentry, *, title=None, **kwargs) -> None:
        self.recorder.calls.append(("update_subentry", title))
        if title is not None:
            subentry.title = title

    def async_update_entry(self, entry, *, options=None, **kwargs) -> None:
        self.recorder.calls.append(("update_entry", options))
        if options is not None:
            entry.options = options


class FakeHass:
    def __init__(self, recorder: Recorder) -> None:
        self.config_entries = FakeConfigEntries(recorder)
        self.entity_registry = FakeEntityRegistry(recorder)


class FakeEntry:
    entry_id = "01ENTRYIDFORTHETESTSXXXXXX"

    def __init__(self, unique_id: str) -> None:
        self.unique_id = unique_id
        self.title = "RF Bridge"
        self.state = ConfigEntryState.LOADED
        self.subentries: dict[str, _ConfigSubentry] = {}
        self.options: dict = {}

    def add_target(self, target_id: str, name: str) -> _ConfigSubentry:
        subentry = _ConfigSubentry(
            data={"target_id": target_id, "target_type": "fan", "area_id": None},
            subentry_type="target",
            title=name,
            unique_id=f"{self.unique_id}_target_{target_id}",
        )
        self.subentries[subentry.subentry_id] = subentry
        return subentry

    def assign(self, command_id: int, target_id: str | None, icon: str | None) -> None:
        stored = dict(self.options.get("commands", {}))
        entry: dict[str, str] = {}
        if target_id is not None:
            entry["target_id"] = target_id
        if icon is not None:
            entry["icon"] = icon
        stored[str(command_id)] = entry
        self.options = {**self.options, "commands": stored}


class FakeCoordinator:
    """Records any attempt to mutate the device."""

    def __init__(self, recorder: Recorder, data) -> None:
        self.recorder = recorder
        self.data = data

    async def async_delete(self, command_id: int, name: str) -> None:
        self.recorder.calls.append(("device_delete", command_id))


class FakeTransport:
    """Exports would be how a snapshot refresh reads the device; none should happen."""

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def async_export(self, command_id: int):
        self.recorder.calls.append(("export", command_id))
        return None


def hass_of(reconciler):
    """The FakeHass a reconciler was built against."""
    return reconciler._hass


def build(entry_unique_id: str = OURS):
    """A loaded entry with one target and one command assigned to it, fully instrumented."""
    recorder = Recorder()
    hass = FakeHass(recorder)
    entry = FakeEntry(entry_unique_id)
    entry.add_target(TARGET_ID, "Bedroom Fan")
    entry.assign(2, TARGET_ID, "mdi:fan")
    hass.entity_registry.add("button.probe_2", f"{OURS}_2", entry.entry_id)
    hass.entity_registry.add(
        "button.rf_bridge_cancel_learning", f"{OURS}_cancel_learn", entry.entry_id
    )
    coordinator = FakeCoordinator(recorder, status(OURS, [(2, "probe_2")]))
    reconciler = reconcile.CommandReconciler(hass, entry, coordinator)
    reconciler.async_activate()
    recorder.calls.clear()
    return recorder, entry, reconciler


async def test_reconciliation_freezes() -> None:
    """A foreign registry is not adopted, and the existing organisation is left alone."""
    print("identity mismatch: reconciliation freezes")
    recorder, entry, reconciler = build()

    # The replacement hardware is blank and calls itself something else.
    reconciler.async_reconcile(status(FOREIGN, []))

    check("the mismatch is detected", reconciler.identity_mismatch)
    check("nothing is added, removed or rewritten", not recorder, str(recorder.names()))
    check("the RF target survives", len(entry.subentries) == 1)
    target = next(iter(targets_module.targets(entry).values()))
    check("it still names the original target", target.name == "Bedroom Fan")
    check("it keeps its immutable id", target.target_id == TARGET_ID, target.target_id)
    meta = targets_module.meta_for(entry, 2)
    check("the command is still assigned to it", meta.target_id == TARGET_ID)
    check("and keeps its icon", meta.icon == "mdi:fan", str(meta.icon))
    check("no mutation is sent to the device", ("device_delete", 2) not in recorder.calls)


async def test_foreign_commands_are_not_adopted() -> None:
    """Commands belonging to the replacement hardware are ignored, not organised locally."""
    print("\nidentity mismatch: a foreign registry is not adopted")
    recorder, entry, reconciler = build()

    reconciler.async_reconcile(status(FOREIGN, [(1, "someone_elses"), (2, "also_theirs")]))

    check("the mismatch is detected", reconciler.identity_mismatch)
    check("nothing is adopted", not recorder, str(recorder.names()))
    check("no command is made visible", reconciler.async_visible_commands() == set())
    check(
        "the listing is reported as unusable, not as empty",
        reconciler.async_listed_commands() is None,
    )
    check("the organisation still holds exactly one command", len(targets_module.command_meta(entry)) == 1)
    check("and it is ours, not theirs", targets_module.meta_for(entry, 2).target_id == TARGET_ID)


async def test_recovery_after_identity_returns() -> None:
    """Once the original identity is back, reconciliation resumes normally."""
    print("\nidentity mismatch: reconciliation resumes once identity returns")
    recorder, entry, reconciler = build()
    reconciler.async_reconcile(status(FOREIGN, []))
    check("frozen while mismatched", reconciler.identity_mismatch)

    reconciler.async_reconcile(status(OURS, [(2, "probe_2")]))
    check("the mismatch clears", not reconciler.identity_mismatch)
    check("the organisation is unchanged, with no churn", not recorder, str(recorder.names()))
    check("the command is visible again", reconciler.async_visible_commands() == {2})
    check(
        "its entity was not purged",
        "button.probe_2" in hass_of(reconciler).entity_registry.records,
    )
    check(
        "and neither was the bridge's own",
        "button.rf_bridge_cancel_learning" in hass_of(reconciler).entity_registry.records,
    )
    check(
        "and still on its target",
        targets_module.meta_for(entry, 2).target_id == TARGET_ID,
    )


async def test_snapshot_refuses_foreign_registry() -> None:
    """The replica is not overwritten by a bridge that is not the one it belongs to."""
    print("\nidentity mismatch: the known-good replica is preserved")
    recorder = Recorder()
    manager = snapshot_module.SnapshotManager(FakeHass(recorder), OURS, FakeTransport(recorder))

    known_good = snapshot_module.Snapshot(
        bridge_id=OURS,
        revision=14,
        next_command_id=4,
        captured_at=ha_stubs.dt.datetime.now(ha_stubs.dt.UTC),
        commands=(),
    )
    manager._snapshot = known_good

    # A blank replacement, at a revision the manager has never seen.
    await manager.async_refresh(status(FOREIGN, [], revision=1, next_command_id=1))

    check("the replica is untouched", manager.snapshot is known_good)
    check("it still names the original bridge", manager.snapshot.bridge_id == OURS)
    check("the foreign device is never even read", not recorder, str(recorder.names()))


async def main() -> None:
    await test_reconciliation_freezes()
    await test_foreign_commands_are_not_adopted()
    await test_recovery_after_identity_returns()
    await test_snapshot_refuses_foreign_registry()
    print(f"\n{PASSED} checks, {FAILED} failure(s)")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
