# Home Assistant

The **RF Cloner Bridge** integration turns a bridge's learned commands into ordinary Home
Assistant entities, groups them under the equipment they actually drive, and gives you a native
way to add, rename, organise and remove them.

Installing it is covered in [installation.md](installation.md). This page is about using it.

## Where authority lives

The bridge owns the registry. Home Assistant reads it, mirrors it, and changes it only when you
ask for a change.

That matters in practice:

- A command learned from the device's own web page shows up in Home Assistant by itself.
- Replay does not depend on Home Assistant being up.
- Home Assistant's copy is a **replica**, not the master. It exists so a dead ESP32 can be
  replaced, not so the two can disagree.

The integration reaches the node through Home Assistant's existing ESPHome connection — the same
one the node's own entities use. It never opens a connection of its own and never needs the
bridge's IP address.

## What you get

### The bridge device

One device per logical bridge, shown as connected *via* the ESPHome node that carries it. Its
entities:

| Entity | What it tells you |
|---|---|
| **Stored commands** | How many commands are on the device |
| **Learn state** | `Idle`, `Waiting for a button press`, `Captured`, `Failed` |
| **Last result** | The name just learned, or why the last operation failed |
| **Read-only storage** | On when the device booted on storage it refuses to interpret |
| **Unfinished restore** | On when a restore began and never committed |
| **Storage used** | Bytes used on the device *(disabled by default)* |
| **Registry revision** | The registry's change token *(disabled by default)* |
| **Cancel learning** | Disarms a capture that is waiting for a press |

**Read-only storage** and **Unfinished restore** are `problem` binary sensors: both are states you
have to resolve deliberately, and both make the bridge refuse changes until you do. See
[troubleshooting.md](troubleshooting.md#the-device-booted-read-only).

### One button per command

Each learned command becomes a button entity. Press it to replay.

A single command is **not** a Home Assistant device — a stored waveform is not a piece of
hardware. The equipment the command drives is, and that is what an *RF device* represents.

## RF devices

An **RF device** is the thing in the room: a ceiling fan, a gate, a projector, a shutter. You
create one in Home Assistant, assign commands to it, and its buttons appear together under one
device, in one area.

```
RF Cloner Bridge
├── Bedroom Fan
│   ├── Power
│   ├── Light
│   ├── Speed 1
│   └── Speed 2
└── Garage Gate
    ├── Open
    ├── Close
    └── Stop
```

RF devices exist **only in Home Assistant**. The bridge does not know about them, stores nothing
about them, and does not need to: grouping is how you want the commands presented, not what the
radio does. It follows that creating, renaming, retyping or deleting an RF device is never an RF
operation — no waveform is touched and the registry's revision does not move.

### Creating one

On the bridge's device page, **Add an RF device**. Give it a name, a type and optionally an area.

The **type** — Fan, Gate, Light, Shutter or cover, TV, Air conditioner, Generic, Other — is
presentation only. It labels the device and suggests icons. It deliberately does **not** turn the
target into a `fan` or `cover` entity: most RF remotes send blind, with no feedback, so Home
Assistant cannot know whether the fan is actually on. Buttons stay truthful about that. Composed,
stateful entities are a separate feature.

The **area** is applied when the device is first created. After that the device's area is yours —
move it wherever you like and the integration will not move it back.

### Unassigned commands

A command does not have to belong to an RF device. Commands learned on the bridge's own web page
arrive unassigned, commands from before you started grouping stay unassigned, and you can leave
them that way forever.

An unassigned command is a full-fledged button: it works, it keeps its identity, it can carry its
own area, and it can be assigned to an RF device later. There is no fake "Unassigned" device —
these entities simply belong to no device, which is exactly what 0.1 did.

### Moving a command

**Configure** on the bridge, then **Edit or delete a command**, then pick a different RF device —
or **Unassigned**.

Moving is Home Assistant-side only. The command keeps its id, its unique id, its entity id, its
history and its waveform, and every automation referring to it keeps working. Nothing is relearned
and the bridge is not contacted.

## Learning a command

On the bridge's device page, **Configure**, then **Learn a command**.

1. Enter a name. Letters, digits, underscore, hyphen and full stop; up to 23 characters. This is
   the name stored on the device, so it is what the bridge's own web page shows too.
2. Choose the RF device it belongs to, or leave it **Unassigned**.
3. Optionally choose an icon. Left empty, one is suggested from the name and the device's type.
4. The dialogue asks you to hold the button on the remote. **Hold it** — remotes repeat their
   frame while held, and agreement between repeats is what validates the capture.
5. The button entity appears, already under the right device.

If nothing is captured, the dialogue says why, in the device's own words:

| Reason | Meaning |
|---|---|
| `timeout` | No usable frames arrived before the learn window closed |
| `no_repeat_agreement` | Frames arrived but never matched each other within the tolerance |
| `too_many_pulses` | The waveform is longer than the configured `max_pulses` |
| `store_full` | Every command slot is occupied |
| `budget_exceeded` | Storing it would exceed `max_storage_bytes` |

Each of these is a device-side tuning problem, covered in
[troubleshooting.md](troubleshooting.md#learning-times-out).

Learning under a name that already exists overwrites that command. That is the intended way to
relearn one that has drifted.

## Renaming a command

**Configure**, then **Edit or delete a command**, then change the name.

A rename is the one command edit that *does* reach the bridge, because the name is stored there —
it is what the bridge's own web page shows. Commands are keyed by an immutable id, so the entity,
its entity id, its history and every automation referencing it are untouched.

## Icons

Every command can carry an icon, chosen in the same **Edit or delete a command** screen. Leaving
the field empty falls back to a suggestion based on the command's name and its RF device's type,
and then to a generic remote icon.

Changing a command's icon touches nothing on the bridge.

The integration's icon is a **default**. If you set an icon on the entity itself — through Home
Assistant's own entity settings — yours wins, and the integration will never overwrite it. That
holds across reloads, restarts and moves between RF devices.

> **RF devices have no icon.** Home Assistant's device registry has no icon field, and the ways
> around that — inventing entities purely to carry one, or writing unsupported registry fields —
> are not worth the breakage. Device *type* and per-command icons are what this version offers. If
> Home Assistant gains real device icons, this integration will use them.

## Deleting

These are two different things, and the difference matters.

### Deleting an RF device

Delete the RF device from the bridge's page. This is **organisational**:

- the RF device and its Home Assistant device disappear,
- its commands become **Unassigned** and keep their buttons, ids, icons and history,
- **nothing is deleted from the bridge** and the registry's revision does not move.

If you later want those commands grouped again, create a new RF device and assign them.

> This is a deliberate change from 0.1, where a subentry *was* a command and removing one deleted
> it from the bridge. In 0.2 a subentry is a piece of equipment, and removing a piece of equipment
> from Home Assistant is not a reason to erase waveforms.

### Deleting a command

**Configure**, then **Edit or delete a command**, tick **Delete this command from the bridge**,
and confirm on the screen that follows. Ticking the box alone deletes nothing.

This one *is* destructive: the waveform is erased from the bridge by its immutable id, and the
button and its history go with it. Only a snapshot restore brings it back.

If the device refuses — it is read-only, or offline — the command comes back on the next poll
rather than vanishing from Home Assistant while still living on the bridge.

## Actions

Most management is native UI. These exist for automations and scripts.

### `rf_cloner.learn`

Arms a capture and waits for it to finish. Fails with the device's own reason if nothing is
learned.

```yaml
action: rf_cloner.learn
data:
  config_entry_id: <the bridge>
  name: shutter_up
```

### `rf_cloner.cancel_learn`

Disarms a capture that is waiting for a press.

### `rf_cloner.export_snapshot`

Returns Home Assistant's replica of the registry, waveforms included, in the bridge's own portable
format. Use it to keep a copy somewhere outside Home Assistant.

```yaml
action: rf_cloner.export_snapshot
data:
  config_entry_id: <the bridge>
response_variable: registry
```

**Replaying** a command from an automation is just pressing its button:

```yaml
action: button.press
target:
  entity_id: button.shutter_up
```

## How Home Assistant stays in step

The integration polls the bridge's `rf_status` roughly every **30 seconds**, and refreshes
immediately after any change it made itself. `rf_status` carries no waveforms, so it stays cheap
enough to poll.

So a change you make in Home Assistant appears at once, and a change made elsewhere — the device's
web page, an ESPHome automation, a relearn — appears within about half a minute.

The **snapshot** is rewritten only when the registry's revision changes, and a refresh that cannot
read every command is abandoned whole, leaving the previous snapshot exactly as it was. A slightly
stale complete replica is worth more than a fresh partial one.

## Diagnostics

The bridge's device page has **Download diagnostics**. It carries the bridge's identity, the
status last read, the command inventory, the state of the replica, and which ESPHome entry the
bridge is bound to.

It also lists the RF devices, which commands belong to each, and which commands carry an icon.

It deliberately leaves out **waveform timings** — they are large and almost never needed for
support. Use `rf_cloner.export_snapshot` if a waveform is genuinely in question. It contains no
credentials, no tokens and nothing about the rest of your Home Assistant configuration.

## Running two bridges on one node

Supported, through the component's action prefixes: give the second `rf_cloner` instance a
different `rf_action_prefix`, then enter that prefix when adding the second bridge. Architecturally
sound, but not yet validated on real hardware — see
[configuration.md](configuration.md#two-instances-on-one-device).

## Destructive actions

Two of the bridge's ESPHome actions destroy data and are **not** exposed in this integration's UI.
They are reachable from Developer Tools and from automations, where Home Assistant asks for no
confirmation, so treat them carefully:

| Action | Effect |
|---|---|
| `esphome.<node>_rf_clear` | Drops every command. Keeps the bridge identity and the id counter |
| `esphome.<node>_rf_factory_reset` | Discards the registry *and* the bridge identity, and writes a fresh empty one |

`rf_factory_reset` is the only way out of read-only storage, which is why it exists. Neither is
undoable on the device. Home Assistant's snapshot is a faithful mirror, so a few seconds after a
clear it will mirror an empty registry too — it is a replica, not an undo buffer. Export before
you run either.

## Upgrading from 0.1

0.1 had no RF devices: every command was a subentry of its own and belonged to no device. The
upgrade runs once, by itself, the first time 0.2 loads the config entry.

Afterwards:

- every command is still there, with the **same command id on the bridge**;
- every button keeps its **entity id**, its unique id, its history and its customisations, so no
  automation, script or dashboard needs editing;
- every command starts out **Unassigned**;
- **nothing is written to the bridge** — no relearn, no rename, no delete, and the registry's
  revision does not move.

Grouping is then entirely up to you: create RF devices and move commands onto them whenever you
feel like it, or never.

## When the bridge is replaced

Covered in [backup-and-replacement.md](backup-and-replacement.md). In short, your grouping
survives it: RF devices and command assignments live in Home Assistant's own configuration, not on
the bridge, so a restore onto replacement hardware brings each command back under the RF device it
was already on.
