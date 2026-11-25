"""Darmoshark N5 / MOJI 1K NRF receiver battery read (#430)."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import darmoshark as D  # noqa: E402


def response(level=67, v2=False):
    frame = bytearray(64)
    frame[0] = D.TYPE_POWER | D.WIRELESS_FLAG
    frame[3] = D.SUB_POWER
    if v2:
        frame[6], frame[7] = 0x15, 0x19
        frame[11] = level
    else:
        frame[6] = level
    return bytes(frame)


class FakeDevice:
    def __init__(self, replies):
        self.replies = list(replies)
        self.writes = []
        self.closed = False

    def open_path(self, path):
        self.path = path

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def read(self, length, timeout_ms):
        return self.replies.pop(0) if self.replies else []

    def close(self):
        self.closed = True


INFO = [{"path": b"nrf", "interface_number": 3, "usage_page": D.USAGE_PAGE,
         "usage": D.USAGE, "product_id": D.RECEIVER_PID,
         "product_string": "1K NRF Receiver"}]


class QueryTests(unittest.TestCase):
    def test_the_query_is_the_openlogi_power_frame(self):
        q = D.make_query(True)
        self.assertEqual((0x41, 0x00, 129, 1), tuple(q[:4]))
        self.assertEqual(222, q[63])
        self.assertEqual(0xA1, sum(q) & 0xFF)

    def test_both_response_layouts(self):
        self.assertEqual(67, D.parse_reply(response(67)))
        self.assertEqual(42, D.parse_reply(response(42, v2=True)))

    def test_bad_lengths_and_levels_are_refused(self):
        for frame in (b"", response(101), response(101, v2=True)):
            with self.subTest(frame=frame):
                self.assertIsNone(D.parse_reply(frame))
        bad = bytearray(response(67))
        bad[3] = 2
        self.assertIsNone(D.parse_reply(bad))


class PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (D.hid, D.hidlist)

    def tearDown(self):
        D.hid, D.hidlist = self._saved

    def poll(self, replies, infos=None):
        dev = FakeDevice(replies)
        D.hid = types.SimpleNamespace(device=lambda: dev)
        D.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(INFO if infos is None else infos))
        self.provider = D.DarmosharkProvider()
        return self.provider.poll(), dev

    def test_the_receiver_reads_one_icon_on_battery(self):
        out, dev = self.poll([response(67)])
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.charging_known, s.kind)
                          for s in out],
                         [("darmoshark:n5", "Darmoshark N5", 67, False, True, "mouse")])
        self.assertEqual([0x41, 0, 129, 1], list(dev.writes[0][:4]))
        self.assertTrue(dev.closed)

    def test_v2_receiver_reads_byte_eleven(self):
        out, _ = self.poll([response(42, v2=True)])
        self.assertEqual(out[0].level, 42)

    def test_no_reply_does_not_create_an_icon(self):
        out, _ = self.poll([])
        self.assertEqual(out, [])

    def test_a_silent_first_query_is_retried_and_reads_the_level(self):
        # #430 test build 2: the receiver acknowledged the query (`1f 00 81 01`)
        # and the mouse answered nothing. A second query must be sent within the
        # same poll rather than waiting for the next one.
        ack = bytes.fromhex("1f 00 81 01").ljust(64, b"\0")
        captured = bytes.fromhex("41 00 8e 01 00 00 3f 00 01 02 03").ljust(64, b"\0")
        out, dev = self.poll([ack, [], [], ack, captured])
        self.assertEqual([s.level for s in out], [63])
        self.assertEqual(2, len(dev.writes))

    def test_three_silent_queries_say_so_in_the_diagnostics(self):
        out, dev = self.poll([])
        self.assertEqual(out, [])
        self.assertEqual(3, len(dev.writes))
        self.assertIn("no power reply after 3 queries",
                      "\n".join(self.provider.diagnostics()))

    def test_a_silent_poll_keeps_the_last_reading_greyed(self):
        # The receiver stays plugged in while the mouse sleeps. The icon must
        # not disappear just because one poll missed the mouse's answer.
        dev = FakeDevice([response(67)])
        D.hid = types.SimpleNamespace(device=lambda: dev)
        D.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(INFO))
        provider = D.DarmosharkProvider()
        first = provider.poll()
        self.assertEqual([(s.level, s.online) for s in first], [(67, True)])
        second = provider.poll()
        self.assertEqual([(s.level, s.online, s.charging, s.charging_known, s.wake_on_input)
                          for s in second],
                         [(67, False, False, False, True)])
        self.assertIn("keeping 67% greyed", "\n".join(provider.diagnostics()))

    def test_unplugging_the_receiver_drops_the_cached_reading(self):
        dev = FakeDevice([response(67)])
        D.hid = types.SimpleNamespace(device=lambda: dev)
        D.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(INFO))
        provider = D.DarmosharkProvider()
        provider.poll()
        D.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: [])
        self.assertEqual(provider.poll(), [])
        self.assertIsNone(provider._last)

    def test_the_captured_430_exchange_reads_63_on_battery(self):
        # #430 test build 1 diagnostics: the ack `1f 00 81 01 ...`, then the power
        # reply `41 00 8e 01 00 00 3f 00 01 02 03 ...` (63 %, the mouse showed 63 %).
        # Byte 5 is 0, which the vendor web app shows as the plain percentage.
        ack = bytes.fromhex("1f 00 81 01").ljust(64, b"\0")
        captured = bytes.fromhex("41 00 8e 01 00 00 3f 00 01 02 03").ljust(64, b"\0")
        out, _ = self.poll([ack, captured])
        self.assertEqual([(s.level, s.charging, s.charging_known) for s in out],
                         [(63, False, True)])

    def test_the_charging_state_follows_the_vendor_web_app(self):
        # The web app's template shows "Charging" for 1 and 3, "Fully charged" for 2
        # and the percentage for 0 (main.47068c4b56a18ed4.js: `1===powerState||
        # 3===powerState` / `2===powerState`). Any other value stays unknown.
        expected = {0: (False, True), 1: (True, True), 2: (False, True),
                    3: (True, True), 7: (False, False)}
        for state, (charging, known) in expected.items():
            with self.subTest(state=state):
                frame = bytearray(response(55))
                frame[5] = state
                out, _ = self.poll([bytes(frame)])
                self.assertEqual([(s.level, s.charging, s.charging_known) for s in out],
                                 [(55, charging, known)])

    def test_v2_state_is_read_from_byte_ten(self):
        for state, charging in ((1, True), (2, False), (3, False)):
            with self.subTest(state=state):
                frame = bytearray(response(42, v2=True))
                frame[10] = state
                out, _ = self.poll([bytes(frame)])
                self.assertEqual([(s.level, s.charging, s.charging_known) for s in out],
                                 [(42, charging, True)])

    def test_wrong_collection_is_not_opened(self):
        out, dev = self.poll([], [dict(INFO[0], usage_page=0x0001, usage=0x0002)])
        self.assertEqual(out, [])
        self.assertEqual(dev.writes, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
