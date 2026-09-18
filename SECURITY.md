# Security

## Reporting

Report a vulnerability through
[GitHub's private advisory form](https://github.com/codebyant/esphome-rf-cloner/security/advisories/new),
not a public issue. Expect a reply within a week.

## Supported versions

Pre-release: only the latest release is supported. There are no backports.

## What this project is responsible for

RF Cloner records and replays raw radio waveforms. A few consequences are worth stating plainly,
because they are properties of the design rather than defects:

- **A learned command is a replayable credential.** Anything that can press the button — a Home
  Assistant automation, the bridge's web page, anyone on your network who can reach either — can
  operate the target. Fixed-code remotes have no authentication; that is why they can be cloned at
  all, by this project and by a £20 device off the internet.
- **The bridge's own web page has no authentication** unless you configure ESPHome's `web_server`
  authentication. Treat an unauthenticated bridge as something anyone on the network can operate.
- **Do not use this on anything protective.** If a gate, door or lock uses a fixed code, its
  security already rests on nobody being nearby with a receiver. Adding a network-reachable
  replayer does not help.
- **`rf_factory_reset` and `rf_clear` are destructive and unauthenticated** beyond ESPHome's API
  encryption key. Anyone who can call your node's actions can wipe the registry.

Keep the ESPHome `api:` encryption key secret, and keep the bridge on a network you trust.

## What is kept out of diagnostics

The Home Assistant diagnostics download carries bridge identity, status, command names and
inventory metadata. It deliberately excludes waveform timings, and it contains no tokens,
credentials or configuration belonging to other integrations.
