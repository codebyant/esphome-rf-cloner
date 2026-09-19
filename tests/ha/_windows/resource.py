"""Stand-in for POSIX `resource`, used only to raise a file-descriptor soft limit.

Reporting a limit that is already far above the one Home Assistant wants makes its helper return
without trying to change anything.
"""

RLIMIT_NOFILE = 7
RLIM_INFINITY = -1


def getrlimit(which):
    """Report a limit high enough that the caller leaves it alone."""
    return (1 << 20, 1 << 20)


def setrlimit(which, limits):
    """No-op: nothing in a test run depends on the limit actually moving."""
    return None
