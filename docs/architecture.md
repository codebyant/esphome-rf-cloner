# Architecture

`rf_cloner` is an ESPHome external component that learns arbitrary sub-GHz OOK remote commands at
runtime, stores them on the device under a name, and replays them on demand.

ESPHome already provides the radio driver (`cc1101`), raw capture and replay (`remote_receiver`,
`remote_transmitter`) and persistent storage (`global_preferences`). Home Assistant provides a
`radio_frequency` transmitter entity domain. Neither provides RF *learning* or a store for learned
codes. This component is that layer and nothing more.

```
remote_receiver ──on_receive──▶ RfCloner ──transmit──▶ remote_transmitter
                                   │
                                   ├── CaptureValidator   frame extraction and validation
                                   └── CommandStore       persistent named registry
```

## Capture and validation

A capture is accepted only when every condition holds:

| Check | Option |
|---|---|
| Pulse count within range | `min_pulses`, `max_pulses` |
| Every pulse duration within range | `min_pulse_length`, `max_pulse_length` |
| Arrived before the deadline | `learn_timeout` |
| Enough agreeing repeats | `min_repeats`, `tolerance` |

**Agreement between repeats is the noise gate.** A held remote emits many identical frames; noise
does not repeat itself. Two frames agree when they match in length, in mark/space polarity at
every position, and in per-pulse duration within `tolerance`.

RSSI would be the obvious alternative gate, but ESPHome's `cc1101` exposes RSSI only through the
packet-mode `on_packet` trigger, not in the async OOK mode used here. Shape-based validation is
also protocol-agnostic, where an RSSI threshold is really a proximity heuristic.

Frames that fail validation never abort a learn in progress — they are ignored and the component
keeps waiting until the timeout. A failed learn writes nothing, so existing commands cannot be
damaged by a bad attempt.

### The terminator

`remote_receiver` appends one entry to every capture that is not part of the waveform. On the
ESP32 RMT path it pushes a synthetic value equal to the configured `idle:`; the software path
returns the terminating silence. It is stripped before anything is stored or replayed.

## The inter-frame gap

Replay repeats each frame `repeat_times` over, separated by the gap the original remote used. That
gap is absent from a capture containing a single frame, because `remote_receiver` replaced it with
the terminator. The component obtains it two ways.

### Measured from the waveform (preferred)

Set the receiver's `idle:` **above** the target's inter-frame gap. `remote_receiver` then stops
framing each repeat separately and returns a window holding several, with the gap between them
recorded as an ordinary space that the RMT peripheral timed at 1 µs resolution:

```
[ frame … , -9443, frame … , -9443, frame … , -terminator ]
```

`split_frames()` cuts the window at every space of at least `frame_gap_min`, and the gap between
two *agreeing* neighbours is stored directly. Windows that open or close mid-transmission yield
partial frames, which fail the agreement check and are skipped.

This costs nothing: `rf_cloner` is the only listener on its receiver and only listens while
learning, so that receiver's framing is effectively the learning configuration. The ESP32's RMT
caps `idle` at 65536 µs, and `idle` must also stay below the pause between separate button
presses.

### Inferred from arrival times (fallback)

When a window holds one frame, the component estimates the gap from when frames arrive, and logs a
warning recommending a larger `idle`.

That path needs care. `on_receive` runs from `loop()`, so every arrival timestamp is quantised to
a loop tick — roughly 16 ms with WiFi and `web_server` active — and individual deltas snap to
multiples of it. Against a remote with a ~41 ms repeat period, consecutive deltas measured
bimodally at ~33 ms and ~50 ms. **A median of those deltas selects one quantisation mode and can
be wrong by milliseconds**, enough for a target device to reject the replay. Averaging across the
whole observation window bounds the error at one loop period divided by the interval count, which
is why `settle_time` exists and why a longer button hold produces a better result.

If neither path yields a plausible value, `default_gap` is used.

## Storage

One `CommandStore` per component instance, backed by ESPHome preferences (NVS on ESP32).

```
header  (10 bytes, fixed)        magic, format_version, slot_count, index_bytes, crc
index   (28 + slot_count*44 + 2) registry metadata, one record per slot, then a crc
payload (pulse_count * 4)        one record per occupied slot, int32 timings
```

Three record kinds because ESPHome's preference `load()` only succeeds on an exact length match:
the fixed-size header must be read first to learn the lengths of the index and of each payload.
Padding every slot to `max_pulses` instead would cost about 1 kB each and exhaust the ~20 kB NVS
budget long before the slot count did.

The header never changes size or layout across formats. It carries the format version and the
exact length of the index, so a build always knows whether the rest is something it understands.

Per command the store holds an id, the name, the timings, `gap_us`, `repeat_times`,
`frequency_hz` and the modulation. Nothing assumes a particular pulse count, bit count, repeat
count or frequency.

### Identity

The 28-byte metadata block at the head of the index holds:

| Field | Meaning |
| --- | --- |
| `bridge_id` | 16 random bytes minted once, identifying the logical bridge rather than the board. Independent of MAC, IP and node name, so a replacement ESP32 can be restored into the same logical bridge. |
| `next_command_id` | Monotonic `uint32`. Never rewound, so an id is never handed out twice. |
| `revision` | Incremented once per committed mutation. Starts at 1; 0 never reaches flash. |
| `restore_state` | 1 while a restore is in progress. |

A command is identified by its `command_id`, which is immutable. The name is mutable metadata:
renaming addresses the command by id and rewrites only the index, never the waveform. Relearning
under an existing name overwrites that command in place and keeps its id, so anything built on the
id survives. Deleting frees the slot but not the id, and `clear_all` keeps `bridge_id` and
`next_command_id` — clearing the commands does not make this a different bridge.

`revision` advances once per committed mutation and only when something actually changed:
renaming a command to the name it already has, or clearing a registry that is already empty with
no restore open, both succeed without writing. A reader can therefore treat a changed `revision`
as a real change. Clearing *does* write when a restore is open, even with nothing imported yet,
because abandoning the restore is itself a change.

Identity and slots live in the same record because assigning an id and advancing
`next_command_id` must commit together; a separate metadata record would leave a window where a
crash could hand the same id out twice.

**Failure behaviour**

Storage the firmware cannot interpret is never rewritten. Each of these leaves every persisted
record exactly as found and makes the session read-only:

| Fault | Cause |
| --- | --- |
| `header_invalid` | Bad magic or CRC, or an index length this format does not use. |
| `index_invalid` | Index record missing or failed its CRC. |
| `version_unsupported` | A `format_version` this build does not write. |
| `payload_invalid` | An occupied slot's waveform is missing, the wrong length, or failed its CRC. |

In read-only mode the commands that did load stay readable and sendable, and every mutation is
refused — including `clear_all` — so nothing rewrites the index over data that may still be
recoverable. `factory_reset` is the single exception: it discards the registry, mints a new
`bridge_id` and writes a clean empty one. It is the only way out, and it is always explicit.

A slot the current `max_commands` or `max_pulses` can no longer hold is reported separately and is
not a fault: the record is intact and restoring the previous configuration brings it back.

Other guarantees:

- A storage write that fails rolls back the in-RAM state, including `revision` and
  `next_command_id`, so the two never diverge.
- `max_storage_bytes` is enforced before writing, not discovered by a failed write.
- During a restore, only restore operations and an explicit clear are accepted.

Instances on one device share a single NVS namespace, so each salts its preference keys with a
hash of its YAML id.

Handles are cached per (key, length): `ESP32Preferences::make_preference()` allocates on every
call and `ESPPreferenceObject` never frees it, so creating one per save would leak.

## Replay

```
send(name) → look up the command
           → remote_transmitter transmit_raw(timings)
             repeat.times = repeat_times, repeat.wait_time = gap_us
           → on_transmit fires cc1101.begin_tx, on_complete fires cc1101.begin_rx
```

The radio turnaround is declarative, wired in YAML through `remote_transmitter`'s triggers. The
transmitter must have `carrier_duty_percent: 100%` — the CC1101 handles modulation itself and an
IR carrier duty cycle would corrupt the waveform. The component rejects any other value during
config validation.

## Control surface

**ESPHome fixes its entity list at compile time.** Entities are built during code generation and
sent to the client in `ListEntities`; there is no API to register one after boot. A learned
command therefore cannot become a Home Assistant entity of its own.

The resolution is that the *controls* are static even though the commands are not: one `text`
entity holding a command name, plus `button` entities for learn, send, delete and cancel, act on
any command by name. Those render automatically both on the Home Assistant device page and in
ESPHome's built-in `web_server`, so no custom frontend exists or is needed.

`api: actions:` provides the same operations for automations, plus the id-addressed ones the
static controls cannot express. `rf_status` is the canonical structured read: it returns the
registry identity, counters and one object per command via `supports_response: only`.

A `select` listing stored names is deliberately not used: ESPHome sends a select's option list
only in `ListEntities`, so it would be correct at boot and stale thereafter.

## Design decisions

### The device owns the store

Home Assistant's `radio_frequency` domain is **transmit-only**. Its entity base class is
`RadioFrequencyTransmitterEntity`, it has no storage module and no learn action, and HA's ESPHome
platform filters entities to `RadioFrequencyCapability.TRANSMITTER`, so a receiver-capable proxy
instance produces no HA entity at all. Upstream's design keeps protocol encoders and device code
sets in the external `rf-protocols` library, with receiver support deferred and RF learning
explicitly out of scope.

That model suits control of *known* protocols. It cannot express learning an arbitrary waveform
from a physical remote, and there is nothing upstream to delegate a learned-code store to. Keeping
the store on the device also means replay works with Home Assistant offline and survives a reboot
without anything re-sending the waveforms.

Should Home Assistant later gain receiver support and a code library, the exit is open: the store
is not entangled with the transport, and a transmit-only proxy entity can be added alongside
without touching the component.

### `ir_rf_proxy` is not used

ESPHome's `ir_rf_proxy` bridges `remote_transmitter` / `remote_receiver` to the `infrared` and
`radio_frequency` entity domains. It is not part of this component for three reasons: an RF
*receiver* proxy instance produces no Home Assistant entity, so it cannot carry a learning flow;
it is flagged EXPERIMENTAL upstream and exempt from the normal breaking-change policy; and
infrared is out of scope here.

Adding a transmit-only `radio_frequency` proxy alongside `rf_cloner` is possible and does not
conflict with it.

## Limitations

- **Rolling-code devices cannot be controlled by replay.** Any device whose transmission changes
  between presses is outside what a raw learn-and-replay bridge can do.
- **Learned commands are not individual entities**, for the reason above.
- **Storage is bounded** by the NVS partition, around 20 kB in ESPHome's default layout.
- **One frequency per component instance.** `frequency` is recorded with each command as metadata
  but does not retune the radio at replay time.
- **`idle:` must sit between the target's inter-frame gap and its inter-press pause** for direct
  gap measurement. Outside that range the component falls back to the estimator.
