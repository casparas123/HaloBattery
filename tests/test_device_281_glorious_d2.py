"""Tests for providers/glorious_pixart.py. No hardware is needed.

The D2 Wireless receiver (`093a:824d`) uses two split collections: the
GetFeature trigger on `ff00:0001` and the input reply on `ff00:ff00`.  This is
GloriousBatteryMonitor-Go's CandidateF; its CandidateD GetFeature-only path
returns firmware status on this shape and is not used.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import glorious_pixart as G  # noqa: E402


def reading(level=80):
    return [0x06, 0xFB, level, 0x01] + [0x00] * 4


def entries(pid=G.PID_RECEIVER):
    return [
        {"path": b"trigger", "interface_number": 1, "product_id": pid,
         "usage_page": 0xFF00, "usage": 0x0001, "product_string": "Model D 2 Wireless"},
        {"path": b"input", "interface_number": 1, "product_id": pid,
         "usage_page": 0xFF00, "usage": 0xFF00, "product_string": "Model D 2 Wireless"},
    ]


class FakeHid:
    def __init__(self, replies=None, trigger_error=True, passive=None):
        self.replies = list(replies or [])
        self.passive = passive
        self.passive_used = False
        self.trigger_error = trigger_error
        self.opened = []
        self.triggers = []
        self.reads = []

    def device(self):
        return FakeHandle(self)


class FakeHandle:
    def __init__(self, owner):
        self.owner = owner
        self.path = None

    def open_path(self, path):
        self.path = path
        self.owner.opened.append(path)

    def get_feature_report(self, rid, length):
        self.owner.triggers.append((rid, length))
        if self.owner.trigger_error:
            raise OSError("feature read rejected")
        return []

    def read(self, size, timeout_ms=0):
        self.owner.reads.append((self.path, size, timeout_ms))
        if not self.owner.passive_used:
            self.owner.passive_used = True
            return self.owner.passive or []
        return self.owner.replies.pop(0) if self.owner.replies else []

    def send_feature_report(self, data):
        raise AssertionError("the D2 reader must not write to the mouse")

    def write(self, data):
        raise AssertionError("the D2 reader must not write to the mouse")

    def close(self):
        pass


class ParseTests(unittest.TestCase):
    def test_the_normal_col05_frame_carries_the_level(self):
        self.assertEqual(G.parse_reading(reading(80)), 80)
        self.assertEqual(G.parse_reading(reading(55)), 55)

    def test_a_zero_or_out_of_range_level_is_refused(self):
        self.assertIsNone(G.parse_reading(reading(0)))
        self.assertIsNone(G.parse_reading(reading(101)))
        self.assertIsNone(G.parse_reading([]))
        self.assertIsNone(G.parse_reading([0x06]))

    def test_other_firmware_may_carry_the_level_later_in_the_frame(self):
        self.assertEqual(G.parse_reading([0x00, 0x00, 0x00, 77, 0x00]), 77)


class PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (G.hid, G.hidlist, G.time)
        self.now = [1000.0]

    def tearDown(self):
        G.hid, G.hidlist, G.time = self._saved

    def make(self, fake, infos=None):
        G.time = types.SimpleNamespace(
            time=lambda: self.now[0],
            sleep=lambda s: None)
        G.hid = types.SimpleNamespace(device=lambda: fake.device())
        G.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: list(entries() if infos is None else infos))
        return G.GloriousD2Provider()

    def test_the_d2_is_read_by_a_trigger_and_a_col05_reply(self):
        fake = FakeHid([reading(80)])
        out = self.make(fake).poll()
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.online, s.kind)
                          for s in out],
                         [("glorious:d2", "Glorious Model D2 Wireless",
                           80, False, True, "mouse")])
        self.assertEqual(fake.triggers[0], (0x03, 64))
        self.assertEqual(fake.reads[0], (b"input", 8, 4000))   # passive
        self.assertEqual(fake.reads[1], (b"input", 8, 6000))   # after the trigger

    def test_a_passive_col05_push_is_used_without_a_trigger(self):
        fake = FakeHid(passive=reading(88))
        out = self.make(fake).poll()
        self.assertEqual([(r.level, r.online) for r in out], [(88, True)])
        self.assertEqual(fake.triggers, [])
        self.assertEqual(fake.reads[0][0], b"input")

    def test_a_rejected_trigger_still_reads_the_col05_reply(self):
        fake = FakeHid([reading(64)], trigger_error=True)
        provider = self.make(fake)
        self.assertEqual(provider.poll()[0].level, 64)
        self.assertTrue(any("feature read rejected" in line
                            for line in provider.diagnostics()))

    def test_a_missing_reply_is_retried_with_the_fallback_report_ids(self):
        fake = FakeHid([])
        provider = self.make(fake)
        provider.poll()
        # the four fallbacks, then the probe burst's rounds (#281)
        self.assertEqual([rid for rid, _ in fake.triggers],
                         [0x03, 0x00, 0x01, 0x02] + [0x03] * G.PROBE_ROUNDS)
        self.assertIn("probe: no answer in any round",
                      "\n".join(provider.diagnostics()))

    def test_the_probe_burst_recovers_a_level(self):
        # four empty cross reads, then the probe's first read carries the level
        fake = FakeHid([[], [], [], [], reading(77)])
        provider = self.make(fake)
        out = provider.poll()
        self.assertEqual((out[0].level, out[0].online), (77, True))
        self.assertIn("probe: answered 77% on round 1",
                      "\n".join(provider.diagnostics()))

    def test_the_cable_counts_as_charging(self):
        fake = FakeHid([reading(80)])
        out = self.make(fake, infos=entries(G.PID_CABLE)).poll()
        self.assertEqual((out[0].level, out[0].charging, out[0].online), (80, True, True))

    def test_the_cable_wins_when_the_receiver_is_also_present(self):
        both = [
            {"path": b"trigger-cable", "interface_number": 1, "product_id": G.PID_CABLE,
             "usage_page": 0xFF00, "usage": 0x0001, "product_string": "Model D 2 Wireless"},
            {"path": b"input-cable", "interface_number": 1, "product_id": G.PID_CABLE,
             "usage_page": 0xFF00, "usage": 0xFF00, "product_string": "Model D 2 Wireless"},
            {"path": b"trigger-receiver", "interface_number": 1, "product_id": G.PID_RECEIVER,
             "usage_page": 0xFF00, "usage": 0x0001, "product_string": "Model D 2 Wireless"},
            {"path": b"input-receiver", "interface_number": 1, "product_id": G.PID_RECEIVER,
             "usage_page": 0xFF00, "usage": 0xFF00, "product_string": "Model D 2 Wireless"},
        ]
        fake = FakeHid([reading(70), reading(80)])
        out = self.make(fake, infos=both).poll()
        self.assertEqual([(s.level, s.charging, s.online) for s in out], [(70, True, True)])
        self.assertEqual(fake.reads[0][0], b"input-cable")

    def test_the_cable_alone_keeps_the_last_level_and_says_charging(self):
        fake = FakeHid([reading(80)])
        provider = self.make(fake)
        self.assertEqual(provider.poll()[0].level, 80)

        fake.replies = []
        G.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: list(entries(G.PID_CABLE)))
        out = provider.poll()
        self.assertEqual((out[0].level, out[0].charging, out[0].online), (80, True, True))

    def test_no_reply_keeps_the_last_reading_greyed(self):
        fake = FakeHid([reading(80)])
        provider = self.make(fake)
        self.assertEqual(provider.poll()[0].level, 80)

        fake.replies = []
        self.now[0] += 30
        out = provider.poll()
        self.assertEqual([(80, False, False)],
                         [(s.level, s.charging, s.online) for s in out])
        self.assertTrue(out[0].wake_on_input)
        self.now[0] += G.ASLEEP_KEEP + 1
        out = provider.poll()
        self.assertEqual([(None, False)], [(s.level, s.online) for s in out])
        self.assertTrue(out[0].wake_on_input)

    def test_no_receiver_reports_nothing(self):
        provider = self.make(FakeHid([]), infos=[])
        self.assertEqual([], provider.poll())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
