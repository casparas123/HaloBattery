"""Tests for providers/endorfy.py - ENDORFY LIV Plus Wireless (#444)."""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import endorfy as E  # noqa: E402


def reply(level, charging=False, mv=3900, checksum_xor=0):
    frame = bytearray(17)
    frame[0] = 0x08
    frame[1] = 0x04
    frame[6] = level
    frame[7] = 1 if charging else 0
    frame[8:10] = int(mv).to_bytes(2, "big")
    from providers.compx import checksum
    frame[16] = checksum(list(frame[:16])) ^ checksum_xor
    return bytes(frame)


class FakeDevice:
    def __init__(self, replies):
        self.replies = list(replies)
        self.frames = []
        self.opened = []
        self.closed = False

    def open_path(self, path):
        self.opened.append(path)

    def write(self, data):
        self.frames.append(list(data))
        return len(data)

    def read(self, length, timeout_ms=0):
        if not self.frames:              # the pre-write drain sees no events
            return []
        return self.replies.pop(0) if self.replies else []

    def close(self):
        self.closed = True


class PerPath:
    def __init__(self, devices):
        self.devices = devices
        self.current = None

    def __call__(self):
        outer = self

        class D:
            def open_path(self, path):
                outer.current = outer.devices[path]

            def write(self, data):
                return outer.current.write(data)

            def read(self, length, timeout_ms=0):
                return outer.current.read(length, timeout_ms)

            def close(self):
                outer.current.close()
        return D()


def entries(*pids):
    return [{"path": b"%04x-ff02" % pid, "interface_number": 1,
             "usage_page": 0xFF02, "usage": 0x0002, "product_id": pid,
             "product_string": "LIV Plus Wireless"} for pid in pids]


class ProtocolTests(unittest.TestCase):
    def test_the_query_is_the_shared_compx_frame(self):
        q = E.make_request()
        self.assertEqual((0x08, 0x04), tuple(q[:2]))
        self.assertEqual(17, len(q))
        from providers.compx import checksum
        self.assertEqual(checksum(q[:16]), q[16])

    def test_the_reply_carries_level_and_charging(self):
        self.assertEqual((55, False), E.parse_power(reply(55)))
        self.assertEqual((55, True), E.parse_power(reply(55, True)))

    def test_a_bad_checksum_is_refused(self):
        self.assertIsNone(E.parse_power(reply(55, checksum_xor=0xFF)))

    def test_level_zero_and_hundred_are_valid(self):
        self.assertEqual((0, False), E.parse_power(reply(0)))
        self.assertEqual((100, False), E.parse_power(reply(100)))


class PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (E.hid, E.hidlist, E.output_length)
        E.output_length = lambda path: None

    def tearDown(self):
        E.hid, E.hidlist, E.output_length = self._saved

    def poll(self, replies, infos):
        dev = FakeDevice(replies)
        E.hid = types.SimpleNamespace(device=lambda: dev)
        E.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(infos))
        return E.EndorfyProvider().poll(), dev

    def test_the_receiver_reports_the_level(self):
        out, dev = self.poll([reply(55)], entries(0x00A7))
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.online, s.kind)
                          for s in out],
                         [("endorfy:livplus", "ENDORFY LIV Plus Wireless", 55,
                           False, True, "mouse")])
        self.assertEqual([0x08, 0x04], dev.frames[0][:2])

    def test_the_cable_identity_is_read(self):
        out, dev = self.poll([reply(55, charging=True)], entries(0x00A6))
        self.assertEqual((out[0].level, out[0].charging), (55, True))
        self.assertEqual([b"00a6-ff02"], dev.opened)

    def test_the_receiver_is_tried_before_the_cable(self):
        out, dev = self.poll([reply(60)], entries(0x00A7, 0x00A6))
        self.assertEqual(out[0].level, 60)
        self.assertEqual([b"00a7-ff02"], dev.opened)

    def test_a_silent_receiver_falls_back_to_the_cable(self):
        cable = FakeDevice([reply(48, charging=True)])
        receiver = FakeDevice([])
        E.hid = types.SimpleNamespace(device=PerPath({
            b"00a7-ff02": receiver, b"00a6-ff02": cable}))
        E.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: entries(0x00A7, 0x00A6))
        out = E.EndorfyProvider().poll()
        self.assertEqual([(s.level, s.charging) for s in out], [(48, True)])

    def test_no_device_reports_nothing(self):
        out, _ = self.poll([], [])
        self.assertEqual([], out)

    def test_a_silent_mouse_keeps_the_last_reading_greyed(self):
        infos = entries(0x00A7)
        dev = FakeDevice([reply(55)])
        E.hid = types.SimpleNamespace(device=lambda: dev)
        E.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(infos))
        provider = E.EndorfyProvider()
        provider.poll()
        dev.replies = []
        out = provider.poll()
        self.assertEqual([(s.level, s.online) for s in out], [(55, False)])
        provider._last = (55, False, time.time() - E.ASLEEP_KEEP)
        self.assertEqual([(s.level, s.online) for s in provider.poll()], [(None, False)])


if __name__ == "__main__":
    unittest.main()
