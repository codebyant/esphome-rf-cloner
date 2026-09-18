# esphome-rf-cloner

A learn-and-replay RF bridge for ESPHome and Home Assistant, built on an ESP32 and a CC1101.

Teach it any compatible OOK remote command at runtime, give it a name, and replay it later. No
YAML editing and no recompile per command, and learned commands live on the device, so they
survive reboots and firmware updates.

## What it does

ESPHome already ships the CC1101 driver, raw OOK capture and replay, and persistent storage. Home
Assistant ships an RF transmitter entity domain. Neither ships RF *learning* or a store for
learned codes — this adds that layer: a named, persistent, on-device command registry with a learn
state machine and capture validation.

- Learn arbitrary OOK commands at runtime, by name
- Store them on the ESP32, surviving reboots and firmware updates
- Replay, list and delete from Home Assistant, the built-in web server, or automations
- Keeps working with Home Assistant offline

**What it cannot do:** control rolling-code devices. Any target whose transmission changes between
presses is outside what raw replay can achieve, regardless of implementation.

## Hardware

An ESP32 plus a CC1101 module for your band. **The CC1101 runs at 3.3 V — never 5 V.** Wiring and
supported hardware are in [docs/hardware.md](docs/hardware.md).

## Getting started

1. Wire the CC1101 as described in [docs/hardware.md](docs/hardware.md).
2. Flash [`config/hardware-check.yaml`](config/hardware-check.yaml) and confirm the radio is
   detected, captures look clean, and a replayed frame drives your device.
3. Copy [`config/rf-bridge.yaml`](config/rf-bridge.yaml), adjust the pins and capture tuning for
   your remote, and flash it.
4. Type a name into **Command name**, hold a button on the original remote, and press
   **Learn command**. Press **Send command** to replay it.

## Documentation

- [Hardware](docs/hardware.md) — supported hardware, wiring, capture tuning
- [Configuration](docs/configuration.md) — every option, action, trigger and entity
- [Architecture](docs/architecture.md) — how it works and why it is built this way
- [Troubleshooting](docs/troubleshooting.md) — when something does not behave

## Tests

The capture validation and storage logic build and run on the host, without ESPHome or hardware:

```sh
sh tests/run.sh
```

## Licence

MIT. See [LICENSE](LICENSE).
