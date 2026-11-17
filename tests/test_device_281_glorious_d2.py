"""Tests for providers/glorious_pixart.py. No hardware is needed.

The device shape is from #281's diagnostics: the Model D2 Wireless receiver is
Pixart `093a:824d`, with a 64-byte vendor feature collection (`ff00:0001`) and
an input collection (`0001:0002`).  The reader is GloriousBatteryMonitor-Go's
CandidateD: GetFeature only, report ids 0x00..0x0f, level scanned in bytes
1..10, and replies with only one or two non-zero bytes rejected as status.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import glorious_pixart as G  # noqa: E402


def frame(level=80, charging=0x00):
    f = [0x00, 0x00, 0x00, 0xAA, level, charging, 0xBB] + [0x00] * 58
    return f[:64]


CAPTURED_80 = frame(80)
CAPTURED_80_CHARGING = frame(80, charging=0x01)
CAPTURED_55 = frame(55)


class ParseTest(unittest.TestCase):
    def test_level_and_charge_are_read(self):
        self.assertEqual((80, False), G.parse_feature(CAPTURED_80))
        self.assertEqual((55, False), G.parse_feature(CAPTURED_55))
        self.assertEqual((80, True), G.parse_feature(CAPTURED_80_CHARGING))

    def test_a_sparse_status_reply_is_not_a_battery_frame(self):
        self.assertIsNone(G.parse_feature([0x00, 0x03, 0x02, 0x00, 0x00]))
        self.assertIsNone(G.parse_feature([0x00, 0x00, 0x50, 0x00]))

    def test_other_replies_are_refused(self):
        self.assertIsNone(G.parse_feature([0x00, 0xC8, 0xC9, 0xCA]))
        self.assertIsNone(G.parse_feature([0x00, 0x01, 0x02, 0x00]))
        self.assertIsNone(G.parse_feature([]))


class FakeDevice:
    def __init__(self, replies):
        self.replies = list(replies)
        self.reads = []
        self.opened = []

    def open_path(self, path):
        self.opened.append(path)

    def get_feature_report(self, rid, length):
        self.reads.append((rid, length))
        return self.replies.pop(0) if self.replies else []

    def send_feature_report(self, data):
        raise AssertionError("the D2 reader must not write feature reports")

    def close(self):
        pass


class PollTest(unittest.TestCase):
    INFO = [{"path": b"ff00", "interface_number": 1,
             "usage_page": 0xFF00, "usage": 0x0001,
             "product_id": G.PID_RECEIVER, "product_string": "Model D 2 Wireless"}]

    def setUp(self):
        self._saved = (G.hid, G.hidlist, G.time, G.feature_length)
        self.now = [1000.0]

    def tearDown(self):
        G.hid, G.hidlist, G.time, G.feature_length = self._saved

    def make(self, replies, infos=None):
        dev = FakeDevice(replies)
        G.feature_length = lambda path: 64
        G.time = types.SimpleNamespace(
            time=lambda: self.now[0],
            sleep=lambda s: None)
        G.hid = types.SimpleNamespace(device=lambda: dev)
        G.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: list(self.INFO if infos is None else infos))
        return G.GloriousD2Provider(), dev

    def test_the_d2_is_read_without_a_feature_write(self):
        provider, dev = self.make([CAPTURED_80])
        out = provider.poll()
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.online, s.kind) for s in out],
                         [("glorious:d2", "Glorious Model D2 Wireless",
                           80, False, True, "mouse")])
        self.assertEqual([(0, 64)], dev.reads)

    def test_a_later_report_id_is_tried_until_one_answers(self):
        provider, dev = self.make([[], [], CAPTURED_55])
        out = provider.poll()
        self.assertEqual(55, out[0].level)
        self.assertEqual([(0, 64), (1, 64), (2, 64)], dev.reads)

    WIRED = {"path": b"wired", "interface_number": 0, "product_id": G.PID_CABLE,
             "usage_page": 0x0001, "usage": 0x0002,
             "product_string": "Model D 2 Wireless"}

    def test_the_cable_counts_as_charging(self):
        provider, _dev = self.make([CAPTURED_80], infos=self.INFO + [self.WIRED])
        out = provider.poll()
        self.assertEqual((80, True, True), (out[0].level, out[0].charging, out[0].online))

    def test_the_cable_alone_keeps_the_last_level_and_says_charging(self):
        provider, dev = self.make([CAPTURED_80])
        self.assertEqual(80, provider.poll()[0].level)
        dev.replies = []
        provider.poll()  # second poll has no reply while still on the receiver
        G.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: list(self.INFO + [self.WIRED]))
        out = provider.poll()
        self.assertEqual((80, True, True), (out[0].level, out[0].charging, out[0].online))

    def test_no_reply_keeps_the_last_reading_greyed(self):
        provider, dev = self.make([CAPTURED_80])
        self.assertEqual(80, provider.poll()[0].level)
        dev.replies = []
        self.now[0] += 30
        out = provider.poll()
        self.assertEqual([(80, False, False)],
                         [(s.level, s.charging, s.online) for s in out])
        self.now[0] += G.ASLEEP_KEEP + 1
        out = provider.poll()
        self.assertEqual([(None, False)], [(s.level, s.online) for s in out])

    def test_no_receiver_reports_nothing(self):
        provider, _dev = self.make([], infos=[])
        self.assertEqual([], provider.poll())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
