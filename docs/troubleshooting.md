# Troubleshooting

## The CC1101 is not detected

The boot log should contain `CC1101 found! Chip ID: 0x0014`. A chip ID of `0xFF0F`, `0x0000` or
`0xFFFF` means SPI is not working — check MISO, MOSI, SCK and CS, and confirm the module is on
3.3 V rather than VIN.

## Learning times out

`Last result` shows `timeout` and nothing was stored.

- **No frames reached the component at all.** Run `config/hardware-check.yaml` and confirm a held
  button produces raw captures. If not, the problem is the receiver, not the learning logic.
- **Frames arrived but were rejected.** The log shows `Ignoring window of N pulses: <reason>` at
  `VERBOSE`. `too_few_pulses` or `pulse_out_of_range` usually means `filter` is too high and short
  pulses are being swallowed; `too_many_pulses` means `max_pulses` is below what your remote sends,
  or `frame_gap_min` is too high to split a multi-frame window.

## Learning reports `no_repeat_agreement`

Frames arrived and passed validation but never matched each other within `tolerance`.

- Hold the button longer. `min_repeats` agreeing frames are required.
- Raise `tolerance` if the remote's timing is unusually loose.
- Some devices use **rolling codes** and genuinely transmit something different each press. Those
  cannot be controlled by learn-and-replay at all.

## The command is learned but does not drive the device

The log confirms `Sent '<name>': N pulses xM, gap G us`, so the waveform was transmitted.

**Check the gap first.** Look at the learn log:

```
Measured inter-frame gap: 9443 us (from the waveform, 4 sample(s))
```

- If it says **`from the waveform`**, the gap was measured directly and is likely correct.
- If it says **`Estimated ... from arrival times`**, raise the receiver's `idle` above the
  target's inter-frame gap and relearn. The estimate carries systematic error.
- If it says **`falling back to the configured default`**, neither method worked; set
  `default_gap` to a plausible value for your device and relearn.

To test a different gap without relearning, pass one explicitly:

```yaml
- rf_cloner.send:
    id: cloner
    name: my_command
    gap: 8ms
```

**Check the transmit path independently** by replaying a known-good frame with
`remote_transmitter.transmit_raw`, as `config/hardware-check.yaml` does. If that also fails to
drive the device, the problem is the radio or antenna rather than the stored command.

## Gap readings are scattered

The `DEBUG` log prints each measurement:

```
waveform gap: 9421 us (frames 2/6 in this window)
waveform gap: 9448 us (frames 3/6 in this window)
```

Values within a few tens of microseconds of each other indicate `frame_gap_min` is splitting
correctly. Widely scattered values mean it is cutting in the wrong places — raise it above the
longest space *inside* a frame, keeping it below the gap *between* frames.

## Commands disappear after a reboot

The boot log reports what was found:

- `No stored commands yet (first boot); bridge id X` — nothing was ever written, so a fresh
  identity was minted and persisted.
- `Loaded N stored command(s); bridge id X, revision R` — a healthy registry.
- `N stored command(s) have an unreadable waveform` — the index still names them, so the session
  is read-only and nothing rewrites over them. See below.
- `Stored data is not readable by this firmware` — a bad header or index, or a `format_version`
  this build does not write. Flash is left intact and the session is read-only.
- `A restore was interrupted before it completed` — replay the restore, or clear to abandon it.
- `max_commands changed (stored N, configured M)` — reducing `max_commands` or `max_pulses` leaves
  commands that no longer fit out of the in-RAM view. Their records are intact; restoring the
  previous values brings them back.

### The device booted read-only

Any storage this firmware refuses to interpret puts the session into read-only mode. Commands that
loaded stay readable and sendable, and every mutation — including `rf_clear` — is refused, so
nothing overwrites data that may still be recoverable. `rf_status` reports it:

```json
{"read_only": true, "fault": "payload_invalid"}
```

| `fault` | Meaning |
|---|---|
| `header_invalid` | Bad magic or CRC, or an index length this format does not use |
| `index_invalid` | Index record missing or failed its CRC |
| `version_unsupported` | A `format_version` this build does not write |
| `payload_invalid` | An occupied command's waveform is missing, the wrong length, or failed its CRC |

Recovery is deliberate and destructive: `rf_factory_reset` discards the registry, mints a new
`bridge_id` and writes a clean empty one. It touches only this component's own records, so other
ESPHome state is unaffected. There is no partial-salvage path — everything still stored is lost,
so export anything you can send first.

If learning reports success but nothing survives, check for `Writing N items` in the log after a
learn — its absence means the preference write never happened.

## Learning refuses to start

- `name_rejected:name_empty` — the command name is blank.
- `name_rejected:name_invalid` — names accept `A-Z a-z 0-9 _ - .` only.
- `name_rejected:name_too_long` — 23 characters maximum.
- `store_full` — every slot is occupied. Delete a command or raise `max_commands`.

Learning under an existing name overwrites it and is the intended way to relearn.

## `budget_exceeded` when learning

The capture would push storage past `max_storage_bytes`. Raise it if the NVS partition has room,
lower `max_pulses`, or delete commands. `dump_config` prints current and worst-case usage at boot.

## Flashing fails with a permission error

`PermissionError(13, 'Access denied')` on the serial port with no process holding it is usually a
stale driver handle. Unplug and replug the USB cable.

Note that starting `esphome logs` over serial resets the device.
