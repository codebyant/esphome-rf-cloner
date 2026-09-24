# Changelog

Both halves of this project — the ESPHome component and the Home Assistant integration — ship from
this one repository under one version. A release tag pins both, and
`custom_components/rf_cloner/manifest.json` carries the same number.

This project is pre-release. Versions are `0.x`: the interfaces described in the documentation are
the ones being stabilised, and a `0.x` bump may change them. Anything that would make existing
stored commands unreadable would be called out here in full, and no release has done that.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Documentation

- **The ESPHome bridge is pinned to a release tag**, not `main`, in `config/rf-bridge-remote.yaml`
  and every example that pulls from GitHub, so a rebuild can no longer pick up an unreleased
  change. It starts at `v0.2.1`, whose ESPHome side is identical to every earlier release. The
  bridge no longer needs reflashing for each release: the integration updates through HACS, and
  release notes now say explicitly whether an ESPHome bridge update is required.

## [0.2.1] — 2026-09-23

A maintenance release on top of 0.2.0. Nothing about how commands are stored, identified, backed
up or restored has changed, the ESPHome component is untouched, and no migration runs: updating
the integration is all there is to it.

### Fixed

- **Adding the integration showed an empty form.** On Home Assistant 2026.9 the *Connect an RF
  bridge* step rendered with no fields at all, so a new install could not get past it. The ESPHome
  node is now chosen from an ordinary dropdown built from the ESPHome config entries that exist:
  the frontend cannot compute an initial value for a required `config_entry` selector and gives up
  on the whole form when it meets one. The value stored is still the ESPHome config entry id, so
  nothing else changes. Setting up with no ESPHome integration at all now says so instead of
  offering an empty picker.
- **A command learned right after another could lose its RF device and icon.** Its button came up
  on no device, or not at all. The metadata was written, then pruned against a read of the bridge
  taken before the command existed, because Home Assistant defers a refresh requested within 10
  seconds of the last. Ids the bridge had not yet handed out when that read was taken are now left
  alone until a read that could list them arrives. Present since 0.2.0, through either way of
  learning.

### Added

- **The integration has its own icon.** Home Assistant showed *icon not available* everywhere this
  integration appeared, because it draws integration icons from `brands.home-assistant.io` and
  that repository only accepts integrations that are already distributed. Home Assistant 2026.3
  and later look in a custom integration's own `brand/` directory first, so the icon now ships
  here and no longer waits on a pull request to someone else's repository. One `icon.png` covers
  every variant Home Assistant asks for - the 2x, the logo and the dark-mode forms all fall back
  to it.
- **`Learn command` on the integration page**, beside *Add an RF device*. Learning was reachable
  only through the bridge's **Configure** menu, two screens in, which is a strange place for the
  thing this integration exists to do. The button opens the same learn form directly — name, RF
  device, icon — and the **Configure -> RF commands** route is unchanged for anyone already using
  it. Both entrances run the same code, so a command learned either way is the same record.
  Existing installs get the button on update; nothing is migrated and nothing is stored
  differently.

### Documentation

- **Step-by-step guides with screenshots**, in `docs/guides/`: learning a new command (and
  relearning one), adding an RF device, renaming, moving and deleting a command, and using
  commands on dashboards, in automations and on the bridge itself. The README, installation and
  Home Assistant pages now show the real dialogues, and their directions point at where things
  actually are - *Add an RF device* and *Learn command* on the integration page, not on the
  bridge's device page.
- The README links the in-depth write-up of how the project was built.

## [0.2.0] — 2026-09-18

### RF devices

- Learned commands can be grouped under **RF devices**: one Home Assistant device per piece of
  equipment — a ceiling fan, a gate, a shutter — with that equipment's commands as its buttons.
  Each RF device is linked to the bridge it is reached through.
- RF devices carry an optional **type** (Fan, Gate, Light, Shutter or cover, TV, Air conditioner,
  Generic, Other) and an **area**. The area is applied when the device is created and never
  reapplied, so a later move of your own is not overwritten.
- Commands may stay **unassigned**, and do by default. An unassigned command is an ordinary
  button with no device, which is what every 0.1 command already was. No placeholder device is
  invented for them.
- Commands can be moved between RF devices, and back to unassigned, without relearning. A move
  keeps the command's id, unique id, entity id and history, writes nothing to the bridge and does
  not advance the registry's revision.
- Per-command **icons**, chosen with Home Assistant's icon picker, or suggested from the command's
  name and its RF device's type. The integration's icon is a default: an icon set on the entity
  itself takes precedence and is never overwritten.
- Learning now asks which RF device a command belongs to and lands it there directly, rather than
  having it appear unassigned and move a moment later.
- Command management — learn, rename, re-icon, move, delete — moved into the bridge's
  **Configure** menu, so none of it needs Developer Tools.

### Changed

- **Deleting a subentry no longer deletes an RF command.** In 0.1 a subentry was a command; in 0.2
  it is a piece of equipment. Removing an RF device now only ungroups: its commands become
  unassigned, keep their buttons and history, and stay on the bridge. Deleting a command is a
  separate, explicitly confirmed action that still deletes by immutable id.
- The config entry's schema version is now 2. Existing 0.1 entries are migrated on first load:
  commands become unassigned, every entity keeps the registry record it already had — same entity
  id, unique id, area, icon and customisations — and nothing is written to the bridge.

### Fixed

- A command deleted on the bridge itself now has its entity registry record removed with it,
  instead of leaving an orphaned record holding on to its entity id.
- An RF device's area field is pre-filled from where its Home Assistant device actually is, rather
  than from the area stored when the device was created. Those differ as soon as you move the
  device yourself, and choosing an area is now applied instead of being mistaken for no change.

### Testing

- New suite that runs the integration inside a real Home Assistant, covering the 0.1 migration,
  the target model, assignment, icons, reconciliation, deletion semantics and hardware
  replacement. The existing suites, which stub Home Assistant, keep covering the pure logic.

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

[Unreleased]: https://github.com/codebyant/esphome-rf-cloner/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/codebyant/esphome-rf-cloner/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/codebyant/esphome-rf-cloner/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/codebyant/esphome-rf-cloner/releases/tag/v0.1.0
