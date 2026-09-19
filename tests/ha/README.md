# Home Assistant integration tests

These run the integration inside a real Home Assistant, because what they check is Home
Assistant's own behaviour: what a config subentry does to the devices and entities attached to it,
whether an entity keeps its identity when it moves, and what survives a reload.

The suite in `tests/` next to this directory deliberately does not install Home Assistant - it
stubs the handful of types the pure logic borrows. That is the right trade for logic that has no
Home Assistant behaviour in it. It is the wrong trade for registry semantics, which is all this
suite is about.

## Running them

```
python -m pip install "homeassistant==2026.9.1" pytest-homeassistant-custom-component
python -m pytest tests/ha
```

Home Assistant 2026.9 requires Python 3.14.2 or newer.

On Windows, `conftest.py` shims the POSIX-only modules Home Assistant's runner imports and stands
down pytest-socket, whose guard rejects the AF_INET socketpair that asyncio's event loop builds
there. Neither shim is active on Linux, which is what CI runs.
