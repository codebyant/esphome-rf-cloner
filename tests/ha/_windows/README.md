# Windows stand-ins for POSIX modules

Home Assistant's `runner` imports `fcntl` and `resource` at module import time, and
`pytest-homeassistant-custom-component` imports `runner` while pytest is still loading its
plugins - before any `conftest.py` has been read. So these cannot be installed from a fixture;
they have to be on `sys.path` before pytest starts.

`tests/run_ha.sh` puts this directory on `PYTHONPATH`, and only on Windows. On Linux, where CI
runs, the real modules are used and this directory is never on the path.

Neither module is used by anything a test reaches: `fcntl` guards a single-instance lock, and
`resource` raises a file-descriptor soft limit.
