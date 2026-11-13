"""Tests for providers/keychron.py. No hardware is needed.

The Ultra-Link 8K (3434:d028) and M5 (3434:d048) use a 64-byte feature report
`b3 06`, answered `b4 06` with the level at byte 20.

The Nape Pro / M6 Link-KM receiver (3434:d026, #270) uses a different exchange:
a 32-byte output report `a7 31` on the 0xFF60:0061 raw-HID collection, answered
`a7 31 <level> <state>`. The Launcher sends that with WebHID report id 0, so the
test also pins the hidapi write buffer's leading zero.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import keychron as K  # noqa: E402


def reply(level):
    return [K.REPORT_ID_RESP, K.CMD_STATUS] + [0x00] * 18 + [level] + [0x00] * (K.REPORT_SIZE - 21)


def nape_reply(level, prefix=False):
    data = [K.NAPE_CMD_GROUP, K.NAPE_CMD_BATTERY, level, 0x00] + [0x00] * (K.NAPE_REPORT_SIZE - 4)
    return ([0x00] + data) if prefix else data


class FakeDevice:
    def __init__(self, replies):
        self.replies = list(replies)
        self.opened = []
        self.sent = []
        self.writes = []

    def open_path(self, path):
        self.opened.append(path)

    def send_feature_report(self, data):
        self.sent.append(bytes(data))
        return len(data)

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def get_feature_report(self, report_id, size):
        return []

    def read(self, size, timeout):
        return self.replies.pop(0) if self.replies else []

    def close(self):
        pass


class PollTest(unittest.TestCase):
    NAPE = [
        {"path": b"nape-mouse", "interface_number": 0,
         "usage_page": 0x0001, "usage": 0x0002, "product_id": 0xD026},
        {"path": b"nape-vendor", "interface_number": 1,
         "usage_page": 0xFFC1, "usage": 0x0001, "product_id": 0xD026},
        {"path": b"nape-via", "interface_number": 3,
         "usage_page": 0xFF60, "usage": 0x0061, "product_id": 0xD026},
        {"path": b"nape-keyboard", "interface_number": 4,
         "usage_page": 0x0001, "usage": 0x0006, "product_id": 0xD026},
    ]

    def setUp(self):
        self._saved = (K.hid, K.hidlist, K.time)

    def tearDown(self):
        K.hid, K.hidlist, K.time = self._saved

    def poll(self, replies, infos):
        dev = FakeDevice(replies)
        K.hid = types.SimpleNamespace(device=lambda: dev)
        K.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(infos))
        K.time = types.SimpleNamespace(sleep=lambda s: None)
        return K.KeychronProvider().poll(), dev

    def test_the_nape_pro_reads_the_a7_31_exchange_on_ff60(self):
        out, dev = self.poll([nape_reply(94)], self.NAPE)
        self.assertEqual([(s.key, s.name, s.level) for s in out],
                         [("keychron:d026", "Keychron Nape Pro", 94)])
        self.assertEqual([b"nape-via"], dev.opened)
        self.assertEqual([b"\x00" + bytes(K.make_nape_request())], dev.writes)
        self.assertEqual([], dev.sent)
        # charging state is not known on this exchange
        self.assertFalse(out[0].charging)
        self.assertFalse(out[0].charging_known)

    def test_the_nape_falls_back_to_the_32_byte_output_form(self):
        # the report-id form is accepted by Windows but gets no answer; the
        # Launcher's 32-byte form does
        out, dev = self.poll([[], [], nape_reply(88)], self.NAPE)
        self.assertEqual(88, out[0].level)
        self.assertEqual(33, len(dev.writes[0]))
        self.assertEqual(32, len(dev.writes[-1]))
        self.assertEqual(bytes(K.make_nape_request()), dev.writes[-1])

    def test_the_nape_reply_may_carry_the_report_id(self):
        out, _dev = self.poll([nape_reply(55, prefix=True)], self.NAPE)
        self.assertEqual(55, out[0].level)

    def test_parse_nape_rejects_bad_shapes(self):
        self.assertIsNone(K.parse_nape([]))
        self.assertIsNone(K.parse_nape([K.NAPE_CMD_GROUP, K.NAPE_CMD_BATTERY]))
        self.assertIsNone(K.parse_nape([K.NAPE_CMD_GROUP, 0x30, 50, 0]))
        self.assertIsNone(K.parse_nape([K.NAPE_CMD_GROUP, K.NAPE_CMD_BATTERY, 101, 0]))

    def test_ultralink_interface_4_still_wins(self):
        infos = [
            {"path": b"iface1", "interface_number": 1,
             "usage_page": 0xFFC1, "usage": 0x0001, "product_id": 0xD028},
            {"path": b"iface4", "interface_number": 4,
             "usage_page": 0xFF00, "usage": 0x0001, "product_id": 0xD028},
        ]
        out, dev = self.poll([reply(42)], infos)
        self.assertEqual(42, out[0].level)
        self.assertEqual([b"iface4"], dev.opened)

    def test_ultralink_silent_vendor_falls_through(self):
        infos = [
            {"path": b"iface1", "interface_number": 1,
             "usage_page": 0xFFC1, "usage": 0x0001, "product_id": 0xD028},
            {"path": b"iface4", "interface_number": 4,
             "usage_page": 0x0001, "usage": 0x0006, "product_id": 0xD028},
        ]
        out, dev = self.poll([[], [], [], reply(42)], infos)
        self.assertEqual(42, out[0].level)
        self.assertEqual([b"iface1", b"iface4"], dev.opened)

    def test_a_bad_ultralink_reply_is_not_a_level(self):
        self.assertIsNone(K.parse_level([0xB3, 0x06] + [0] * 62))
        self.assertIsNone(K.parse_level([0xB4, 0x06] + [0] * 10))     # too short
        self.assertIsNone(K.parse_level(reply(101)))
        self.assertIsNone(K.parse_level([]))

    def test_no_reply_reports_nothing(self):
        out, _dev = self.poll([], self.NAPE)
        self.assertEqual([], out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
