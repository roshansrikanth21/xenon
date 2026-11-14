"""Test-wide setup.

Xenon learns the user's habits into %APPDATA%\\xenon\\patterns.sqlite3.
Tests drive the real voice and typed paths, which record each exchange,
so without this they wrote fake requests ("do open notepad") into the
user's real habit store. Tests that exercise the store build their own
``PatternStore`` on a temp path, which this switch does not affect.
"""
import os

os.environ["XENON_PATTERNS"] = "off"
