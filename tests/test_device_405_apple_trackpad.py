"""Tests for providers/apple_trackpad.py - no hardware needed.

The report shape is the one imbushuo/mac-precision-touchpad reads on Windows:
usage page FF00 usage 0014, input report 0x90, byte 2 = charge percent.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import apple_trackpad as A  # noqa: E402


def report(level=75):
    return [0x90, 0x00, level]


class FakeTrackpad:
    def __init__(self, reports=None, fail_input=False):
        self.reports = list(reports or [])
        self.fail_input = fail_input
        self.path = None
        self.input_reads = 0
        self.feature_reads = 0

    def open_path(self, path):
        self.path = path

    def close(self):
        pass

    def get_input_report(self, report_id, size):
        self.input_reads += 1
        if self.fail_input:
            raise OSError("not an input report")
        return self.reports.pop(0) if self.reports else []

    def get_feature_report(self, report_id, size):
        self.feature_reads += 1
        return self.reports.pop(0) if self.reports else []


def entries(vid=A.VID_BT, pid=A.PID):
    return [{"vendor_id": vid, "product_id": pid, "interface_number": -1,
             "usage_page": A.PAGE, "usage": A.USAGE,
             "path": b"trackpad", "product_string": "Magic Trackpad 2"}]


class ParseTest(unittest.TestCase):
    def test_the_level_is_byte_two(self):
        self.assertEqual(A.parse_report(report(75)), 75)
        self.assertEqual(A.parse_report(report(0)), 0)

    def test_a_wrong_report_or_level_is_refused(self):
        self.assertIsNone(A.parse_report([]))
        self.assertIsNone(A.parse_report([0x91, 0, 50]))
        self.assertIsNone(A.parse_report([0x90, 0, 101]))


class PollTest(unittest.TestCase):
    def setUp(self):
        self._saved = (A.hid, A.hidlist)

    def tearDown(self):
        A.hid, A.hidlist = self._saved

    def poll(self, fake, ents):
        A.hid = types.SimpleNamespace(device=lambda: fake)
        A.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: [e for e in ents if e["vendor_id"] == vid])
        p = A.AppleTrackpadProvider()
        return p.poll(), p.diagnostics()

    def test_the_bt_trackpad_is_read(self):
        fake = FakeTrackpad([report(75)])
        out, _ = self.poll(fake, entries())
        self.assertEqual([(s.key, s.name, s.level, s.online, s.kind) for s in out],
                         [("apple_trackpad:0265", "Apple Magic Trackpad 2", 75, True, "mouse")])
        self.assertEqual(fake.input_reads, 1)

    def test_the_feature_form_is_a_fallback(self):
        fake = FakeTrackpad([report(66)], fail_input=True)
        out, _ = self.poll(fake, entries())
        self.assertEqual(out[0].level, 66)
        self.assertEqual(fake.feature_reads, 1)

    def test_another_vendor_collection_is_ignored(self):
        ents = entries()
        ents[0]["usage"] = 0x0001
        out, _ = self.poll(FakeTrackpad([report()]), ents)
        self.assertEqual(out, [])

    def test_no_reply_reports_nothing(self):
        out, _ = self.poll(FakeTrackpad([]), entries())
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
