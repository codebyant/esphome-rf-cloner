# Backup and hardware replacement

Learned commands only exist as waveforms. There is no protocol to re-derive them from, so if the
ESP32's flash goes, the only way back is a copy — and the only way to make one is while the bridge
is alive.

Two copies exist, and they serve different purposes.

| | Where | Made by | For |
|---|---|---|---|
| **Snapshot** | Home Assistant's own storage | Automatic | Restoring onto replacement hardware |
| **Export** | Wherever you put it | You | Keeping a copy outside Home Assistant |

## Identity

Two identities carry all of this.

- A **bridge id** identifies one *logical* bridge. It is minted on first boot, persists across
  reboots and firmware updates, and survives a clear. Only a factory reset replaces it.
- A **command id** identifies one command within a bridge. It is immutable, never reused, and
  unaffected by renaming.

Home Assistant keys every command entity by `{bridge id}_{command id}`. That is why a rename costs
nothing, and why a restore that preserves command ids also preserves every entity, dashboard card,
automation and history graph pointing at them.

## The snapshot

Home Assistant keeps one complete replica per bridge: every command, with its waveform, gap,
repeat count, frequency and modulation, plus the bridge id and the id counter.

It is refreshed when the bridge's revision changes — the registry's change token — so it is
rewritten only when something actually changed. A refresh that cannot read every command is
abandoned whole, leaving the previous snapshot untouched. A slightly stale complete replica beats a
fresh partial one.

The snapshot is a **faithful mirror**, not an undo buffer. Clear the bridge and, a poll later, the
snapshot mirrors an empty registry too. It protects you from dead hardware, not from a destructive
action you asked for.

### What it deliberately does not hold

The snapshot is the **RF registry** and nothing else. Your Home Assistant-side organisation — RF
devices, which command belongs to which, their types, their areas and their icons — is not in it,
and must not be: it is not RF state, it means nothing to a bridge, and a change to it should never
cause a waveform to be re-exported.

That organisation lives in the integration's config entry, which an ordinary Home Assistant backup
already covers. The two restore independently and meet correctly: the RF snapshot brings the
commands back under the ids they held, and the config entry still says which RF device each of
those ids belongs to.

To keep a copy outside Home Assistant:

```yaml
action: rf_cloner.export_snapshot
data:
  config_entry_id: <the bridge>
response_variable: registry
```

## Exporting without Home Assistant

The bridge exports itself, one command at a time, through its own ESPHome action:

```yaml
action: esphome.<node>_rf_export
data:
  command_id: 2
response_variable: command
```

```json
{
  "schema": 1,
  "found": true,
  "bridge_id": "0123456789abcdef0123456789abcdef",
  "command_id": 2,
  "name": "shutter_up",
  "pulses": 65,
  "gap_us": 8669,
  "repeat_times": 20,
  "frequency_hz": 433920000,
  "modulation": 0,
  "timings": [242, -744, 244, -739]
}
```

Everything needed to reproduce the command on another bridge, and nothing else. `schema` versions
this payload independently of the on-flash format. `rf_status` lists the command ids to iterate.

## Replacing dead hardware

1. **Flash the replacement** with the same configuration. It boots empty, mints a bridge id of its
   own, and Home Assistant adopts it as an ESPHome node in the usual way.
2. In Home Assistant, open the **existing** RF Cloner bridge — the config entry, not a new one —
   and choose **Reconfigure**.
3. Pick the new ESPHome node.
4. It reports an identity that is not the one this entry expects, so instead of adopting it, Home
   Assistant offers to **restore**. Confirm.

Every command is written back under its original id, and the bridge adopts the original bridge id.
Your entities keep working, and so does your grouping: because entities are keyed by
`{bridge id}_{command id}`, each restored command reappears under the RF device it was already
assigned to, with its icon, without anything having to be re-organised.

### Why it will not do this silently

A bridge reporting an unexpected identity is treated as a fact to be resolved, never as a change
to follow:

- Reconciliation **freezes**. Home Assistant does not drop the commands it mirrors just because
  the hardware in front of it has none.
- The snapshot is **not overwritten**. The foreign device is not even read.
- Your **RF devices and command assignments are untouched**, waiting for the restore that brings
  their commands back.
- The foreign identity is **not adopted**. The config entry's identity is the logical bridge's,
  not whatever the hardware currently claims.

That freeze is exactly what leaves a known-good replica to restore from. The same protection
applies to a bridge that was factory-reset by accident.

### What is checked before anything is written

A restore is refused up front, with a reason, if the target:

- is in **read-only** mode;
- already has an **unfinished restore**;
- **already holds commands** — the device itself only begins a restore on an empty registry, which
  is what stops a stale backup overwriting a live bridge;
- has **fewer command slots** than the snapshot needs;
- has a **`max_pulses`** too small for one of the stored waveforms.

### What happens during one

Three phases, which are the device's own protocol:

```
rf_restore_begin(bridge_id, next_command_id)
rf_import(...)   once per command
rf_restore_commit(source_revision)
```

Between `begin` and `commit` the bridge reports `restore_incomplete`, which survives a reboot and
refuses normal mutations. An interrupted restore is therefore visible rather than silently
partial — the **Unfinished restore** binary sensor turns on. Repeat the restore, or clear the
bridge to abandon it.

After the commit, Home Assistant verifies the bridge reports the right identity, the right command
count, no unfinished restore, and a revision that advances past the snapshot's. Anything else is
an error rather than a quiet success.

## Moving a bridge to a renamed or re-added node

The same **Reconfigure** flow. If the node reports the identity this entry already expects, it is
simply re-pointed at it — no restore, nothing written.

## Restoring onto a bridge that still has commands

Deliberately refused. Clear the target first (`esphome.<node>_rf_clear`), which is destructive and
irreversible on that device. Be certain you are clearing the replacement and not the original.
