"""Tests for providers/graphite2.py (IO Graphite 2 Pro, #537), test 2. No hardware.

The dongle only exposes its vendor collections with the headset linked at
enumeration, and Windows refuses the output report in the first test build, so
this build logs the collection's report lengths and tries output 0x55, a feature
read of 0x55 and an unnumbered 0x00 report before giving up.

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


def reply(level=72, state=G.DISCHARGING, prefix=(0x55, 0x65)):
    r = [0] * 64
    r[:len(prefix)] = prefix
    r[len(prefix)] = level
    r[len(prefix) + 1] = state
    return r


def dongle_entry(page=0xFF90, iface=0, product="Graphite 2 Pro Wireless"):
    return {"vendor_id": G.VID, "product_id": G.PID, "interface_number": iface,
            "usage_page": page, "usage": 0x0001, "path": f"dongle-{page:04x}".encode(),
            "product_string": product}


def wired_entry(page=0xFF90, iface=0):
    return {"vendor_id": G.WIRED_VID, "product_id": G.WIRED_PID, "interface_number": iface,
            "usage_page": page, "usage": 0x0001, "path": f"wired-{page:04x}".encode(),
            "product_string": "Graphite 2 Pro (Wired Mode)"}


class FakeDev:
    """One collection: caps, per-report-id write behaviour, feature read, queued answers."""

    def __init__(self, world):
        self.world = world
        self.path = None
        self.queue = []

    def open_path(self, path):
        if path in self.world.refuse_open:
            raise OSError("open failed")
        self.path = path
        self.world.opened.append(path)

    def get_report_lengths(self):
        return dict(self.world.caps)

    def error(self):
        return self.world.error_text

    def write(self, data):
        data = bytes(data)
        self.world.writes.append((self.path, data))
        behaviour = self.world.write_behaviour.get(data[0], "ok")
        if behaviour == "raise":
            raise OSError("write failed")
        if behaviour == "refuse":
            return -1
        self.queue += self.world.answers.get((self.path, data[0]), [])
        return len(data)

    def read(self, size, timeout_ms=0):
        self.world.timeouts.append(timeout_ms)
        return self.queue.pop(0) if self.queue else []

    def get_feature_report(self, report_id, size):
        self.world.features.append((self.path, report_id))
        if self.world.feature_raises:
            raise OSError("feature failed")
        return self.world.feature_answers.get((self.path, report_id), [])

    def close(self):
        self.world.closed.append(self.path)


class World:
    def __init__(self, caps=None, answers=None, feature_answers=None, write_behaviour=None,
                 refuse_open=(), feature_raises=False, error_text="Incorrect function"):
        self.caps = caps if caps is not None else {"input": 64, "output": 64, "feature": 64}
        self.answers = answers or {}
        self.feature_answers = feature_answers or {}
        self.write_behaviour = write_behaviour or {}
        self.refuse_open = set(refuse_open)
        self.feature_raises = feature_raises
        self.error_text = error_text
        self.opened, self.closed, self.writes, self.features, self.timeouts = [], [], [], [], []


class ParseTests(unittest.TestCase):
    def test_the_family_answer_forms(self):
        self.assertEqual(G.parse_status(reply(72)), (72, False, False))
        self.assertEqual(G.parse_status(reply(55, G.CHARGING)), (55, True, True))
        self.assertEqual(G.parse_status(reply(100, G.FULL)), (100, False, False))
        self.assertEqual(G.parse_status(reply(28, prefix=(0x00, 0x55, 0x65))), (28, False, False))
        self.assertEqual(G.parse_status(reply(31, prefix=(0x65,))), (31, False, False))

    def test_asleep_and_malformed(self):
        self.assertEqual(G.parse_status(reply(0, G.ASLEEP)), (None, False, False))
        self.assertIsNone(G.parse_status(None))
        self.assertIsNone(G.parse_status([]))
        self.assertIsNone(G.parse_status([0x55, 0x66, 50, 2] + [0] * 60))
        self.assertIsNone(G.parse_status([0x55, 0x65, 50]))            # no state byte
        self.assertIsNone(G.parse_status(reply(101)))
        self.assertIsNone(G.parse_status(reply(50, 7)))

    def test_the_request_is_the_family_frame(self):
        self.assertEqual(bytes([G.REPORT]) + G.REQUEST,
                         bytes([0x55, 0x65, 0x01]) + bytes(61))
        self.assertEqual(len(G.REQUEST), 63)

    def test_identity_is_the_exact_id_and_product(self):
        self.assertTrue(G.matches(dongle_entry(), G.SOURCES[1]))
        self.assertTrue(G.matches(wired_entry(), G.SOURCES[0]))
        self.assertFalse(G.matches(dongle_entry(product="Graphite 2 Pro"), G.SOURCES[1]))
        self.assertFalse(G.matches(dict(dongle_entry(), vendor_id=0x4C4B), G.SOURCES[1]))
        self.assertFalse(G.matches(dict(wired_entry(), product_id=0x037A), G.SOURCES[0]))


class PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (G.hid, G.hidlist.enumerate)

    def tearDown(self):
        G.hid, G.hidlist.enumerate = self._saved

    def poll(self, world, infos):
        fake = FakeDev(world)
        G.hid = types.SimpleNamespace(device=lambda: fake)
        G.hidlist.enumerate = lambda vid=0: [d for d in infos if d["vendor_id"] == vid]
        p = G.GraphiteProvider()
        return p, p.poll()

    def test_output_report_0x55_is_read_first(self):
        world = World(answers={(b"dongle-ff90", 0x55): [reply(72)]})
        p, out = self.poll(world, [dongle_entry()])
        [st] = out
        self.assertEqual((st.level, st.charging, st.online, st.kind), (72, False, True, "headset"))
        self.assertEqual(world.opened, [b"dongle-ff90"])
        self.assertIn("caps in=64 out=64 feat=64", "\n".join(p.diagnostics()))

    def test_a_refused_output_report_falls_back_to_a_feature_read(self):
        world = World(write_behaviour={0x55: "refuse"},
                      feature_answers={(b"dongle-ff90", 0x55): reply(64, G.CHARGING)})
        p, out = self.poll(world, [dongle_entry()])
        [st] = out
        self.assertEqual((st.level, st.charging, st.charging_known), (64, True, True))
        text = "\n".join(p.diagnostics())
        self.assertIn("write 0x55 refused (-1) error='Incorrect function'", text)
        self.assertIn("feature 0x55", text)

    def test_unnumbered_report_is_the_last_resort(self):
        world = World(write_behaviour={0x55: "refuse"}, feature_raises=True,
                      answers={(b"dongle-ff90", 0x00): [reply(41, prefix=(0x65,))]})
        _p, out = self.poll(world, [dongle_entry()])
        self.assertEqual(out[0].level, 41)
        self.assertEqual([d[0] for _path, d in world.writes], [0x55, 0x00])

    def test_the_wired_device_is_tried_before_the_dongle(self):
        world = World(answers={(b"wired-ff90", 0x55): [reply(88)]})
        _p, out = self.poll(world, [dongle_entry(), wired_entry()])
        self.assertEqual(out[0].level, 88)
        self.assertEqual(world.opened, [b"wired-ff90"])

    def test_the_dongle_is_used_when_the_wired_device_does_not_answer(self):
        world = World(answers={(b"dongle-ff90", 0x55): [reply(66)]})
        _p, out = self.poll(world, [dongle_entry(), wired_entry()])
        self.assertEqual(out[0].level, 66)
        self.assertEqual(world.opened, [b"wired-ff90", b"dongle-ff90"])

    def test_a_refused_open_is_reported_and_does_not_break_the_poll(self):
        world = World(refuse_open=[b"dongle-ff90"])
        p, out = self.poll(world, [dongle_entry()])
        [st] = out
        self.assertEqual(st.level, None)
        self.assertIn("open: open failed", "\n".join(p.diagnostics()))

    def test_an_unexpected_failure_does_not_escape_the_provider(self):
        class Exploding(FakeDev):
            def write(self, data):
                raise RuntimeError("boom")

        G.hid = types.SimpleNamespace(device=lambda: Exploding(World()))
        G.hidlist.enumerate = lambda vid=0: [d for d in [dongle_entry()] if d["vendor_id"] == vid]
        p = G.GraphiteProvider()
        out = p.poll()                                   # must not raise
        self.assertEqual(out[0].level, None)
        self.assertIn("unexpected: boom", "\n".join(p.diagnostics()))

    def test_absent_collections_leave_the_icon_without_a_level(self):
        world = World()
        p, out = self.poll(world, [dongle_entry(page=0x000C)])
        [st] = out
        self.assertEqual((st.level, st.online), (None, False))
        self.assertEqual(world.writes, [])
        self.assertIn("ff90:0001 absent", "\n".join(p.diagnostics()))

    def test_no_known_source_no_icon(self):
        _p, out = self.poll(World(), [dict(dongle_entry(), product_string="Something Else")])
        self.assertEqual(out, [])

    def test_asleep_keeps_a_recent_level_greyed(self):
        p = G.GraphiteProvider()
        p._last = (70, False, time.time() - 10, False)
        world = World(answers={(b"dongle-ff90", 0x55): [reply(0, G.ASLEEP)]})
        G.hid = types.SimpleNamespace(device=lambda: FakeDev(world))
        G.hidlist.enumerate = lambda vid=0: [d for d in [dongle_entry()] if d["vendor_id"] == vid]
        [st] = p.poll()
        self.assertEqual((st.level, st.online), (70, False))
        self.assertFalse(st.charging_known)


if __name__ == "__main__":
    unittest.main()
