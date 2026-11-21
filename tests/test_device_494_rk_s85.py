"""Tests for providers/rk_s85.py. No hardware is needed."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import rk_s85 as R  # noqa: E402


def reply(level=54, linked=1):
    return [0x13, 0x0A] + [0x00] * 4 + [level, linked] + [0x00] * 12


def offline(level=1):
    """The receiver's answer for a sleeping keyboard: link byte 0 (#494)."""
    return reply(level=level, linked=0)


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

    def test_an_offline_frame_is_not_a_level(self):
        # The reporter's keyboard answered level 1 and 0 while it was at 85 %:
        # the link byte 0 marks the receiver's placeholder, not a reading.
        for level in (0, 1, 85):
            with self.subTest(level=level):
                self.assertIsNone(R.parse_reply(offline(level)))
                self.assertTrue(R.is_offline_frame(offline(level)))
        self.assertEqual((54, True), R.parse_reply(reply(54)))


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

    def test_the_poll_never_shows_an_offline_level(self):
        dev = FakeDevice([offline(1)] * 3)
        out = self.poll([INFO], dev)
        self.assertEqual([(s.level, s.online) for s in out], [(None, False)])
        self.assertIn("offline frame", " ".join(self.provider.diagnostics()))

    def test_the_placeholder_frame_before_a_real_one_is_skipped(self):
        # The receiver can answer the placeholder first and the real status
        # frame inside the same window; the poll must not stop at the first.
        dev = FakeDevice([offline(1), offline(0), reply(82)])
        out = self.poll([INFO], dev)
        self.assertEqual([(s.level, s.online) for s in out], [(82, True)])

    def test_offline_frames_after_a_reading_keep_that_reading(self):
        dev = FakeDevice([reply(82)])
        self.poll([INFO], dev)
        dev.replies = [offline(0)] * 3
        out = self.poll([INFO], dev)
        self.assertEqual([(82, False, False)], [(s.level, s.charging, s.online) for s in out])

    def test_a_cable_offline_frame_is_charging_without_a_level(self):
        dev = FakeDevice([offline(0)] * 3)
        out = self.poll([INFO, CABLE], dev)
        self.assertEqual([(s.level, s.charging, s.online, s.ui_message) for s in out],
                         [(None, True, True, "state.charging_level_unknown")])

    def test_a_cable_offline_frame_shows_the_level_from_before_the_cable(self):
        dev = FakeDevice([reply(82)])
        self.poll([INFO], dev)
        dev.replies = [offline(0)] * 3
        out = self.poll([INFO, CABLE], dev)
        self.assertEqual([(s.level, s.charging, s.ui_message, s.ui_params) for s in out],
                         [(82, True, "state.charging_was", {"level": 82})])

    def test_the_fallback_keeps_a_level_read_on_the_cable_itself(self):
        dev = FakeDevice([reply(83)])
        self.poll([INFO, CABLE], dev)          # a live reading on the cable
        dev.replies = [offline(0)] * 3
        out = self.poll([INFO, CABLE], dev)
        self.assertEqual([(s.level, s.charging, s.ui_message, s.approx) for s in out],
                         [(83, True, "", "")])

    def test_a_stale_last_level_is_not_shown_on_the_cable(self):
        dev = FakeDevice([reply(82)])
        self.poll([INFO], dev)
        self.provider._last = (82, R.time.time() - R.ASLEEP_KEEP - 1, False)
        dev.replies = [offline(0)] * 3
        out = self.poll([INFO, CABLE], dev)
        self.assertEqual([(s.level, s.charging, s.ui_message) for s in out],
                         [(None, True, "state.charging_level_unknown")])

    def test_a_live_cable_reading_needs_no_before_text(self):
        dev = FakeDevice([reply(83)])
        out = self.poll([INFO, CABLE], dev)
        self.assertEqual([(s.level, s.charging, s.ui_message, s.approx) for s in out],
                         [(83, True, "", "")])

    def test_silence_keeps_the_last_level_greyed(self):
        dev = FakeDevice([reply(54)])
        self.assertTrue(self.poll([INFO], dev)[0].online)
        dev.replies = []
        out = self.poll([INFO], dev)
        self.assertEqual([(54, False, False)], [(s.level, s.charging, s.online) for s in out])
        self.assertTrue(out[0].wake_on_input)


class TransportFailureTest(unittest.TestCase):
    """#494 test build 3: the cable id raised "Incorrect function" mid-read and the
    provider was marked I/O unavailable, then switched off after three failures."""

    def setUp(self):
        self._saved = (R.hid, R.hidlist, R.time)

    def tearDown(self):
        R.hid, R.hidlist, R.time = self._saved

    def poll(self, device, infos):
        R.hid = types.SimpleNamespace(device=lambda: device)
        R.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(infos))
        return R.RkS85Provider().poll()

    def test_a_read_error_keeps_the_icon_instead_of_raising(self):
        class BadRead:
            def open_path(self, path):
                pass

            def write(self, data):
                return len(data)

            def read(self, size, timeout=0):
                raise OSError("native I/O failed: (0x1) Incorrect function")

            def close(self):
                pass

        infos = [{"product_id": R.PID, "usage_page": 0xFF02, "usage": 0x0002,
                  "path": b"receiver"}]
        out = self.poll(BadRead(), infos)
        self.assertEqual(1, len(out))
        self.assertFalse(out[0].online)

    def test_the_cable_id_that_fails_still_reads_as_charging(self):
        class BadRead:
            def open_path(self, path):
                pass

            def write(self, data):
                return len(data)

            def read(self, size, timeout=0):
                raise OSError("native I/O failed: (0x1) Incorrect function")

            def close(self):
                pass

        infos = [{"product_id": R.CABLE_PID, "usage_page": 0xFF02, "usage": 0x0002,
                  "path": b"cable"}]
        out = self.poll(BadRead(), infos)
        self.assertEqual(1, len(out))
        self.assertTrue(out[0].charging)
        self.assertTrue(out[0].online)

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
