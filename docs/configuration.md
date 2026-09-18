# Configuration reference

Every option, action, trigger and entity of the `rf_cloner` component. For how it works, see
[architecture.md](architecture.md); for wiring and capture tuning, [hardware.md](hardware.md).

A complete working configuration is [`config/rf-bridge.yaml`](../config/rf-bridge.yaml).

```yaml
external_components:
  - source:
      type: local
      path: ../components
    components: [rf_cloner]

rf_cloner:
  id: cloner
  receiver_id: rf_rx
  transmitter_id: rf_tx
```

`remote_receiver` and `remote_transmitter` are both required. The transmitter must have
`carrier_duty_percent: 100%` — anything else is rejected at config validation, because the RF
front-end does its own modulation and an IR carrier would corrupt the waveform.

## Configuration

### Storage

| Option | Default | Meaning |
|---|---|---|
| `max_commands` | `16` | Number of slots. Changing it later is detected and logged, not silently destructive |
| `max_pulses` | `128` | Longest waveform that may be stored, in pulses |
| `max_storage_bytes` | `12288` | Hard budget. `put` refuses with `budget_exceeded` before this is crossed |

Bytes used are `10 + max_commands * 40 + 2` for the index, plus `4 * pulse_count` per stored
command. A 65-pulse command costs 260 bytes. Raising `max_pulses` much past the default makes
`max_commands * max_pulses * 4` exceed `max_storage_bytes`; that is legal (the byte budget is the
real cap, and `put` refuses with `budget_exceeded` before it is crossed) but it logs a notice at
build time so the ceiling is not a surprise. The practical ceiling is ESPHome's NVS partition,
around 20 kB; `dump_config` prints the worst case at boot.

Command names are at most 23 characters from `A-Z a-z 0-9 _ - .`.

### Capture validation

| Option | Default | Meaning |
|---|---|---|
| `min_pulses` | `8` | Frames shorter than this are ignored as noise |
| `min_pulse_length` | `100us` | Keep below the shortest real pulse of your remote |
| `max_pulse_length` | `100ms` | |
| `tolerance` | `25%` | Per-pulse tolerance when comparing frames, same notion as `remote_receiver`'s |
| `min_repeats` | `3` | Agreeing frames required before a capture is accepted |
| `frame_gap_min` | `3ms` | Splits a receive window into repeated frames. Must exceed the longest space *inside* a frame and stay below the gap *between* frames |
| `settle_time` | `800ms` | After `min_repeats` is reached, keep collecting this long. Directly controls gap accuracy |
| `learn_timeout` | `15s` | |

`min_repeats` is the noise gate: a held remote emits many identical frames, noise does not repeat
itself. Frames that fail validation never abort a learn in progress — they are ignored and the
bridge keeps waiting.

### Replay defaults

| Option | Default | Meaning |
|---|---|---|
| `default_repeat_times` | `20` | Transmissions per send |
| `default_gap` | `10ms` | Used when the inter-frame gap cannot be measured |
| `min_gap` / `max_gap` | `1ms` / `200ms` | Plausibility bounds on the measured gap |
| `frequency` | `433.92MHz` | Recorded as metadata with each capture; does not tune hardware |

### How the inter-frame gap is obtained

The gap is stored per command and obtained by one of three routes, in order of preference.

**Measured from the waveform.** With the receiver's `idle:` above the target's inter-frame gap,
several repeats arrive in one window and the gap is read directly:

```
waveform gap: 9443 us (frames 1/5 in this window)
Measured inter-frame gap: 9443 us (from the waveform, 4 sample(s))
```

The per-sample lines at `DEBUG` show whether `frame_gap_min` is splitting correctly — clustered
values mean it is.

**Inferred from arrival times.** With `idle:` below the gap, each window holds one frame and the
component estimates, logging a warning that recommends raising `idle`. The estimate carries a few
hundred microseconds of error.

**`default_gap`**, when neither produces a plausible value.

Override per send with an explicit `gap:` on `rf_cloner.send`.

## Actions

```yaml
- rf_cloner.learn:            # name (templatable, required), timeout (templatable, optional)
    id: cloner
    name: my_command
- rf_cloner.send:             # name required; repeat_times and gap override the stored values
    id: cloner
    name: my_command
- rf_cloner.delete:
    id: cloner
    name: my_command
- rf_cloner.cancel: cloner
- rf_cloner.clear_all: cloner
```

Learning under an existing name overwrites it, which is the intended way to relearn a command.

## Triggers

`on_learn_started`, `on_learn_success`, `on_learn_failed`, `on_send`. Each passes a
`std::string x`: the command name, except for `on_learn_failed`, where it is the failure reason.

Failure reasons: `timeout`, `no_repeat_agreement`, `too_few_pulses`, `too_many_pulses`,
`pulse_out_of_range`, `cancelled`, plus `name_rejected:*`, `store_full` and `store_error:*`.

## Entities

```yaml
text_sensor:
  - platform: rf_cloner
    rf_cloner_id: cloner
    state:
      name: "Learn state"       # idle / armed / captured / failed
    last_result:
      name: "Last result"       # the learned name, or the failure reason

sensor:
  - platform: rf_cloner
    rf_cloner_id: cloner
    command_count:
      name: "Stored commands"
    storage_used:
      name: "Storage used"
```

`captured` and `failed` persist until the next learn, so the outcome stays visible.

There are no per-command entities: ESPHome cannot create entities at runtime. Pair these with a
`template.text` for the command name and `template.button`s for learn, send and delete, as in the
reference configuration.

## Failure behaviour

- A failed or noisy learn writes nothing. Existing commands cannot be damaged by it.
- A slot whose payload fails its CRC, or whose record cannot be read, is dropped from the in-RAM
  view and logged. Flash is left untouched until the user changes something.
- An unknown `format_version` on flash is refused rather than reinterpreted; the device starts
  empty and the stored bytes survive for a downgrade.
- A storage write that fails rolls back the in-RAM state too, so the two never diverge.

Covered by the host tests in [`tests/`](../tests); run `sh tests/run.sh`.

## Multiple instances

`rf_cloner` is `MULTI_CONF`. Each instance salts its preference keys with its YAML id, so two
bridges on one device keep separate stores.
