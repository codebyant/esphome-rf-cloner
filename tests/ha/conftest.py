"""Fixtures for the tests that run inside a real Home Assistant.

The only platform-specific thing left here is pytest-socket. Home Assistant's test plugin arms it
for every test, and on Windows that also blocks the AF_INET socketpair asyncio builds for the
event loop's own self-pipe - so the guard is stood down there rather than the loop. It is not
behaviour under test, and it is untouched on Linux, which is what CI runs.

The POSIX module stand-ins Windows also needs are not here: the plugin imports Home Assistant's
runner before any conftest is read, so they go on PYTHONPATH from `tests/run_ha.sh` instead.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# So `custom_components.rf_cloner` imports as itself, the way Home Assistant loads it.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if sys.platform == "win32":
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None

# Imported after the guard above, because the plugin arms pytest-socket as it loads.
pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant find custom_components/rf_cloner."""
    return enable_custom_integrations


@pytest.fixture(autouse=True)
def _custom_components_path(hass):
    """Point Home Assistant's config directory at this repository's custom_components."""
    hass.config.config_dir = str(REPO_ROOT)
    return hass
