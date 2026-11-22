"""Tests for providers/stellar.py (ROCCAT Syn Max Air / Stealth 700 Gen 2). No hardware.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import stellar as S  # noqa: E402


def entry(vid, pid, product, path, page=0xFF00, usage=0x0001):
    return {"vendor_id": vid, "product_id": pid, "interface_number": 0,
            "usage_page": page, "usage": usage, "path": path, "product_string": product}


def roccat(page=0xFF00):
    return entry(0x1E7D, 0x3A3F, "ROCCAT Syn Max Air", b"roccat-ff00", page)


def turtle(page=0xFF00):
    return entry(0x10F5, 0x218C, "Turtle Beach Stealth 700 G2", b"turtle-ff00", page)


def reply(level=5, mv=3920, charging=0):
    return [0x8A, 0x83, 0x06, 0x02, level, 0x00, mv & 0xFF, mv >> 8,
            mv & 0xFF, mv >> 8, charging, 0] + [0] * 53


class FakeDev:
    def __init__(self, world):
        self.world = world
        self.path = None
        self.queue = []

    def open_path(self, path):
        if path in self.world.refuse_open:
            raise OSError("open failed")
        self.path = path
        self.world.opened.append(path)
        self.queue = list(self.world.drain.get(path, []))

    def get_report_lengths(self):
        return dict(self.world.caps.get(self.path, {"input": 64, "output": 64, "feature": 0}))

    def write(self, data):
        data = bytes(data)
        self.world.writes.append((self.path, data))
        if self.world.write_raises:
            raise OSError("write failed")
        if self.world.write_refused:
            return -1
        self.queue += [list(f) for f in self.world.frames.get(self.path, [])]
        return 64

    def read(self, size, timeout_ms=0):
        self.world.timeouts.append(timeout_ms)
        return self.queue.pop(0) if self.queue else []

    def close(self):
        self.world.closed.append(self.path)


class World:
    def __init__(self, frames=None, drain=None, caps=None, refuse_open=(),
                 write_raises=False, write_refused=False):
        self.frames = frames or {}
        self.drain = drain or {}
        self.caps = caps or {}
        self.refuse_open = set(refuse_open)
        self.write_raises = write_raises
        self.write_refused = write_refused
        self.opened, self.closed, self.writes, self.timeouts = [], [], [], []


class DecodeTests(unittest.TestCase):
    def test_curve_points_and_ends(self):
        self.assertEqual(S.percent_from_mv(4200), 100)
        self.assertEqual(S.percent_from_mv(4080), 100)
        self.assertEqual(S.percent_from_mv(3920), 78)
        self.assertEqual(S.percent_from_mv(3960), 86)
        self.assertEqual(S.percent_from_mv(3700), 20)
        self.assertEqual(S.percent_from_mv(3400), 0)

    def test_on_battery_reply(self):
        self.assertEqual(S.decode(reply(), S.SOURCES[0]), (78, False, True, ""))

    def test_dock_and_full(self):
        self.assertEqual(S.decode(reply(level=5, mv=3940, charging=1), S.SOURCES[0]),
                         (100, True, True, "about 100% (coarse, charging)"))
        self.assertEqual(S.decode(reply(level=6, mv=4200, charging=1), S.SOURCES[0]),
                         (100, True, True, ""))

    def test_a_charging_reply_with_no_usable_level_stays_unknown(self):
        self.assertEqual(S.decode(reply(level=6, mv=3900, charging=1), S.SOURCES[0]),
                         (None, True, True, "charging (level unavailable)"))

    def test_the_gen_2_uses_the_coarse_level_not_the_syn_max_air_curve(self):
        self.assertEqual(S.decode(reply(level=3, mv=3920), S.SOURCES[1]),
                         (60, False, True, "about 60% (coarse)"))

    def test_other_frames_are_ignored(self):
        self.assertIsNone(S.decode([0xE0, 1, 2] + [0] * 61, S.SOURCES[0]))
        self.assertIsNone(S.decode(reply()[:10], S.SOURCES[0]))
        self.assertIsNone(S.decode([0x8A, 0x83, 0x06, 0x03] + [0] * 60, S.SOURCES[0]))
        self.assertIsNone(S.decode(None, S.SOURCES[0]))

    def test_identity_needs_the_model_name_and_vendor_id(self):
        self.assertTrue(S.matches(roccat(), S.SOURCES[0]))
        self.assertTrue(S.matches(turtle(), S.SOURCES[1]))
        self.assertFalse(S.matches(dict(roccat(), product_string="ROCCAT Kone"), S.SOURCES[0]))
        self.assertFalse(S.matches(dict(turtle(), vendor_id=0x1234), S.SOURCES[1]))


class PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (S.hid, S.hidlist.enumerate)

    def tearDown(self):
        S.hid, S.hidlist.enumerate = self._saved

    def poll(self, world, infos):
        G = types.SimpleNamespace(device=lambda: FakeDev(world))
        S.hid = G
        S.hidlist.enumerate = lambda vid=0: [d for d in infos if d["vendor_id"] == vid]
        p = S.StellarProvider()
        return p, p.poll()

    def test_the_request_is_the_unnumbered_stellar_frame(self):
        world = World(frames={b"roccat-ff00": [reply()]})
        _p, out = self.poll(world, [roccat()])
        [st] = out
        self.assertEqual((st.key, st.level, st.charging, st.online, st.kind),
                         ("roccat:3a3f", 78, False, True, "headset"))
        self.assertEqual(world.writes, [(b"roccat-ff00", bytes([0x00, 0x8A, 0x07, 0x06, 0x01]) + bytes(59))])

    def test_unsolicited_events_are_skipped_until_the_tag(self):
        event = [0xE0, 0x01] + [0] * 62
        world = World(frames={b"roccat-ff00": [event, reply()]})
        p, out = self.poll(world, [roccat()])
        self.assertEqual(out[0].level, 78)
        self.assertIn("frame 1", "\n".join(p.diagnostics()))
        self.assertIn("frame 2", "\n".join(p.diagnostics()))

    def test_a_receiver_that_stays_silent_shows_an_offline_icon_and_keeps_the_last_level(self):
        world = World()
        p = S.StellarProvider()
        p._last["roccat:3a3f"] = (64, False, time.time() - 10, True, "")
        S.hid = types.SimpleNamespace(device=lambda: FakeDev(world))
        S.hidlist.enumerate = lambda vid=0: [d for d in [roccat()] if d["vendor_id"] == vid]
        [st] = p.poll()
        self.assertEqual((st.level, st.online, st.charging_known), (64, False, True))

    def test_no_collection_no_icon(self):
        _p, out = self.poll(World(), [roccat(page=0x000C)])
        self.assertEqual(out, [])

    def test_both_models_report_their_own_icon(self):
        world = World(frames={b"roccat-ff00": [reply()],
                              b"turtle-ff00": [reply(level=5, mv=3920)]})
        _p, out = self.poll(world, [roccat(), turtle()])
        self.assertEqual([(s.key, s.level) for s in out],
                         [("roccat:3a3f", 78), ("turtlebeach:stealth700g2", 100)])

    def test_the_gen_2_shows_a_coarse_reading(self):
        world = World(frames={b"turtle-ff00": [reply(level=2, mv=3800)]})
        _p, out = self.poll(world, [turtle()])
        [st] = out
        self.assertEqual((st.level, st.approx), (40, "about 40% (coarse)"))

    def test_a_wrong_report_length_is_not_queried(self):
        world = World(caps={b"roccat-ff00": {"input": 32, "output": 32, "feature": 0}},
                      frames={b"roccat-ff00": [reply()]})
        p, out = self.poll(world, [roccat()])
        self.assertEqual(out[0].level, None)
        self.assertEqual(world.writes, [])
        self.assertIn("incompatible report lengths", "\n".join(p.diagnostics()))

    def test_a_refused_write_is_reported(self):
        world = World(write_refused=True)
        p, out = self.poll(world, [roccat()])
        self.assertEqual(out[0].level, None)
        self.assertIn("write refused", "\n".join(p.diagnostics()))

    def test_an_unexpected_error_does_not_escape(self):
        class Boom(FakeDev):
            def write(self, data):
                raise RuntimeError("boom")

        S.hid = types.SimpleNamespace(device=lambda: Boom(World()))
        S.hidlist.enumerate = lambda vid=0: [d for d in [roccat()] if d["vendor_id"] == vid]
        p = S.StellarProvider()
        out = p.poll()                      # must not raise
        self.assertEqual(out[0].level, None)
        self.assertIn("query error: boom", "\n".join(p.diagnostics()))


if __name__ == "__main__":
    unittest.main()
