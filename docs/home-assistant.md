# Home Assistant

The **RF Cloner Bridge** integration turns a bridge's learned commands into ordinary Home
Assistant entities and gives you a native way to add, rename and remove them.

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

Commands are **not** Home Assistant devices — a stored waveform is not a piece of hardware. They
are entities belonging to their own config subentry, which means each one can be given its own
**area** independently of the bridge, and appear on dashboards and in automations like any other
button.

## Learning a command

On the bridge's device page, **Learn a command**.

1. Enter a name. Letters, digits, underscore, hyphen and full stop; up to 23 characters. This is
   the name stored on the device, so it is what the bridge's own web page shows too.
2. The dialogue asks you to hold the button on the remote. **Hold it** — remotes repeat their
   frame while held, and agreement between repeats is what validates the capture.
3. The button entity appears.

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

## Renaming

The command's three-dot menu, then **Rename command**.

A rename changes the label on the device and in Home Assistant, and nothing else. Commands are
keyed by an immutable id, so the entity, its entity id, its history and every automation
referencing it are untouched.

## Deleting

Delete the command's subentry from the bridge's device page. Home Assistant asks for confirmation,
then the integration deletes it on the device by its immutable id.

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

## When the bridge is replaced

Covered in [backup-and-replacement.md](backup-and-replacement.md).
