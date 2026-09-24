# Installation

RF Cloner has two halves, installed separately:

- an **ESPHome component** that runs on the ESP32 and owns the learned commands;
- a **Home Assistant integration** that manages them.

The bridge is useful on its own. The integration is not useful without it, so build the bridge
first.

## 1. Wire the radio

Follow [hardware.md](hardware.md). The short version: CC1101 on **3.3 V**, SPI to the ESP32,
**GDO0 to the transmitter pin** and **GDO2 to the receiver pin**.

## 2. Prove the radio works

Flash [`config/hardware-check.yaml`](../config/hardware-check.yaml) before anything else. It uses
only upstream ESPHome components, so a failure there is a wiring or radio problem, never a bug in
this component.

It confirms three things:

1. the CC1101 answers over SPI — the boot log says `CC1101 found! Chip ID: 0x0014`;
2. a held remote button produces clean framed pulses in the raw dump;
3. a replayed frame actually drives your target.

Do not move on until all three work.

## 3. Flash the bridge

Copy [`config/rf-bridge-remote.yaml`](../config/rf-bridge-remote.yaml) into your ESPHome
directory. It fetches the component and the packages from this repository at compile time, so
there is nothing to clone:

```yaml
external_components:
  - source: github://codebyant/esphome-rf-cloner@v0.2.1
    components: [rf_cloner]

packages:
  rf_cloner:
    url: https://github.com/codebyant/esphome-rf-cloner
    ref: v0.2.1
    refresh: 1d
    files:
      - path: packages/api-actions.yaml
        vars:
          rf_cloner_id: cloner
      - path: packages/controls.yaml
        vars:
          rf_cloner_id: cloner
          rf_hide_in_ha: "false"
```

Both refs name a release tag, and must name the same one. A rebuild gets exactly the firmware it
got last time rather than whatever is on the default branch that day. You move the tag only when a
release says the bridge needs updating — see [Updating](#updating).

Then:

- set the pins to match your wiring;
- set `cc1101.frequency` to your remote's band;
- put `wifi_ssid`, `wifi_password` and `api_key` in your ESPHome `secrets.yaml`;
- tune `remote_receiver`'s `filter` and `idle` for your remote — see
  [hardware.md](hardware.md#capture-tuning);
- flash.

### What the two packages give you

| Package | Contents | Needed for |
|---|---|---|
| `packages/api-actions.yaml` | `rf_learn`, `rf_send`, `rf_status`, `rf_export`, restore, … as Home Assistant actions | **Required** by the Home Assistant integration |
| `packages/controls.yaml` | A name field, learn/send/delete/cancel buttons, learn-state and storage sensors | The bridge's own web page, and use without the integration |

Both are optional as far as the component is concerned, but the integration speaks to the bridge
through `api-actions.yaml` and nothing else, so omit it and the integration cannot see the bridge.

If you use the integration, `controls.yaml`'s entities duplicate what it already offers. Set
`rf_hide_in_ha: "true"` to keep them on the device's web page — where they remain the fallback
with Home Assistant down — and out of Home Assistant.

### Home Assistant API

The `api:` block is required. The integration goes through Home Assistant's existing ESPHome
connection, so the node has to be adopted by Home Assistant in the ordinary way first — it should
be listed under **Settings → Devices & services → ESPHome** before you go on:

![The ESPHome integration listing the RF Bridge node](../assets/screenshots/01-esphome-node.png)

 It is never
addressed by IP, so DHCP can move it freely.

### Building from a clone

[`config/rf-bridge.yaml`](../config/rf-bridge.yaml) is the same configuration with local paths, for
working on the component itself.

## 4. Install the Home Assistant integration

Requires **Home Assistant 2026.8 or newer**. Earlier versions lack the device registry API the
integration uses to link the bridge to its ESPHome node (`async_get_device_by_connection`), and
will fail at setup.

HACS refuses to install on an older version, because `hacs.json` declares the floor. A manual copy
has nothing to enforce it, so check your version first if you install by hand.

### Through HACS

RF Cloner is not in the HACS default store, so add it as a custom repository:

1. **HACS**, then the three-dot menu, then **Custom repositories**.
2. Repository `https://github.com/codebyant/esphome-rf-cloner`, category **Integration**.
3. Find **RF Cloner Bridge** in HACS and install it.
4. Restart Home Assistant.

### By hand

Copy `custom_components/rf_cloner/` from this repository into your Home Assistant configuration
directory, so that `config/custom_components/rf_cloner/manifest.json` exists. Restart Home
Assistant.

## 5. Bind the integration to the bridge

1. **Settings → Devices & services → Add integration → RF Cloner Bridge**.
2. Pick the ESPHome node running the bridge. It has to be connected.
3. Leave **Action prefix** alone unless you changed `rf_action_prefix` in the package.

![The Connect an RF bridge dialogue](../assets/screenshots/02-connect-bridge.png)

A new **RF Cloner bridge** device appears, linked to the ESPHome node. Commands already stored on
the bridge are adopted immediately.

The integration page now shows the bridge as a hub, with **Add an RF device** and **Learn command**
at the top:

![The RF Cloner Bridge integration page](../assets/screenshots/03-integration-page.png)

If the node is rejected with *"That node exposes no rf_cloner actions"*, the node is not running
`packages/api-actions.yaml`, or is not connected.

## 6. Learn your first command

Optionally, first [add an RF device](guides/add-an-rf-device.md) for the equipment the remote
drives, so its commands land grouped together.

Then, on the integration page, **Learn command**. Name it, choose which RF device it belongs to —
or leave it unassigned — then hold the button on the remote when the dialogue asks. A button
entity appears under that name.

![The Learn a command dialogue](../assets/screenshots/05-learn-command-form.png)

The full walkthrough, including what to do when a learn fails, is
[guides/learn-a-command.md](guides/learn-a-command.md). The reference for grouping, renaming,
icons, deleting and the automation actions is [home-assistant.md](home-assistant.md).

## Updating

RF Cloner has two parts, released under one version number but updated separately.

- **The Home Assistant integration** updates through HACS like any other (or recopy the folder),
  then restart Home Assistant.
- **The ESPHome bridge** does not need reflashing for every release. Most releases change only the
  integration, and their notes say *No ESPHome bridge update is required*.

When a release does change the bridge, its notes open with **ESPHome bridge update required** and
name the tag to use. Then move both refs in your bridge's YAML — `@vX.Y.Z` on `source:` and
`ref: vX.Y.Z` — to that tag, rebuild and flash, and update the integration. Stored commands survive
a firmware update.

## Uninstalling

Deleting the config entry removes the bridge device, its command entities and Home Assistant's
replica of the registry. It does **not** touch what is stored on the ESP32 — reinstall and rebind,
and every command is adopted again.

To wipe the device itself, call `rf_factory_reset` on the node. That is destructive and
irreversible: it discards every command and mints a new bridge identity. Export first.
