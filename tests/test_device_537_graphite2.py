"""Tests for providers/graphite2.py (IO by Red Square Graphite 2 Pro Wireless, #537).

No hardware: the dongle's HID enumeration and a fake 64-byte report 0x55 channel
stand in for the device. The frame is the C-Media audio status read already used
for the MCHOSE X9 (providers/mchose.py): write 55 65 01 00 ..., answer
55 65 <level> <state> ...

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import graphite2 as G  # noqa: E402


def reply(level=72, state=G.DISCHARGING):
    r = [0] * 64
    r[0], r[1], r[2], r[3] = G.REPORT, G.REPLY_ID, level, state
    return r


def entry(page=0xFF90, usage=0x0001, iface=0, pid=G.PID, product="Graphite 2 Pro Wireless",
          vid=G.VID):
    return {"vendor_id": vid, "product_id": pid, "interface_number": iface,
            "usage_page": page, "usage": usage, "path": f"{page:04x}:{usage:04x}".encode(),
            "product_string": product}


class FakeDev:
    """One open collection: records writes, answers from `answers` by collection path."""

    def __init__(self, world):
        self.world = world
        self.path = None
        self.queue = []

    def open_path(self, path):
        if path in self.world.refuse_open:
            raise OSError("access denied")
        self.path = path
        self.world.opened.append(path)
        self.queue = list(self.world.queued.get(path, []))

    def write(self, data):
        data = bytes(data)
        self.world.sent.append((self.path, data))
        if data[:2] != bytes([G.REPORT, 0x65]):
            self.world.strange.append((self.path, data))
        if self.path in self.world.refuse_write:
            return -1
        self.queue += self.world.answers.get(self.path, [])
        return len(data)

    def read(self, size, timeout_ms=0):
        self.world.timeouts.append(timeout_ms)
        return self.queue.pop(0) if self.queue else []

    def close(self):
        self.world.closed.append(self.path)


class World:
    def __init__(self, answers=None, queued=None, refuse_open=(), refuse_write=()):
        self.answers = answers or {}
        self.queued = queued or {}
        self.refuse_open = set(refuse_open)
        self.refuse_write = set(refuse_write)
        self.opened, self.closed, self.sent, self.timeouts, self.strange = [], [], [], [], []


def world_for(page=0xFF90, *answers):
    # no answers given means "present, but nothing arrives": an explicit empty list
    return World({f"{page:04x}:0001".encode(): list(answers)})


class ParseTests(unittest.TestCase):
    def test_level_and_state_are_read_from_the_answer(self):
        self.assertEqual(G.parse_status(reply(72, G.DISCHARGING)), (72, False, False))
        self.assertEqual(G.parse_status(reply(44, G.CHARGING)), (44, True, True))
        self.assertEqual(G.parse_status(reply(100, G.FULL)), (100, False, False))

    def test_a_sleeping_headset_is_an_answer_without_a_level(self):
        self.assertEqual(G.parse_status(reply(0, G.ASLEEP)), (None, False, False))

    def test_malformed_answers_are_refused(self):
        self.assertIsNone(G.parse_status([]))
        self.assertIsNone(G.parse_status(None))
        self.assertIsNone(G.parse_status(reply(72)[:3]))                 # no state byte
        self.assertIsNone(G.parse_status([0x00] + reply(72)[1:]))        # not report 0x55
        self.assertIsNone(G.parse_status(reply(72, 7)))                  # unknown state
        self.assertIsNone(G.parse_status(reply(101, G.DISCHARGING)))     # not a level
        self.assertIsNone(G.parse_status([0x55, 0x66, 72, 2] + [0] * 60))  # not a status reply

    def test_the_request_is_the_family_frame(self):
        self.assertEqual(bytes([G.REPORT]) + G.REQUEST,
                         bytes([0x55, 0x65, 0x01]) + bytes(61))
        self.assertEqual(len(G.REQUEST), 63)

    def test_only_the_dongle_id_and_product_string_are_claimed(self):
        self.assertTrue(G.is_graphite(entry()))
        self.assertTrue(G.is_graphite(entry(product="  graphite 2 pro WIRELESS ")))
        self.assertFalse(G.is_graphite(entry(product="Graphite 2 Pro")))          # no "Wireless"
        self.assertFalse(G.is_graphite(entry(pid=0x4154)))                        # another pid
        self.assertFalse(G.is_graphite(entry(vid=0x4C4B)))
        self.assertFalse(G.is_graphite({"vendor_id": G.VID, "product_id": True,
                                        "product_string": "Graphite 2 Pro Wireless"}))


class PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (G.hid, G.hidlist.enumerate)

    def tearDown(self):
        G.hid, G.hidlist.enumerate = self._saved

    def poll(self, world, infos=None):
        fake = FakeDev(world)
        G.hid = types.SimpleNamespace(device=lambda: fake)
        G.hidlist.enumerate = lambda vid=0: [d for d in (infos if infos is not None
                                                          else [entry()])
                                              if d["vendor_id"] == vid]
        p = G.GraphiteProvider()
        return p, p.poll()

    def test_the_answer_becomes_the_headset_reading(self):
        world = world_for(0xFF90, reply(72))
        p, out = self.poll(world)
        [st] = out
        self.assertEqual((st.key, st.name, st.level, st.charging, st.online, st.kind),
                         ("graphite2:4155", "IO Graphite 2 Pro", 72, False, True, "headset"))
        self.assertFalse(st.charging_known)
        self.assertEqual(world.opened, [b"ff90:0001"])
        self.assertIn("answered: 55 65 48 02", "\n".join(p.diagnostics()))

    def test_charging_state_is_reported_when_the_headset_reports_it(self):
        _p, out = self.poll(world_for(0xFF90, reply(55, G.CHARGING)))
        [st] = out
        self.assertEqual((st.level, st.charging, st.charging_known), (55, True, True))

    def test_only_the_status_request_is_sent_and_it_is_full_length(self):
        world = world_for(0xFF90, reply(72))
        self.poll(world)
        [(path, data)] = world.sent
        self.assertEqual(path, b"ff90:0001")
        self.assertEqual(len(data), 64)
        self.assertEqual(data[:3], bytes([0x55, 0x65, 0x01]))
        self.assertEqual(world.strange, [])
        self.assertTrue(all(0 < t <= G.READ_MS for t in world.timeouts))

    def test_the_second_collection_is_tried_when_the_first_is_absent(self):
        world = world_for(0xFF82, reply(81))
        p, out = self.poll(world, infos=[entry(page=0xFF82)])
        self.assertEqual(out[0].level, 81)
        self.assertEqual(world.opened, [b"ff82:0001"])
        self.assertIn("ff90:0001 not enumerated", "\n".join(p.diagnostics()))

    def test_the_second_collection_is_tried_when_the_first_refuses_the_write(self):
        world = World({b"ff82:0001": [reply(90)]}, refuse_write=[b"ff90:0001"])
        p, out = self.poll(world, infos=[entry(page=0xFF90), entry(page=0xFF82)])
        self.assertEqual(out[0].level, 90)
        self.assertEqual(world.opened, [b"ff90:0001", b"ff82:0001"])
        self.assertIn("cannot carry report 0x55", "\n".join(p.diagnostics()))

    def test_a_dongle_without_both_collections_keeps_the_icon_without_a_level(self):
        # the reporter saw enumerations with only the standard audio/HID pages: the
        # dongle is there, the headset is not linked, nothing is sent
        world = World()
        p, out = self.poll(world, infos=[entry(page=0x000C)])
        [st] = out
        self.assertEqual((st.level, st.online), (None, False))
        self.assertEqual(world.sent, [])
        self.assertIn("not linked to the dongle", "\n".join(p.diagnostics()))

    def test_no_dongle_no_icon(self):
        _p, out = self.poll(World(), infos=[entry(vid=0x046D, product="G502 HERO")])
        self.assertEqual(out, [])

    def test_a_foreign_product_on_the_same_id_is_ignored(self):
        # 4c4a is JieLi's stack: the product string must pin the model
        _p, out = self.poll(World(), infos=[entry(product="Graphite 2 Pro")])
        self.assertEqual(out, [])

    def test_asleep_keeps_a_recent_level_greyed(self):
        p, out = self.poll(world_for(0xFF90, reply(70)))
        self.assertEqual(out[0].level, 70)
        # the headset sleeps moments later: its level is still shown, greyed out
        p._last = (70, False, time.time() - 10, False)
        world = world_for(0xFF90, reply(0, G.ASLEEP))
        fake = FakeDev(world)
        G.hid = types.SimpleNamespace(device=lambda: fake)
        G.hidlist.enumerate = lambda vid=0: [entry()]
        out = p.poll()
        [st] = out
        self.assertEqual((st.level, st.online), (70, False))
        self.assertFalse(st.charging_known)

    def test_asleep_with_no_earlier_level_shows_the_icon_without_a_number(self):
        world = world_for(0xFF90, reply(0, G.ASLEEP))
        p, out = self.poll(world)
        [st] = out
        self.assertEqual((st.level, st.online), (None, False))
        self.assertIn("asleep or out of range", "\n".join(p.diagnostics()))

    def test_a_level_from_long_ago_is_not_shown_again(self):
        # five minutes after the last answer the icon stays (the dongle is alive) but
        # without a number: the old level is not evidence of the current charge
        p = G.GraphiteProvider()
        p._last = (70, False, 0.0, False)
        G.hidlist.enumerate = lambda vid=0: [entry()]
        G.hid = types.SimpleNamespace(device=lambda: FakeDev(World()))
        [st] = p.poll()
        self.assertEqual((st.level, st.online), (None, False))

    def test_unrelated_frames_are_skipped_before_the_answer(self):
        world = world_for(0xFF90, [0x55, 0x01] + [0] * 62, reply(64))
        p, out = self.poll(world)
        self.assertEqual(out[0].level, 64)
        self.assertIn("not a status reply", "\n".join(p.diagnostics()))

    def test_two_reads_fail_then_the_icon_has_no_level(self):
        p, out = self.poll(World({b"ff90:0001": []}))
        [st] = out
        self.assertEqual(st.level, None)
        self.assertIn("no reply", "\n".join(p.diagnostics()))


if __name__ == "__main__":
    unittest.main()
