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

Bytes used are `10 + 28 + max_commands * 44 + 2` for the header and index — 744 bytes at the
default `max_commands: 16` — plus `4 * pulse_count` per stored command. A 65-pulse command costs
260 bytes. Raising `max_pulses` much past the default makes
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

## Packages

The control entities and the Home Assistant actions ship as includable packages, so a device
configuration does not have to copy them. Include either, both, or neither.

```yaml
packages:
  rf_controls: !include
    file: packages/controls.yaml
    vars:
      rf_cloner_id: cloner
  rf_actions: !include
    file: packages/api-actions.yaml
    vars:
      rf_cloner_id: cloner
```

Or straight from the repository, without vendoring anything:

```yaml
external_components:
  - source: github://codebyant/esphome-rf-cloner
    components: [rf_cloner]

packages:
  rf_bridge:
    url: https://github.com/codebyant/esphome-rf-cloner
    files:
      - path: packages/controls.yaml
        vars: {rf_cloner_id: cloner}
      - path: packages/api-actions.yaml
        vars: {rf_cloner_id: cloner}
    refresh: 1d
```

Both packages require an `rf_cloner:` component with a matching id, and a `remote_receiver` and
`remote_transmitter` for it to use. Neither contains secrets, so both work as remote packages.

### `packages/controls.yaml`

Defines the `text` entity holding the command name, `button` entities for learn, send, delete and
cancel, and the `text_sensor` and `sensor` diagnostics.

| Variable | Default | Meaning |
|---|---|---|
| `rf_cloner_id` | `cloner` | The component these controls drive |
| `rf_name_id` | `rf_command_name` | id of the command-name text entity |
| `rf_prefix` | *(empty)* | Prepended to every entity name |

### `packages/api-actions.yaml`

Defines the actions below, callable from Home Assistant as `esphome.<node>_rf_learn` and so on.
Merges with an existing `api:` block, so a device's own actions are kept.

| Action | Arguments | Purpose |
|---|---|---|
| `rf_status` | — | Canonical structured read: identity, counters, one object per command |
| `rf_learn` | `name` | Arm learning under a name |
| `rf_cancel` | — | Abort an armed learn |
| `rf_send` | `name` | Replay by name |
| `rf_send_id` | `command_id` | Replay by immutable id, unaffected by a rename |
| `rf_rename` | `command_id`, `name` | Rename; keeps the id and the waveform |
| `rf_delete` | `name` | Delete by name; the id is not recycled |
| `rf_clear` | — | Drop every command, keeping `bridge_id` and `next_command_id` |
| `rf_factory_reset` | — | Discard the registry and its identity; the only escape from read-only |
| `rf_export` | `command_id` | One command as a portable JSON record, waveform included |
| `rf_restore_begin` | `bridge_id`, `next_command_id` | Adopt an identity and open a restore |
| `rf_import` | `command_id`, `name`, `timings`, `gap_us`, `repeat_times`, `frequency_hz`, `modulation` | Write one exported command back under its original id |
| `rf_restore_commit` | — | Close the restore |
| `rf_list` | — | Older name-only read, superseded by `rf_status` |

### Export and restore

`rf_export` returns everything needed to reproduce one command on another bridge, and nothing
else:

```json
{
  "schema": 1,
  "found": true,
  "bridge_id": "b7397c870440b9228377fbdb4ed95624",
  "command_id": 2,
  "name": "probe_2",
  "pulses": 65,
  "gap_us": 8669,
  "repeat_times": 20,
  "frequency_hz": 433920000,
  "modulation": 0,
  "timings": [242, -744, 244, -739, ...]
}
```

`schema` versions this payload independently of the on-flash format, so the two evolve separately.
`modulation` is the stored code (`0` = OOK) rather than a label, so an export round-trips exactly.
One command per call keeps each response around a kilobyte, well inside the API message limit.

Restoring is explicit and three-phase, so a half-finished restore is visible rather than silently
partial:

```
rf_restore_begin(bridge_id, next_command_id)
rf_import(...)   once per command
rf_restore_commit()
```

`rf_restore_begin` is accepted only on an empty registry — that is what keeps a stale backup from
overwriting a live bridge — and rejects a `bridge_id` that is not exactly 32 hex characters.
Between begin and commit, `rf_status` reports `restore_incomplete: true` and normal mutations are
refused; `rf_clear` abandons the attempt, keeping the adopted identity so it can simply be
replayed. An interrupted restore survives a reboot as `restore_incomplete`, and replaying it from
the same export is the recovery path.

| Variable | Default | Meaning |
|---|---|---|
| `rf_cloner_id` | `cloner` | The component these actions drive |
| `rf_action_prefix` | `rf_` | Prepended to every action name |

`rf_status` is the canonical read. It uses `supports_response: only` and returns the registry
identity, its counters and one object per command:

```yaml
action: esphome.rf_bridge_rf_status
response_variable: rf
```

```json
{
  "bridge_id": "b7397c870440b9228377fbdb4ed95624",
  "revision": 5,
  "next_command_id": 3,
  "restore_incomplete": false,
  "read_only": false,
  "fault": "none",
  "state": "idle",
  "last_result": "porch_light",
  "count": 2,
  "max_commands": 16,
  "max_pulses": 128,
  "used_bytes": 1172,
  "capacity_bytes": 12288,
  "commands": [
    {"id": 1, "name": "porch_light", "pulses": 65},
    {"id": 3, "name": "gate_open", "pulses": 50}
  ]
}
```

`read_only` and `fault` report storage the firmware refuses to interpret; see
[Failure behaviour](#failure-behaviour). `rf_list` remains as the older name-only read, returning
`commands` as a plain array of names with no ids or identity; prefer `rf_status`.

### Two instances on one device

`rf_cloner` is `MULTI_CONF`, and each instance salts its preference keys with its YAML id, so the
stores stay separate. Give each its own ids, entity prefix and action prefix:

```yaml
packages:
  garage_controls: !include
    file: packages/controls.yaml
    vars:
      rf_cloner_id: garage
      rf_name_id: garage_command_name
      rf_prefix: "Garage "
  garage_actions: !include
    file: packages/api-actions.yaml
    vars:
      rf_cloner_id: garage
      rf_action_prefix: garage_
```

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
- Storage the firmware cannot interpret is never rewritten. A bad header or index, an unknown
  `format_version`, or an occupied command whose waveform is missing or fails its CRC all leave
  every record exactly as found and make the session **read-only**: whatever loaded stays
  readable and sendable, and every mutation is refused. `rf_status` reports this as `read_only`
  with a `fault` of `header_invalid`, `index_invalid`, `version_unsupported` or `payload_invalid`.
- `rf_factory_reset` is the only way out of read-only mode. It discards the registry, mints a new
  `bridge_id` and writes a clean empty one, touching only this component's own records.
- A slot the current `max_commands` or `max_pulses` can no longer hold is logged but is not a
  fault; the record is intact and restoring the previous configuration brings it back.
- A storage write that fails rolls back the in-RAM state too, so the two never diverge.

Covered by the host tests in [`tests/`](../tests); run `sh tests/run.sh`.
