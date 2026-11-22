#!/bin/sh
# Tests ohne Xcode (laufen auch unter Linux):
#   Tischtelefon-Bausteine (Swift), push-relay und dessen Verwaltung (Python)
set -e
cd "$(dirname "$0")/.."
swiftc -o /tmp/deskphone_tests tests/DeskPhoneTests/main.swift \
  CMEPhoneMac/DeskPhone/CiscoXML.swift CMEPhoneMac/DeskPhone/DialogInfo.swift
/tmp/deskphone_tests
python3 tests/test_relay.py
python3 tests/test_admin.py
