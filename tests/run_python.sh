#!/bin/sh
# Host-side tests for the Home Assistant integration's pure logic (no Home Assistant, no hardware).
#
# Covers the paths that cannot be rehearsed against a live bridge without destroying its registry:
# restore orchestration, and the identity-mismatch safety invariant a replacement depends on.
set -e
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python}"
"$PYTHON" test_restore_logic.py
echo
"$PYTHON" test_identity_mismatch.py
