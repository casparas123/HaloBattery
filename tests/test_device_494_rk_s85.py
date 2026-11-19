"""Tests for providers/rk_s85.py. No hardware is needed."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import rk_s85 as R  # noqa: E402


def reply(level=54, linked=1):
    return [0x13, 0x0A] + [0x00] * 4 + [level, linked] + [0x00] * 12


INFO = {
    "path": b"rk-s85",
    "product_id": R.PID,
    "product_string": "Gaming KB",
    "usage_page": 0xFF02,
    "usage": 0x0002,
}


class ParseTest(unittest.TestCase):
    def test_the_status_frame(self):
        self.assertEqual((54, True), R.parse_reply(reply()))

    def test_other_frames_and_levels_are_refused(self):
        self.assertIsNone(R.parse_reply([0x13, 0x05, 0x01]))
        self.assertIsNone(R.parse_reply(reply(101)))
        bad = reply()
        bad[1] = 0x05
        self.assertIsNone(R.parse_reply(bad))


CABLE = dict(INFO, product_id=R.CABLE_PID, product_string="RK S85")


class FakeDevice:
    def __init__(self, replies):
        self.replies = list(replies)
        self.writes = []
        self.closed = 0

    def open_path(self, path):
        self.path = path

    def write(self, data):
        self.writes.append(list(data))
        return len(data)

    def read(self, size, timeout):
        return self.replies.pop(0) if self.replies else []

    def close(self):
        self.closed += 1


class PollTest(unittest.TestCase):
    def setUp(self):
        self.saved = (R.hid, R.hidlist)
        self.provider = R.RkS85Provider()

    def tearDown(self):
        R.hid, R.hidlist = self.saved

    def poll(self, infos, dev):
        R.hid = types.SimpleNamespace(device=lambda: dev)
        R.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(infos))
        return self.provider.poll()

    def test_the_keyboard_level_and_query(self):
        dev = FakeDevice([reply()])
        out = self.poll([INFO], dev)
        self.assertEqual([(s.key, s.name, s.level, s.online, s.kind) for s in out],
                         [("rk:s85", "Royal Kludge S85", 54, True, "keyboard")])
        self.assertEqual([R.REQUEST], dev.writes)
        self.assertTrue(out[0].charging_known is False)

    def test_the_cable_id_is_charging_and_preferred(self):
        dev = FakeDevice([reply(53)])
        out = self.poll([INFO, CABLE], dev)
        self.assertEqual([(s.level, s.charging, s.online, s.via) for s in out],
                         [(53, True, True, "USB cable")])

    def test_a_wrong_product_or_collection_is_not_opened(self):
        dev = FakeDevice([reply()])
        wrong = dict(INFO, product_id=0x010C)
        self.assertEqual(self.poll([wrong], dev), [])
        self.assertEqual(dev.writes, [])

    def test_silence_keeps_the_last_level_greyed(self):
        dev = FakeDevice([reply(54)])
        self.assertTrue(self.poll([INFO], dev)[0].online)
        dev.replies = []
        out = self.poll([INFO], dev)
        self.assertEqual([(54, False, False)], [(s.level, s.charging, s.online) for s in out])
        self.assertTrue(out[0].wake_on_input)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
