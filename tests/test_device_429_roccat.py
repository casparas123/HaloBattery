"""ROCCAT Vulcan II Mini Air over its 2.4 GHz receiver (#429)."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import roccat as R  # noqa: E402


INFO = [{"path": b"nrf", "interface_number": 2, "usage_page": R.USAGE_PAGE,
         "usage": R.USAGE, "product_id": 0x3AA9,
         "product_string": "ROCCAT Vulcan II Mini Air"}]


class FakeDevice:
    def __init__(self, reports):
        self.reports = list(reports)
        self.feature_writes = []
        self.closed = False

    def open_path(self, path):
        self.path = path

    def send_feature_report(self, data):
        self.feature_writes.append(bytes(data))
        return len(data)

    def read(self, length, timeout_ms):
        return self.reports.pop(0) if self.reports else []

    def close(self):
        self.closed = True


def frame(*body):
    return bytes(body).ljust(64, b"\0")


def reply(level=43):
    return frame(0x07, 0x04, 0x88, 0x00, 0x41, level)


def push(byte8=25):
    return frame(0x09, 0x0A, 0x88, 0x05, 0x43, 0x8B, 0x01, 0x01, byte8)


class QueryTests(unittest.TestCase):
    def test_the_query_is_the_captured_set_report(self):
        q = R.make_query()
        self.assertEqual(64, len(q))
        self.assertEqual("07 04 88 00 01", q[:5].hex(" "))
        self.assertEqual(bytes(59), q[5:])

    def test_the_captured_reply_reads_43(self):
        # #429 capture: `07 04 88 00 41 2b`, Swarm on the receiver.
        self.assertEqual(43, R.parse_reply(reply(0x2B)))

    def test_settings_answers_are_not_levels(self):
        # Same shape, different command: `07 04 33 01 41 7f` is DPI/brightness.
        for body in ((0x07, 0x04, 0x33, 0x01, 0x41, 0x7F),
                     (0x07, 0x04, 0x88, 0x00, 0x43, 0x2B, 0x01),
                     (0x07, 0x04, 0x88, 0x00, 0x41, 0x65)):
            with self.subTest(body=body):
                self.assertIsNone(R.parse_reply(frame(*body)))

    def test_the_push_candidate_is_byte_eight(self):
        self.assertEqual(25, R.parse_push(push(25)))
        self.assertEqual(38, R.parse_push(push(38)))
        self.assertIsNone(R.parse_push(reply(43)))


class PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (R.hid, R.hidlist)

    def tearDown(self):
        R.hid, R.hidlist = self._saved

    def poll(self, reports, infos=None):
        dev = FakeDevice(reports)
        R.hid = types.SimpleNamespace(device=lambda: dev)
        R.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: list(INFO if infos is None else infos))
        self.provider = R.RoccatProvider()
        return self.provider.poll(), dev

    def test_the_receiver_reads_one_keyboard_without_charging(self):
        out, dev = self.poll([reply(0x2B)])
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.charging_known, s.kind)
                          for s in out],
                         [("roccat:vulcan-mini-air", "ROCCAT Vulcan II Mini Air",
                           43, False, False, "keyboard")])
        self.assertEqual("07 04 88 00 01", dev.feature_writes[0][:5].hex(" "))
        self.assertTrue(dev.closed)

    def test_a_push_before_the_reply_is_skipped_and_logged(self):
        out, _ = self.poll([push(25), reply(0x2B)])
        self.assertEqual([s.level for s in out], [43])
        log = "\n".join(self.provider.diagnostics())
        self.assertIn("candidate B (push byte 8): 25", log)
        self.assertIn("candidate A (0x88 reply): 43", log)

    def test_no_reply_creates_no_icon(self):
        out, dev = self.poll([])
        self.assertEqual(out, [])
        self.assertIn("no level reply", "\n".join(self.provider.diagnostics()))
        self.assertEqual(1, len(dev.feature_writes))

    def test_the_turtle_beach_pid_is_accepted(self):
        out, _ = self.poll([reply(0x2B)], [dict(INFO[0], product_id=0x501E)])
        self.assertEqual([s.level for s in out], [43])

    def test_wrong_collection_is_not_opened(self):
        out, dev = self.poll([reply(0x2B)], [dict(INFO[0], usage_page=0x0001, usage=0x0006)])
        self.assertEqual(out, [])
        self.assertEqual(dev.feature_writes, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
