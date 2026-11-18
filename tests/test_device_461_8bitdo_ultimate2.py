"""Tests for the 8BitDo Ultimate 2 Wireless battery reader, #461; no hardware.

The controller is an XInput pad; its battery is byte 14 of the enhanced input
report on vendor collection 2dc8:310b, 0xFF7A:0x0001, as SDL reads it.  The
reader only listens, because the controller streams the report after waking.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import eightbitdo_ultimate2 as U  # noqa: E402


def report(level=83, charging=False, rid=0x04):
    r = [0x00] * 64
    r[0] = rid
    r[U.BATTERY_BYTE] = (level & 0x7F) | (0x80 if charging else 0)
    return r


def entries():
    return [{"vendor_id": 0x2DC8, "product_id": 0x310B, "interface_number": 2,
             "usage_page": 0xFF7A, "usage": 0x0001,
             "path": b"ultimate2", "product_string": "8BitDo Ultimate 2 Wireless Controller for PC"}]


class FakeDev:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.sent = []
        self.path = None

    def open_path(self, path):
        self.path = path

    def close(self):
        pass

    def set_nonblocking(self, value):
        pass

    def read(self, size, timeout_ms=0):
        return self.replies.pop(0) if self.replies else []


class ParseTests(unittest.TestCase):
    def test_the_enhanced_byte_carries_level_and_charging(self):
        self.assertEqual((U.parse_report(report(83)).level,
                          U.parse_report(report(83)).charging), (83, False))
        self.assertEqual((U.parse_report(report(64, True)).level,
                          U.parse_report(report(64, True)).charging), (64, True))

    def test_an_ordinary_zero_padded_report_is_not_a_battery(self):
        self.assertIsNone(U.parse_report(report(0)))
        self.assertIsNone(U.parse_report([]))
        self.assertIsNone(U.parse_report([0x04] * 14))

    def test_a_level_above_100_is_refused(self):
        self.assertIsNone(U.parse_report(report(101)))


class ReadTests(unittest.TestCase):
    def setUp(self):
        self._hid = U.hid
        self._enumerate = U.hidlist.enumerate

    def tearDown(self):
        U.hid = self._hid
        U.hidlist.enumerate = self._enumerate

    def read(self, fake):
        U.hid = types.SimpleNamespace(device=lambda: fake)
        U.hidlist.enumerate = lambda vid=0: [e for e in entries()
                                              if e["vendor_id"] == vid]
        diag = []
        return U.read_connected(diag), diag

    def test_the_first_enhanced_report_is_read(self):
        got, diag = self.read(FakeDev([report(83, False)]))
        self.assertEqual((got.name, got.level, got.charging),
                         ("8BitDo Ultimate 2", 83, False))
        self.assertTrue(any("len=64" in line for line in diag))

    def test_ordinary_reports_are_skipped_until_the_enhanced_one(self):
        ordinary = [0x04] + [0] * 63
        got, _ = self.read(FakeDev([ordinary, report(77, True)]))
        self.assertEqual((got.level, got.charging), (77, True))

    def test_silence_is_reported_and_not_invented(self):
        got, diag = self.read(FakeDev([]))
        self.assertIsNone(got)
        self.assertTrue(any("nothing received" in line for line in diag))


if __name__ == "__main__":
    unittest.main()
