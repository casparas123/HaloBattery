"""Tests for providers/barracuda_x_0550.py (Razer Barracuda X, YS-Tech bridge).

No hardware: frames follow mehdibouchami/razer-barracuda-x-battery
(`Protocol.cs`, MIT): 64-byte report id 0x02, CRC-16/XMODEM over the whole
report, `02 'O'` acknowledgement, RACE frames in `50 49 ...` replies.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import barracuda_x_0550 as B  # noqa: E402


def race_frame(group, seq, body):
    payload = [group, 0x80 | seq, 0x00] + list(body)
    header = [0x50, 0x49, 0, 0, 0, 0, 0, 0,
              len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]
    return [0x02, len(header) + len(payload)] + header + payload


class FakeDevice:
    def __init__(self, mv=3975):
        self.mv = mv
        self.writes = []
        self.opened = []
        self._ack = False
        self._group = None
        self._seq = None
        self.closed = False

    def open_path(self, path):
        self.opened.append(path)

    def write(self, data):
        self.writes.append(bytes(data))
        self._group, self._seq = data[7], data[8]
        self._ack = False
        return len(data)

    def read(self, size, timeout):
        if not self._ack:
            self._ack = True
            return [0x02, ord("O")]
        if self._group == 0x0E:
            return race_frame(0x0E, self._seq, [])
        if self._group == 0x06:
            return race_frame(0x06, self._seq, [self.mv & 0xFF, self.mv >> 8])
        return []

    def close(self):
        self.closed = True


INFO = [{"path": b"ys-tech", "interface_number": 3,
         "usage_page": 0xFF00, "usage": 0x0001,
         "product_id": B.PID, "product_string": "Razer Barracuda X 2.4",
         "input_report_byte_length": 64, "output_report_byte_length": 64}]


class FrameTests(unittest.TestCase):
    def test_the_frame_is_64_bytes_with_the_ystech_header_and_crc(self):
        frame = B.make_frame(B.race_request(0x06, 3, [1, 0, 0x31]))
        self.assertEqual(64, len(frame))
        self.assertEqual((0x02, 0x80, 7), (frame[0], frame[1], frame[2]))
        self.assertEqual(bytes([0x50, 0x41, 0x06, 0x03, 1, 0, 0x31]), frame[5:12])
        crc = frame[3] | (frame[4] << 8)
        blank = bytearray(frame)
        blank[3] = blank[4] = 0
        self.assertEqual(B.crc16_xmodem(blank), crc)

    def test_the_battery_reply_is_voltage_at_3_4(self):
        payload = [0x06, 0x83, 0x00, 0xEE, 0x0F]     # 4078 mV
        self.assertEqual(4078, B.battery_mv(payload))
        self.assertIsNone(B.battery_mv([0x06, 0x83, 1, 0, 0]))

    def test_the_reply_frame_is_found_inside_an_input_report(self):
        frame = race_frame(0x06, 5, [0xEE, 0x0F])
        payload = B.find_frame(frame, 2, frame[1], 0x06, 5)
        self.assertEqual([0x06, 0x85, 0, 0xEE, 0x0F], payload)


class PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (B.hid, B.hidlist)

    def tearDown(self):
        B.hid, B.hidlist = self._saved

    def poll(self, mv=3975):
        dev = FakeDevice(mv)
        B.hid = type("HID", (), {"device": staticmethod(lambda: dev)})
        B.hidlist = type("HL", (), {
            "enumerate": staticmethod(lambda vid=0: list(INFO))})
        return B.BarracudaX0550Provider().poll(), dev

    def test_the_ystech_dongle_is_read(self):
        out, dev = self.poll()
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.online) for s in out],
                         [("barracuda-x:0550", "Razer Barracuda X (2022)", 72, False, True)])
        self.assertEqual([b"ys-tech"], dev.opened)
        self.assertTrue(dev.closed)

    def test_charging_is_inferred_from_the_sample_spread(self):
        provider = B.BarracudaX0550Provider()
        dev = FakeDevice(3975)
        B.hid = type("HID", (), {"device": staticmethod(lambda: dev)})
        B.hidlist = type("HL", (), {
            "enumerate": staticmethod(lambda vid=0: list(INFO))})
        # the fake returns one voltage for all samples, so this documents the
        # steady state rather than re-implementing the reference's jitter
        out = provider.poll()
        self.assertFalse(out[0].charging)

    def test_no_receiver_is_harmless(self):
        dev = FakeDevice()
        B.hid = type("HID", (), {"device": staticmethod(lambda: dev)})
        B.hidlist = type("HL", (), {"enumerate": staticmethod(lambda vid=0: [])})
        self.assertEqual([], B.BarracudaX0550Provider().poll())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
