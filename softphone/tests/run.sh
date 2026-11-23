#!/bin/sh
# Tests ohne Xcode (laufen auch unter Linux):
#   Tischtelefon-Bausteine (Swift); push-relay und cme-web-Teil (Python, ../tests)
set -e
cd "$(dirname "$0")/.."
swiftc -o /tmp/deskphone_tests tests/DeskPhoneTests/main.swift \
  CMEPhoneMac/DeskPhone/CiscoXML.swift CMEPhoneMac/DeskPhone/DialogInfo.swift
/tmp/deskphone_tests
cd .. && python3 tests/test_relay.py && CME_DEMO=true python3 tests/test_phoneapp.py
