"""Tests for providers/ducky.py - the Ducky OK-M on its I-CHIP dongle (#545).

The protocol comes from the Yunzii B87's documented read of the same
056d:c077 dongle (jordan-lee-code/yunzii-applet); the Ducky's reply is not on
hand yet. Frames:

    request  0a 05 00 ...          report id 0x0a, command 0x05
    reply    0a 05 ... 50 2a       byte 14 = 80 %, byte 15 = 4.2 V x 10
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import ducky as D  # noqa: E402

CAPTURED_80 = [0x0A, 0x05] + [0x00] * 12 + [0x50, 0x2A] + [0x00] * 48
CAPTURED_80_PREFIXED = [0x00] + CAPTURED_80
UNRELATED = [0x0A, 0x06] + [0x00] * 12 + [0x50] + [0x00] * 49


class FrameTest(unittest.TestCase):
    def test_the_request_is_report_id_then_command(self):
        req = D.make_request()
        self.assertEqual(D.REPORT_SIZE, len(req))
        self.assertEqual([0x0A, 0x05], req[:2])
        self.assertEqual([0] * (D.REPORT_SIZE - 2), req[2:])

    def test_the_documented_reply_reads_80_and_4_2_v(self):
        self.assertEqual((80, 42), D.parse_reply(CAPTURED_80))

    def test_a_leading_zero_before_the_report_id_is_accepted(self):
        self.assertEqual((80, 42), D.parse_reply(CAPTURED_80_PREFIXED))

    def test_another_command_is_not_the_battery(self):
        self.assertIsNone(D.parse_reply(UNRELATED))

    def test_zero_and_oversized_levels_are_refused(self):
        self.assertIsNone(D.parse_reply([0x0A, 0x05] + [0] * 12 + [0x00] + [0] * 49))
        self.assertIsNone(D.parse_reply([0x0A, 0x05] + [0] * 12 + [0x65] + [0] * 49))

    def test_a_short_frame_is_ignored(self):
        self.assertIsNone(D.parse_reply([0x0A, 0x05, 0x00]))


class FakeDevice:
    def __init__(self, frames):
        self.frames = list(frames)
        self.written = []
        self.opened = []

    def open_path(self, path):
        self.opened.append(path)

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def read(self, size, timeout_ms=0):
        return self.frames.pop(0) if self.frames else []

    def close(self):
        pass


class PollTest(unittest.TestCase):
    INFO = [{"path": b"ducky", "interface_number": 1, "usage_page": 0xFF00,
             "usage": 0x0001, "product_id": D.PID,
             "product_string": "Ducky OK-M 2.4G"}]
    WIRED = [{"path": b"wired", "interface_number": 0, "usage_page": 0x0001,
              "usage": 0x0006, "product_id": D.WIRED_PID,
              "product_string": "Ducky OK-M-98 Gaming Keyboard"}]

    def setUp(self):
        self._saved = (D.hid, D.hidlist, D.time)
        self.now = [1000.0]

    def tearDown(self):
        D.hid, D.hidlist, D.time = self._saved

    def poll(self, frames, infos=None, wired=None):
        dev = FakeDevice(frames)
        D.time = types.SimpleNamespace(time=lambda: self.now[0])
        D.hid = types.SimpleNamespace(device=lambda: dev)
        lists = {D.WIRED_VID: list(wired or []),
                 D.VID: list(self.INFO if infos is None else infos),
                 0: list(self.INFO if infos is None else infos)}

        def enumerate_(vid=0):
            return list(lists.get(vid, []))

        D.hidlist = types.SimpleNamespace(enumerate=enumerate_)
        return D.DuckyProvider().poll(), dev

    def test_the_keyboard_is_read_and_charging_is_known(self):
        out, dev = self.poll([CAPTURED_80])
        self.assertEqual([(s.key, s.name, s.level, s.online, s.kind) for s in out],
                         [("ducky:c077", "Ducky OK-M", 80, True, "keyboard")])
        self.assertFalse(out[0].charging)
        self.assertTrue(out[0].charging_known)
        self.assertEqual([b"ducky"], dev.opened)
        self.assertEqual([bytes(D.make_request())], dev.written)

    def test_the_wired_device_is_the_charging_signal(self):
        out, dev = self.poll([CAPTURED_80], wired=self.WIRED)
        self.assertTrue(out[0].charging)
        self.assertTrue(out[0].charging_known)
        self.assertEqual([b"ducky"], dev.opened, "the wired collection is never opened")

    def test_another_smartcloud_device_is_not_a_ducky_cable(self):
        wired = [dict(self.WIRED[0], product_string="SmartCloud generic keyboard")]
        out, _ = self.poll([CAPTURED_80], wired=wired)
        self.assertFalse(out[0].charging)

    def test_a_yunzii_product_is_not_claimed(self):
        infos = [dict(self.INFO[0], product_string="I-CHIP YUNZII B87 2.4G")]
        out, dev = self.poll([], infos=infos)
        self.assertEqual([], out)
        self.assertEqual([], dev.opened)

    def test_a_reply_to_the_wrong_command_falls_through(self):
        out, _ = self.poll([UNRELATED])
        self.assertEqual(1, len(out))
        self.assertIsNone(out[0].level)
        self.assertFalse(out[0].online)
