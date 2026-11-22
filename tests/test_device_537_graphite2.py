"""Tests for providers/graphite2.py (IO Graphite 2 Pro, #537), test 4. No hardware.

The reporter's descriptors show ff90 is input-only report 0x55 (status) and ff82
is an input/output pair on report 0x41 (command), with no feature report on
either. This build sends the family request on ff82 0x41 and reads both sides.

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


def frame(level=72, state=G.DISCHARGING, report_id=0x55, with_id=True):
    r = [0] * 64
    if with_id:
        r[:2] = [report_id, G.REPLY_ID]
        r[2], r[3] = level, state
    else:
        r[0], r[1] = G.REPLY_ID, level
        r[2] = state
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
    """One collection: per-path caps, command answers, status frames."""

    def __init__(self, world):
        self.world = world
        self.path = None
        self.queue = []

    def open_path(self, path):
        if path in self.world.refuse_open:
            raise OSError("open failed")
        self.path = path
        self.world.opened.append(path)
        if path in self.world.status_paths:
            if self.world.command_sent:
                self.queue += list(self.world.after_command.get(path, []))
            else:
                self.queue += list(self.world.idle_frames.get(path, []))

    def get_report_lengths(self):
        return dict(self.world.caps_by_path.get(self.path, self.world.caps))

    def error(self):
        return self.world.error_text

    def write(self, data):
        data = bytes(data)
        self.world.writes.append((self.path, data))
        if self.world.write_raises:
            raise OSError("write failed")
        self.world.command_sent = True
        self.queue += list(self.world.answers.get((self.path, data[0]), []))
        return len(data)

    def read(self, size, timeout_ms=0):
        self.world.timeouts.append(timeout_ms)
        return self.queue.pop(0) if self.queue else []

    def get_feature_report(self, report_id, size):     # must never be called
        self.world.features.append((self.path, report_id))
        return []

    def close(self):
        self.world.closed.append(self.path)


class World:
    def __init__(self, answers=None, idle_frames=None, after_command=None,
                 refuse_open=(), write_raises=False, error_text="Incorrect function"):
        self.caps = {"input": 64, "output": 64, "feature": 0}
        self.caps_by_path = {b"dongle-ff90": {"input": 64, "output": 0, "feature": 0},
                             b"dongle-ff82": {"input": 64, "output": 64, "feature": 0},
                             b"wired-ff90": {"input": 64, "output": 0, "feature": 0},
                             b"wired-ff82": {"input": 64, "output": 64, "feature": 0}}
        self.answers = answers or {}
        self.idle_frames = idle_frames or {}
        self.after_command = after_command or {}
        self.status_paths = {b"dongle-ff90", b"wired-ff90"}
        self.refuse_open = set(refuse_open)
        self.write_raises = write_raises
        self.error_text = error_text
        self.command_sent = False
        self.opened, self.closed, self.writes, self.features, self.timeouts = [], [], [], [], []


class ParseTests(unittest.TestCase):
    def test_both_sides_decode(self):
        self.assertEqual(G.parse_status(frame(72), 0x55), (72, False, False))
        self.assertEqual(G.parse_status(frame(55, G.CHARGING, report_id=0x41), 0x41), (55, True, True))
        self.assertEqual(G.parse_status(frame(100, G.FULL), 0x55), (100, False, False))
        self.assertEqual(G.parse_status(frame(31, with_id=False), 0x55), (31, False, False))

    def test_asleep_and_malformed(self):
        self.assertEqual(G.parse_status(frame(0, G.ASLEEP), 0x55), (None, False, False))
        self.assertIsNone(G.parse_status(None, 0x55))
        self.assertIsNone(G.parse_status([], 0x55))
        self.assertIsNone(G.parse_status(frame(50, 2, report_id=0x41), 0x55))
        self.assertIsNone(G.parse_status(frame(101), 0x55))
        self.assertIsNone(G.parse_status(frame(50, 7), 0x55))

    def test_the_request_is_sent_on_the_command_report(self):
        self.assertEqual(bytes([G.COMMAND_ID]) + G.REQUEST,
                         bytes([0x41, 0x65, 0x01]) + bytes(61))
        self.assertEqual(len(G.REQUEST), 63)

    def test_identity_is_the_exact_id_and_product(self):
        self.assertTrue(G.matches(dongle_entry(), G.SOURCES[1]))
        self.assertTrue(G.matches(wired_entry(), G.SOURCES[0]))
        self.assertFalse(G.matches(dongle_entry(product="Graphite 2 Pro"), G.SOURCES[1]))
        self.assertFalse(G.matches(dict(dongle_entry(), vendor_id=0x4C4B), G.SOURCES[1]))


class PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (G.hid, G.hidlist.enumerate)

    def tearDown(self):
        G.hid, G.hidlist.enumerate = self._saved

    def poll(self, world, infos):
        G.hid = types.SimpleNamespace(device=lambda: FakeDev(world))
        G.hidlist.enumerate = lambda vid=0: [d for d in infos if d["vendor_id"] == vid]
        p = G.GraphiteProvider()
        return p, p.poll()

    def test_the_command_reply_is_read_on_ff82(self):
        world = World(answers={(b"dongle-ff82", 0x41): [frame(72, report_id=0x41)]})
        p, out = self.poll(world, [dongle_entry(), dongle_entry(page=0xFF82)])
        [st] = out
        self.assertEqual((st.level, st.charging, st.online, st.kind), (72, False, True, "headset"))
        self.assertEqual(world.writes, [(b"dongle-ff82", bytes([0x41, 0x65, 0x01]) + bytes(61))])
        self.assertEqual(world.features, [])
        text = "\n".join(p.diagnostics())
        self.assertIn("caps in=64 out=64 feat=0", text)
        self.assertIn("caps in=64 out=0 feat=0", text)

    def test_the_status_side_carries_the_answer_when_the_command_side_does_not(self):
        world = World(after_command={b"dongle-ff90": [frame(64, G.CHARGING)]})
        _p, out = self.poll(world, [dongle_entry(), dongle_entry(page=0xFF82)])
        [st] = out
        self.assertEqual((st.level, st.charging, st.charging_known), (64, True, True))

    def test_an_idle_status_frame_is_used_without_sending_anything(self):
        world = World(idle_frames={b"dongle-ff90": [frame(88)]})
        _p, out = self.poll(world, [dongle_entry(), dongle_entry(page=0xFF82)])
        self.assertEqual(out[0].level, 88)
        self.assertEqual(world.writes, [])

    def test_the_wired_device_is_tried_before_the_dongle(self):
        world = World(answers={(b"wired-ff82", 0x41): [frame(81, report_id=0x41)]})
        infos = [dongle_entry(), dongle_entry(page=0xFF82), wired_entry(), wired_entry(page=0xFF82)]
        _p, out = self.poll(world, infos)
        self.assertEqual(out[0].level, 81)
        self.assertEqual(world.opened[0], b"wired-ff90")

    def test_the_dongle_is_used_when_the_wired_device_does_not_answer(self):
        world = World(answers={(b"dongle-ff82", 0x41): [frame(66, report_id=0x41)]})
        infos = [dongle_entry(), dongle_entry(page=0xFF82), wired_entry(), wired_entry(page=0xFF82)]
        _p, out = self.poll(world, infos)
        self.assertEqual(out[0].level, 66)

    def test_nothing_is_written_to_the_status_collection(self):
        world = World(after_command={b"dongle-ff90": [frame(50)]})
        self.poll(world, [dongle_entry(), dongle_entry(page=0xFF82)])
        self.assertEqual([p for p, _d in world.writes], [b"dongle-ff82"])

    def test_a_refused_open_is_reported_and_does_not_break_the_poll(self):
        world = World(refuse_open=[b"dongle-ff90", b"dongle-ff82"])
        p, out = self.poll(world, [dongle_entry(), dongle_entry(page=0xFF82)])
        [st] = out
        self.assertEqual(st.level, None)
        self.assertIn("open: open failed", "\n".join(p.diagnostics()))

    def test_an_unexpected_failure_does_not_escape_the_provider(self):
        class Exploding(FakeDev):
            def write(self, data):
                raise RuntimeError("boom")

        G.hid = types.SimpleNamespace(device=lambda: Exploding(World()))
        G.hidlist.enumerate = lambda vid=0: [d for d in [dongle_entry(), dongle_entry(page=0xFF82)]
                                            if d["vendor_id"] == vid]
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
        text = "\n".join(p.diagnostics())
        self.assertIn("ff90:0001 absent", text)
        self.assertIn("ff82:0001 absent", text)

    def test_no_known_source_no_icon(self):
        _p, out = self.poll(World(), [dict(dongle_entry(), product_string="Something Else")])
        self.assertEqual(out, [])

    def test_asleep_keeps_a_recent_level_greyed(self):
        p = G.GraphiteProvider()
        p._last = (70, False, time.time() - 10, False)
        world = World(idle_frames={b"dongle-ff90": [frame(0, G.ASLEEP)]})
        G.hid = types.SimpleNamespace(device=lambda: FakeDev(world))
        G.hidlist.enumerate = lambda vid=0: [d for d in [dongle_entry(), dongle_entry(page=0xFF82)]
                                            if d["vendor_id"] == vid]
        [st] = p.poll()
        self.assertEqual((st.level, st.online), (70, False))
        self.assertFalse(st.charging_known)


if __name__ == "__main__":
    unittest.main()
