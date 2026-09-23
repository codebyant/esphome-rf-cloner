# Contributing

Bug reports, wiring questions and "my remote does not work" reports are all welcome — the last one
especially, because the only way this project learns which remotes it handles is people trying it.

## Scope

**In scope**

- Learning and replaying fixed-code sub-GHz remote commands.
- The on-device command registry: storage, identity, backup, restore.
- Managing all of that from Home Assistant.
- Support for other OOK bands and other RF front-ends, where the change stays inside
  `remote_receiver` / `remote_transmitter`.

**Out of scope**

- **Protocol decoding.** The whole point is not needing to know what a waveform means. ESPHome's
  `remote_receiver` already decodes the protocols it knows.
- **Rolling-code targets.** Not implementable by replay.
- **Infrared.** ESPHome's own IR components cover it.
- **A custom Lovelace card.** The integration deliberately uses native Home Assistant UI. If you
  hit a UX limit native HA genuinely cannot solve, open an issue describing the limit before
  writing a frontend.

## Layout

```
components/rf_cloner/       the ESPHome external component (C++ and codegen Python)
packages/                   includable ESPHome YAML: controls and Home Assistant actions
config/                     reference configurations and the hardware bring-up check
custom_components/rf_cloner/  the Home Assistant integration
tests/                      host tests for both halves
docs/                       reference documentation: everything a user needs
docs/guides/                step-by-step tasks, with screenshots
assets/                     the integration icon sources and documentation screenshots
```

## Running the tests

Neither suite needs ESPHome, Home Assistant or radio hardware. Both run on a plain toolchain.

```sh
sh tests/run.sh          # C++: capture validation and the command store
sh tests/run_python.sh   # Python: restore orchestration and identity safety
```

The C++ suite needs a C++20 compiler. On Windows, MSYS2's `g++` works; ESP-IDF is not involved.

```sh
CXX=/c/msys64/mingw64/bin/g++ sh tests/run.sh
```

Both must stay green. They cover the paths that cannot be rehearsed against a live bridge without
destroying its registry, which is exactly why they exist.

## Validating the ESPHome side

```sh
esphome config config/rf-bridge.yaml
esphome config config/hardware-check.yaml
```

This runs the component's own configuration schema and code generation, which the host tests do
not reach. Any change under `components/`, `packages/` or `config/` should pass it.

On Windows, ESP-IDF's installer refuses to run under MSYS/MinGW, so use PowerShell — see
[docs/hardware.md](docs/hardware.md#building-on-windows).

## Testing the Home Assistant integration

Copy `custom_components/rf_cloner/` into a Home Assistant `config/custom_components/` and restart.
There is no fixture harness; `tests/ha_stubs.py` stubs only the handful of Home Assistant types the
pure logic borrows, and is deliberately not a Home Assistant simulator. Anything that needs real
config entry, entity or device registry behaviour has to be exercised against a running instance.

Worth walking through by hand for anything touching the flows: add a command, rename it, delete it,
reload the entry, restart Home Assistant.

## Changes to the RF path

Learning, capture validation, gap measurement, replay and storage **need a hardware run**. The host
tests will not catch a regression that only shows up against a real remote and a real target.

Say in the pull request which remote and which target you tested against, and what the learn log
reported for the inter-frame gap.

## What is settled

Storage format, command and bridge identity, the revision domain, the backup and restore protocol,
and Home Assistant reconciliation are done and validated end to end. They are not closed to change,
but changing them needs a concrete reason — a reproducible bug, a violated invariant, a current
Home Assistant, HACS or ESPHome requirement, or a data-loss problem — not that a different shape
would be tidier. Open an issue with the concrete case before a redesign.

In particular: there is no tombstone architecture, and the Home Assistant snapshot is a faithful
mirror rather than an undo buffer. Both are deliberate.

## Style

Match what is already there. The codebase favours comments that explain *why* a thing is the way it
is over comments that restate the code, and it is consistent about it — a patch that reads
differently from its surroundings will get that comment in review.

- C++: C++20, ESPHome conventions, no exceptions, no dynamic allocation in the capture path.
- Python (component): ESPHome codegen conventions.
- Python (integration): Home Assistant conventions — `async_` prefixes, `_LOGGER`, typed config
  entries, translated strings. Every user-visible string belongs in `strings.json` **and**
  `translations/en.json`; CI fails if the two disagree.

## Pull requests

- One change per pull request.
- Tests green, and a new test for anything that was a bug.
- Documentation updated in the same pull request, not a follow-up.
- A change to a dialogue's wording or layout updates the matching screenshot in
  `assets/screenshots/`, keeping its file name — see
  [its index](assets/screenshots/README.md).
- No version bumps. Releases are cut separately.

## Reporting a problem

[Open an issue](https://github.com/codebyant/esphome-rf-cloner/issues). For anything involving
Home Assistant, attach the diagnostics download from the bridge's device page — it carries no
waveforms, credentials or unrelated configuration.

[docs/troubleshooting.md](docs/troubleshooting.md) covers most of what gets reported.
