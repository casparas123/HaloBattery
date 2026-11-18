"""Tests for the Xinmeng M87 (0c45:fefe) and Zero 3 (3151:5045) providers, #455.

No hardware needed.  The M87 exchange is the EWEADN X87 vendor-channel protocol:
32-byte packet, report id 0, command 0x10, device-info reply 0x55 with the level
at byte 17.  The Zero 3 exchange is the URX HUB Rongyuan status query: 64-byte
feature report, 0xF7, mouse level at byte 2 and the disconnect flag at byte 4.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import xinmeng_m87 as M  # noqa: E402
from providers import xinmeng_zero3 as Z  # noqa: E402


def m87_data(level=73, product=0x8006):
    p = [0x00] * 32
    p[12], p[13] = 0x45, 0x0C
    p[14], p[15] = product & 0xFF, product >> 8
    p[17] = level
    return p


def m87_chunk(data, offset, length):
    r = [0x00] * 32
    r[0], r[1], r[2] = 0x55, 0x10, length
    r[3], r[4] = offset & 0xFF, offset >> 8
    r[8:8 + length] = data[offset:offset + length]
    return r


def m87_replies(level=73, product=0x8006):
    data = m87_data(level, product)
    return [m87_chunk(data, 0, 24), m87_chunk(data, 24, 8)]


def z_status(level=54, disconnected=0, usb=1, devices=1):
    r = [0x00] * 64
    r[0] = 1
    r[2] = level
    r[4] = disconnected
    r[6] = devices
    r[7] = usb
    return r


def m87_entries(receiver=True, wired=False, product="2.4G Dongle"):
    out = []
    if receiver:
        out.append({"vendor_id": 0x0C45, "product_id": 0xFEFE, "interface_number": 3,
                    "usage_page": 0xFF60, "usage": 0x0061,
                    "path": b"m87-receiver", "product_string": product})
    if wired:
        out.append({"vendor_id": 0x0C45, "product_id": 0x8006, "interface_number": 0,
                    "usage_page": 0x0001, "usage": 0x0006,
                    "path": b"m87-wired", "product_string": "Xinmeng M87"})
    return out


def z_entries(receiver=True, wired=False):
    out = []
    if receiver:
        out.append({"vendor_id": 0x3151, "product_id": 0x5045, "interface_number": 1,
                    "usage_page": 0xFFFF, "usage": 0x0002,
                    "path": b"z-receiver", "product_string": "2.4G Wireless Mouse"})
    if wired:
        out.append({"vendor_id": 0x3151, "product_id": 0x5043, "interface_number": 0,
                    "usage_page": 0xFFFF, "usage": 0x0002,
                    "path": b"z-wired", "product_string": "2.4G Wireless Mouse"})
    return out


class FakeDev:
    def __init__(self, replies=None, feature_replies=None):
        self.replies = list(replies or [])
        self.feature_replies = list(feature_replies or [])
        self.sent = []
        self.feature_sent = []
        self.path = None

    def open_path(self, path):
        self.path = path

    def close(self):
        pass

    def set_nonblocking(self, value):
        pass

    def write(self, data):
        self.sent.append((self.path, bytes(data)))
        return len(data)

    def read(self, size, timeout_ms=0):
        if timeout_ms == 0:
            return []
        return self.replies.pop(0) if self.replies else []

    def send_feature_report(self, data):
        self.feature_sent.append(bytes(data))
        return len(data)

    def get_feature_report(self, report_id, size):
        if self.feature_replies:
            return self.feature_replies.pop(0)
        return []


class M87ParseTests(unittest.TestCase):
    def test_the_request_is_the_two_chunk_x87_info_block(self):
        requests = M.make_requests()
        self.assertEqual([(r[2], r[3], r[6]) for r in requests],
                         [(24, 0, 0), (8, 24, 1)])
        self.assertEqual((requests[0][0], requests[0][1]), (0xAA, 0x10))
        self.assertEqual(len(requests[0]), 32)

    def test_the_captured_shape_reads_level_and_product(self):
        self.assertEqual(M.parse_info(m87_data(73)), (73, 0x0C45, 0x8006, False))

    def test_each_reply_carries_its_offset_and_payload(self):
        data = m87_data(41)
        self.assertEqual(M.parse_chunk(m87_chunk(data, 0, 24)), (0, bytes(data[:24])))
        self.assertEqual(M.parse_chunk(m87_chunk(data, 24, 8)), (24, bytes(data[24:])))

    def test_the_reply_may_keep_or_drop_the_report_id(self):
        reply = [0] + m87_chunk(m87_data(41), 0, 24)
        self.assertEqual(M.parse_chunk(reply)[1], bytes(m87_data(41)[:24]))

    def test_a_foreign_or_short_answer_is_refused(self):
        bad = m87_chunk(m87_data(), 0, 24)
        bad[0] = 0xAA
        self.assertIsNone(M.parse_chunk(bad))
        bad = m87_chunk(m87_data(), 0, 24)
        bad[1] = 0x11
        self.assertIsNone(M.parse_chunk(bad))
        self.assertIsNone(M.parse_info(m87_data(101)))
        self.assertIsNone(M.parse_info([]))
        self.assertIsNone(M.parse_chunk([0x55, 0x10]))


class M87PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (M.hid, M.hidlist)

    def tearDown(self):
        M.hid, M.hidlist = self._saved

    def poll(self, fake, ents):
        M.hid = types.SimpleNamespace(device=lambda: fake)
        M.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: [e for e in ents if e["vendor_id"] == vid])
        p = M.XinmengM87Provider()
        return p.poll(), p.diagnostics()

    def test_the_receiver_is_read_and_charging_is_unknown(self):
        fake = FakeDev(m87_replies(73))
        out, diag = self.poll(fake, m87_entries())
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.online, s.kind,
                           s.charging_known) for s in out],
                         [("xinmeng:m87", "Xinmeng M87", 73, False, True, "keyboard", False)])
        self.assertEqual({p for p, _ in fake.sent}, {b"m87-receiver"})
        self.assertEqual(len(fake.sent), 2)
        self.assertTrue(any("73%" in line for line in diag))

    def test_the_wired_id_is_the_charging_signal(self):
        fake = FakeDev(m87_replies(73))
        out, _ = self.poll(fake, m87_entries(wired=True))
        self.assertEqual(out[0].level, 73)
        self.assertTrue(out[0].charging)
        self.assertTrue(out[0].charging_known)
        self.assertEqual({p for p, _ in fake.sent}, {b"m87-wired"})

    def test_the_x66_is_left_to_its_own_provider(self):
        out, _ = self.poll(FakeDev(m87_replies()), m87_entries(product="X66 2.4G"))
        self.assertEqual(out, [])

    def test_a_missing_answer_keeps_an_icon_without_a_level(self):
        out, diag = self.poll(FakeDev([]), m87_entries())
        self.assertEqual([(s.level, s.online, s.charging_known) for s in out],
                         [(None, False, False)])
        self.assertTrue(any("no answer" in line for line in diag))


class Zero3ParseTests(unittest.TestCase):
    def test_the_status_query_is_a_zeroed_feature_report_with_f7(self):
        req = Z.make_request()
        self.assertEqual(len(req), 64)
        self.assertEqual(req[0], 0xF7)
        self.assertEqual(sum(req[1:]), 0)

    def test_the_mouse_level_is_byte_two(self):
        self.assertEqual(Z.parse_status(z_status(54)), (54, 1, 1))

    def test_a_disconnected_mouse_is_refused(self):
        self.assertIsNone(Z.parse_status(z_status(54, disconnected=1)))

    def test_a_wired_echo_with_no_connected_devices_is_refused(self):
        self.assertIsNone(Z.parse_status(z_status(0, disconnected=0, devices=0)))

    def test_a_foreign_or_short_answer_is_refused(self):
        self.assertIsNone(Z.parse_status([]))
        self.assertIsNone(Z.parse_status(z_status(101)))
        self.assertIsNone(Z.parse_status([1, 0, 54]))

    def test_the_reply_may_keep_or_drop_the_report_id(self):
        self.assertEqual(Z.parse_status(z_status(22))[0], 22)
        self.assertEqual(Z.parse_status([0] + z_status(22))[0], 22)


class Zero3PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (Z.hid, Z.hidlist)

    def tearDown(self):
        Z.hid, Z.hidlist = self._saved

    def poll(self, fake, ents):
        Z.hid = types.SimpleNamespace(device=lambda: fake)
        Z.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: [e for e in ents if e["vendor_id"] == vid])
        p = Z.XinmengZero3Provider()
        return p.poll(), p.diagnostics()

    def test_the_receiver_status_is_read(self):
        fake = FakeDev(feature_replies=[z_status(54)])
        out, diag = self.poll(fake, z_entries())
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.online, s.kind,
                           s.charging_known) for s in out],
                         [("xinmeng:zero3", "Xinmeng Zero 3", 54, False, True, "mouse", False)])
        self.assertTrue(out[0].wake_on_input)
        self.assertEqual(len(fake.feature_sent), 1)
        self.assertEqual(fake.feature_sent[0][0], 0)
        self.assertEqual(fake.feature_sent[0][1], 0xF7)
        self.assertTrue(any("usb_mode=1" in line for line in diag))

    def test_the_wired_id_is_the_charging_signal(self):
        fake = FakeDev(feature_replies=[z_status(54)])
        out, _ = self.poll(fake, z_entries(wired=True))
        self.assertEqual(out[0].level, 54)
        self.assertTrue(out[0].charging)
        self.assertTrue(out[0].charging_known)

    def test_a_disconnected_mouse_keeps_an_icon_without_a_level(self):
        out, diag = self.poll(FakeDev(feature_replies=[z_status(54, disconnected=1)] * 3),
                              z_entries())
        self.assertEqual([(s.level, s.online, s.charging_known) for s in out],
                         [(None, False, False)])
        self.assertTrue(any("no answer" in line for line in diag))

    def test_a_previous_level_is_greyed_when_the_mouse_disconnects(self):
        provider = Z.XinmengZero3Provider()
        Z.hid = types.SimpleNamespace(device=lambda: FakeDev(feature_replies=[z_status(61)]))
        Z.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: [e for e in z_entries() if e["vendor_id"] == vid])
        awake = provider.poll()
        self.assertEqual((awake[0].level, awake[0].online), (61, True))

        Z.hid = types.SimpleNamespace(
            device=lambda: FakeDev(feature_replies=[z_status(61, disconnected=1)] * 3))
        asleep = provider.poll()
        self.assertEqual([(s.level, s.online) for s in asleep], [(61, False)])


if __name__ == "__main__":
    unittest.main()
