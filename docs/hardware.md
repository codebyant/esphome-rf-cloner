# Hardware

## Supported hardware

- **ESP32** with RMT (the original ESP32, S2, S3, C3, C6, H2, P4). The ESP32-C2 and C61 use
  ESPHome's software receiver, which works but has not been tested here.
- **CC1101** module for the band you need — 315, 433, 868 or 915 MHz. A 433 MHz module with an SMA
  antenna is the common choice in Europe and South America.
- ESPHome **2026.1 or newer**. The `cc1101` component landed in 2025.12.

Other RF front-ends can work in principle: `rf_cloner` only talks to `remote_receiver` and
`remote_transmitter`. Substitute the chip's own TX/RX state actions for `cc1101.begin_tx` and
`cc1101.begin_rx`.

## Wiring

**Power the CC1101 from 3.3 V.** It is not 5 V tolerant; VIN will damage it.

| CC1101 | ESP32 | Role |
|---|---|---|
| VCC | 3V3 | |
| GND | GND | |
| CSN | GPIO5 | `cc1101.cs_pin` |
| SCK | GPIO18 | `spi.clk_pin` |
| MOSI | GPIO23 | `spi.mosi_pin` |
| MISO | GPIO19 | `spi.miso_pin` |
| GDO0 | GPIO22 | `remote_transmitter.pin` |
| GDO2 | GPIO4 | `remote_receiver.pin` |

This is ESPHome's recommended *dual pin* topology: the CC1101 only accepts transmit data on GDO0,
so GDO0 goes to the transmitter and GDO2 carries received data. `gdo0_pin:` is left unset — it is
needed only for packet mode or single-pin switching.

GPIO5 is a strapping pin on the ESP32. It works as chip select, but if the board misbehaves at
boot, move CS to another pin first.

Any GPIO can be substituted; the numbers above are simply what the reference configuration uses.

## Verifying the wiring

Flash [`config/hardware-check.yaml`](../config/hardware-check.yaml) before using the component. It
uses only upstream ESPHome components and does three things:

1. Probes the CC1101 over SPI. A successful boot logs `CC1101 found! Chip ID: 0x0014`.
2. Dumps raw captures, so you can confirm a held remote button produces clean framed pulses.
3. Provides a button that replays a pasted frame, confirming the transmit path drives your device.

Once all three work, move to [`config/rf-bridge.yaml`](../config/rf-bridge.yaml).

## Capture tuning

Two receiver options matter more than anything else, and the right values depend on the remote you
are learning. Read them off the `dump: raw` output from the hardware check.

### `filter`

Pulses shorter than `filter` are merged into the preceding one. Set it **below the shortest pulse
your remote emits**. ESPHome's general RF guide suggests `250us`, which is too coarse for remotes
using sub-250 µs symbols — the reference configuration uses `100us`.

Too high and short pulses vanish from the capture.

### `idle`

A capture ends after `idle` of silence. The right value depends on which gap strategy you want,
and the two configurations differ deliberately:

| File | `idle` | Behaviour |
|---|---|---|
| `hardware-check.yaml` | `4ms` | Each repeat framed separately — easy to read in the log |
| `rf-bridge.yaml` | `30ms` | Several repeats per window, so the gap is measured off the waveform |

For learning, set `idle` **above** the remote's inter-frame gap but **below** the pause between
separate button presses. The ESP32's RMT caps it at 65536 µs. See
[architecture.md](architecture.md) for why this matters.

If you do not know the gap yet, run the hardware check with `idle: 4ms` and read the interval
between frames from the log timestamps, then set `idle` comfortably above it.

### CC1101 settings

`symbol_rate` and `filter_bandwidth` affect what the chip's data slicer emits before ESPHome ever
sees it. The defaults in the reference configuration (5000 baud, 203 kHz) suit typical 433 MHz
OOK remotes. If short pulses are mangled or missing and lowering `filter` did not help, try a
lower `symbol_rate`.

## Building on Windows

Two environment constraints are worth knowing before the first build:

- **ESP-IDF's tool installer refuses to run under MSYS/MinGW**, so ESPHome cannot be invoked from
  Git Bash. Use PowerShell or another native shell.
- **The default toolchain path can exceed the 260-character Windows limit**, producing errors like
  `fatal error: bits/c++config.h: No such file or directory`. Either enable long path support and
  reboot, or point the toolchain somewhere short:

```powershell
$env:ESPHOME_ESP_IDF_PREFIX = 'C:\ESPHome\idf'
esphome compile config\rf-bridge.yaml
```
