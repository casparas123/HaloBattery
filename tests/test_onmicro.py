"""Tests for providers/onmicro.py - the HS6209 2.4 GHz receiver (32c2:0012), #24.

The frame is mirrored from ksanjeev284/acer-popgo-companion, which documents the
same vendor page on 32c2:0066:

    request  B5 | 01 00 00 00 00 00 00    report id 0xB5, status command
    reply    B5 | 01 flags pct a b c d    pct in byte 3, LiPo mV often in 5-6

No hardware has answered yet: the first real reply decides whether the layout
transfers from 0066 to 0012.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import onmicro as O  # noqa: E402

# The reference project's live sample: [181, 1, 1, 56, 27, 2, 14, 40]
STATUS_56 = [0xB5, 0x01, 0x01, 56, 27, 2, 14, 40]
STATUS_56_STRIPPED = STATUS_56[1:]          # hidapi without the report id
NO_VOLTAGE_56 = [0xB5, 0x01, 0x01, 56, 0, 0, 0, 0]


class FrameTest(unittest.TestCase):
    def test_the_request_is_report_id_then_status(self):
        req = O.make_request()
        self.assertEqual(O.REPORT_SIZE, len(req))
        self.assertEqual([0xB5, 0x01], req[:2])
        self.assertEqual([0] * 6, req[2:])

    def test_the_reference_status_packet_prefers_the_voltage_estimate(self):
        # 0x0E02 = 3586 mV -> (3586 - 3300) / 9 = 31.8 -> 32 %
        self.assertEqual((32, 3586), O.parse_reply(STATUS_56))

    def test_a_reply_without_the_report_id_is_read_too(self):
        self.assertEqual((32, 3586), O.parse_reply(STATUS_56_STRIPPED))

    def test_without_a_plausible_voltage_the_mcu_percentage_is_used(self):
        self.assertEqual((56, None), O.parse_reply(NO_VOLTAGE_56))

    def test_a_voltage_estimate_is_capped_at_100(self):
        self.assertEqual((100, 4300), O.parse_reply([0xB5, 0x01, 0x01, 56, 0, 0xCC, 0x10, 0]))

    def test_another_command_is_not_the_status(self):
        self.assertIsNone(O.parse_reply([0xB5, 0x02, 0x01, 56, 0, 0, 0, 0]))

    def test_a_zero_or_oversized_level_is_refused_when_there_is_no_voltage(self):
        self.assertIsNone(O.parse_reply([0xB5, 0x01, 0x01, 0x00, 0, 0, 0, 0]))
        self.assertIsNone(O.parse_reply([0xB5, 0x01, 0x01, 0x65, 0, 0, 0, 0]))

    def test_a_short_frame_is_ignored(self):
        self.assertIsNone(O.parse_reply([0xB5, 0x01]))
        self.assertIsNone(O.parse_reply([]))


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
    INFO = [{"path": b"onmicro", "interface_number": 1, "usage_page": 0xFFB5, "usage": 0x0001,
             "product_id": O.PID, "product_string": "HS6209 2.4G Wireless Receiver"}]

    def setUp(self):
        self._saved = (O.hid, O.hidlist, O.time)
        self.now = [1000.0]

    def tearDown(self):
        O.hid, O.hidlist, O.time = self._saved

    def poll(self, frames, infos=None):
        dev = FakeDevice(frames)
        O.time = types.SimpleNamespace(time=lambda: self.now[0], sleep=lambda s: None)
        O.hid = types.SimpleNamespace(device=lambda: dev)
        O.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: list(self.INFO if infos is None else infos))
        return O.OnMicroProvider().poll(), dev

    def test_the_receiver_is_read_on_its_vendor_page(self):
        out, dev = self.poll([STATUS_56])
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.kind) for s in out],
                         [("onmicro:0012", "2.4G Wireless Mouse (HS6209)", 32, False, "mouse")])
        self.assertEqual([b"onmicro"], dev.opened)
        self.assertEqual([bytes(O.make_request())], dev.written)
        self.assertFalse(out[0].charging_known)

    def test_no_reply_keeps_the_last_reading_greyed_for_five_minutes(self):
        provider = O.OnMicroProvider()
        dev = FakeDevice([STATUS_56])
        O.hid = types.SimpleNamespace(device=lambda: dev)
        O.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(self.INFO))
        O.time = types.SimpleNamespace(time=lambda: self.now[0], sleep=lambda s: None)
        first = provider.poll()
        self.assertEqual(32, first[0].level)

        self.now[0] += 60
        dev.frames = []
        second = provider.poll()
        self.assertEqual([(32, False)], [(s.level, s.online) for s in second])
        self.assertTrue(second[0].wake_on_input)

        self.now[0] += O.ASLEEP_KEEP
        third = provider.poll()
        self.assertEqual([None], [s.level for s in third])

    def test_no_receiver_reports_nothing(self):
        out, _dev = self.poll([], infos=[])
        self.assertEqual([], out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
