"""Tests for providers/airpods.py. No hardware and no bleak needed.

The payload is the live capture from 2026-09-26: an AirPods 3 advertising
`07 19 01 13 20 2b 98 8f ...`, decoded as left 90 %, right 80 %, case 20 %
with both buds charging. Windows APIs hand the manufacturer data over without
the 4C 00 company prefix, so the offsets are two lower than in a raw capture.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import airpods as A  # noqa: E402

CAPTURED = bytes.fromhex("07 19 01 13 20 2b 98 8f")


def packet(model=0x2013, status=0x2B, battery=0x98, lid=0x8F):
    return bytes([0x07, 0x19, 0x01, model & 0xFF, (model >> 8) & 0xFF, status, battery, lid])


class ParseTest(unittest.TestCase):
    def test_the_captured_packet_decodes(self):
        p = A.parse_proximity(CAPTURED)
        self.assertEqual(p["model"], "AirPods 3")
        self.assertEqual((p["left"], p["right"], p["case"]), (90, 80, 20))
        self.assertEqual(p["charging"], {"case": False, "left": True, "right": True})
        self.assertEqual((p["lid_open"], p["left_in_ear"], p["right_in_ear"]),
                         (True, True, True))     # as the references read 0x8f: "open, both in ear"

    def test_a_pro_2_model_id(self):
        self.assertEqual(A.parse_proximity(packet(model=0x2014))["model"], "AirPods Pro 2")

    def test_an_unknown_model_id_names_the_number(self):
        self.assertEqual(A.parse_proximity(packet(model=0x9999))["model"], "AirPods (0x9999)")

    def test_an_unknown_nibble_is_unknown_not_150(self):
        p = A.parse_proximity(packet(battery=0xF8, status=0xF8))
        self.assertEqual((p["left"], p["right"], p["case"]), (None, 80, None))

    def test_a_short_or_foreign_packet_is_refused(self):
        self.assertIsNone(A.parse_proximity(b""))
        self.assertIsNone(A.parse_proximity(b"\x07\x19\x01"))
        self.assertIsNone(A.parse_proximity(bytes([0x06]) + CAPTURED[1:]))
        self.assertIsNone(A.parse_proximity(None))


class PollTest(unittest.TestCase):
    def setUp(self):
        self._saved = (A.bleak, A.time)
        self.now = [1000.0]
        A.time = types.SimpleNamespace(time=lambda: self.now[0])
        self.provider = A.AirPodsProvider()

    def tearDown(self):
        A.bleak, A.time = self._saved

    def poll(self, found):
        async def fake_scan(seconds):
            return found

        A.bleak = types.SimpleNamespace()          # "installed"
        A._scan = fake_scan
        return self.provider.poll()

    def entry(self, rssi=-55, address="AA:BB:CC:DD:EE:FF"):
        return {address: (rssi, "Someone's AirPods", A.parse_proximity(CAPTURED))}

    def test_a_close_pair_is_shown_with_the_weaker_bud(self):
        out = self.poll(self.entry())
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.online, s.source, s.kind)
                          for s in out],
                         [("airpods:AA:BB:CC:DD:EE:FF", "AirPods 3 (AA:BB:CC:DD:EE:FF)",
                           80, True, True, "airpods", "headset")])

    def test_a_weaker_pair_is_ignored_with_a_reason(self):
        out = self.poll(self.entry(rssi=-75))
        self.assertEqual(out, [])
        self.assertTrue(any("dBm" in line for line in self.provider.diagnostics()))

    def test_charging_follows_the_buds_not_the_case(self):
        # case charging only (status 0x24): the buds are not charging
        found = {"AA": (-55, "", A.parse_proximity(bytes.fromhex("07 19 01 13 20 24 98 8f")))}
        out = self.poll(found)
        self.assertEqual([(s.level, s.charging) for s in out], [(80, False)])

    def test_a_pair_without_a_bud_level_is_skipped(self):
        found = {"AA": (-55, "", A.parse_proximity(bytes.fromhex("07 19 01 13 20 2f ff 8f")))}
        self.assertEqual(self.poll(found), [])

    def test_a_silent_scan_keeps_the_pair_for_a_while_then_drops_it(self):
        self.assertEqual(len(self.poll(self.entry())), 1)
        self.now[0] += A.KEEP_SECONDS - 1
        kept = self.poll({})
        self.assertEqual([(s.level, s.online) for s in kept], [(80, True)])
        self.now[0] += 2
        self.assertEqual(self.poll({}), [])

    def test_without_bleak_it_reports_instead_of_breaking(self):
        A.bleak = None
        out = self.provider.poll()
        self.assertEqual(out, [])
        self.assertTrue(any("bleak" in line for line in self.provider.diagnostics()))

    def test_a_scan_failure_is_reported(self):
        async def boom(seconds):
            raise RuntimeError("adapter off")

        A.bleak = types.SimpleNamespace()
        A._scan = boom
        out = self.provider.poll()
        self.assertEqual(out, [])
        self.assertTrue(any("adapter off" in line for line in self.provider.diagnostics()))


if __name__ == "__main__":
    unittest.main()
