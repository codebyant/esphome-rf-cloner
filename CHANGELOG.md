# Changelog

Both halves of this project — the ESPHome component and the Home Assistant integration — ship from
this one repository under one version. A release tag pins both, and
`custom_components/rf_cloner/manifest.json` carries the same number.

This project is pre-release. Versions are `0.x`: the interfaces described in the documentation are
the ones being stabilised, and a `0.x` bump may change them. Anything that would make existing
stored commands unreadable would be called out here in full, and no release has done that.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] — 2026-09-18

First public release.

### The bridge

- `rf_cloner` ESPHome external component: learn arbitrary sub-GHz OOK remote commands at runtime by
  name, store them on the ESP32, and replay them. No recompile per command and no protocol
  decoding.
- Capture validation: pulse count and length bounds, and agreement between repeats within a
  tolerance, so a single noisy frame is never stored.
- Inter-frame gap measured off the waveform where the receiver's `idle` allows it, estimated from
  arrival times otherwise, with the configured default as the last fallback.
- Persistent registry with an immutable bridge identity and immutable, never-reused command ids.
  Survives reboots and firmware updates.
- Storage that refuses to overwrite what it cannot interpret: a bad header, index, format version
  or waveform CRC makes the session read-only, leaving every record intact. `rf_factory_reset` is
  the only escape, and is destructive by design.
- Portable per-command export and a three-phase restore protocol, so an interrupted restore is
  visible rather than silently partial.
- `packages/controls.yaml` — name field and learn, send, delete and cancel buttons, on the device's
  own web page and in Home Assistant. `rf_hide_in_ha` keeps them out of Home Assistant when the
  integration provides the UI instead.
- `packages/api-actions.yaml` — the full action surface, including `rf_status` as the canonical
  structured read.
- `config/hardware-check.yaml` — bring-up check built only from upstream ESPHome components.

### The Home Assistant integration

- Binds to an existing ESPHome config entry. No second connection to the node, and no IP address
  anywhere.
- One bridge device, linked to the ESPHome node that carries it.
- One button entity per learned command, created at runtime and keyed by the command's immutable
  id, so a rename leaves entities, history and automations alone.
- Native add, rename and delete through config subentries — no Developer Tools for ordinary use.
- Diagnostic sensors for stored commands, learn state, last result, storage used and revision, and
  problem sensors for read-only storage and an unfinished restore.
- Automatic snapshot of the full registry, waveforms included, refreshed on revision change and
  never left partial.
- Hardware replacement: reconfigure onto a new node, and a bridge reporting a different identity is
  offered a restore rather than silently adopted. Command ids are preserved, so entities survive.
- `rf_cloner.learn`, `rf_cloner.cancel_learn` and `rf_cloner.export_snapshot` actions for
  automations.
- Diagnostics download, deliberately without waveform timings.

### Known limitations

- Rolling-code targets cannot be replayed. This is a property of the target.
- Only 433.92 MHz ASK/OOK has been tested on hardware.
- Reconciliation is polling-based, roughly every 30 seconds, plus an immediate refresh after any
  change the integration makes itself.
- Two `rf_cloner` instances on one ESPHome node are supported through action prefixes, but have not
  been validated on real hardware.
- Home Assistant's snapshot is a faithful mirror of the device, not an undo buffer for a
  destructive action you asked for.

[Unreleased]: https://github.com/codebyant/esphome-rf-cloner/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/codebyant/esphome-rf-cloner/releases/tag/v0.1.0
