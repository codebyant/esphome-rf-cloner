## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## How it was verified

<!-- Tick what applies; delete the rest. CONTRIBUTING.md has the commands. -->

- [ ] `sh tests/run.sh` (firmware)
- [ ] `sh tests/run_python.sh` (integration)
- [ ] `esphome config config/rf-bridge.yaml`
- [ ] Flashed and exercised on real hardware
- [ ] Loaded in a running Home Assistant

**RF path changes** — learning, capture validation, gap measurement, replay, storage — need a
hardware run, not only the host tests. Say which remote and target you tested against.
