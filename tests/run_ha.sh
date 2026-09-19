#!/bin/sh
# The Home Assistant integration tests, which need a real Home Assistant installed.
#
#   python -m pip install "homeassistant==2026.9.1" pytest-homeassistant-custom-component
#
# Home Assistant 2026.9 needs Python 3.14.2 or newer.
set -e
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"

# Home Assistant's runner imports fcntl and resource at import time, and the pytest plugin
# imports the runner before any conftest.py is read - so on Windows the stand-ins have to be on
# the path before pytest starts. On Linux the real modules are used and this is skipped.
case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN* | Windows_NT)
        PYTHONPATH="$(pwd)/tests/ha/_windows${PYTHONPATH:+:$PYTHONPATH}"
        export PYTHONPATH
        ;;
esac

exec "$PYTHON" -m pytest tests/ha "$@"
