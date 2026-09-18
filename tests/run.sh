#!/bin/sh
# Host-side unit tests for the pure rf_cloner logic (no ESPHome, no hardware).
set -e
cd "$(dirname "$0")"
CXX="${CXX:-g++}"
"$CXX" -std=c++20 -Wall -Wextra -I../components/rf_cloner \
    -o test_rf_cloner test_rf_cloner.cpp \
    ../components/rf_cloner/capture_validator.cpp \
    ../components/rf_cloner/command_store.cpp
./test_rf_cloner
