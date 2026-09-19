"""Stand-in for POSIX `fcntl`, used only by Home Assistant's single-instance lock."""

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def flock(fd, operation):
    """No-op: a test run is the only Home Assistant on this machine."""
    return None


def lockf(fd, operation, length=0, start=0, whence=0):
    """No-op, for the same reason as flock."""
    return None
