"""Tests for providers/rapoo.py. No hardware is needed.

The frames are the ones a real VT9 Pro receiver pushed in the reporter's
USBPcap capture in issue #109: `bb b0 42 20 03 01 64` (100 %, discharging)
about every 3 seconds, with no software running. The collection shape is the
reporter's diagnostics dump (24ae:185a: ff00:0002 and ff00:000e on
interface 1, the ff00:0002 entry repeated, plus non-vendor collections).

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import rapoo as R  # noqa: E402


CAPTURED = [0xBB, 0xB0, 0x42, 0x20, 0x03, 0x01, 0x64]        # 100 %, discharging


class FakeCollection:
    """frames: what the receiver pushes, in order; an entry of None reads as silence."""

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.opened = 0

    def read(self):
        if self.frames:
            f = self.frames.pop(0)
            return f if f is not None else []
        return []


def fake_device_class(cols, order_log):
    class FakeDevice:
        def open_path(self, path):
            self.c = cols[path]
            self.c.opened += 1
            order_log.append(path)

        def read(self, n, timeout):
            return self.c.read()

        def write(self, *a):
            raise AssertionError("the Rapoo provider must never write")

        def send_feature_report(self, *a):
            raise AssertionError("the Rapoo provider must never write")

        def close(self):
            pass
    return FakeDevice


def receiver_entries(pid=R.VT9_PRO_RECEIVER, prefix=b"recv"):
    # the collections of the reporter's VT9 Pro receiver, in dump order
    shape = [(0, 0x0001, 0x02), (1, 0xFF00, 0x02), (1, 0x0001, 0x80), (1, 0x000C, 0x01),
             (1, 0x0001, 0x06), (1, 0xFF00, 0x0E), (1, 0xFF00, 0x02)]
    return [{"product_id": pid, "interface_number": i, "usage_page": p, "usage": u,
             "path": prefix + b"-%d-%04x-%d" % (i, p, n),
             "product_string": "Rapoo Gaming Device"}
            for n, (i, p, u) in enumerate(shape)]


class PollTest(unittest.TestCase):
    def setUp(self):
        self._saved = (R.hid, R.hidlist, R.time)
        self.now = [1000.0]
        R.time = types.SimpleNamespace(time=lambda: self.now[0], sleep=lambda s: None)
        self.provider = R.RapooProvider()
        self.order = []

    def tearDown(self):
        R.hid, R.hidlist, R.time = self._saved

    def poll(self, entries, cols):
        R.hid = types.SimpleNamespace(device=fake_device_class(cols, self.order))
        R.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(entries))
        return self.provider.poll()

    def cols(self, entries, frames=(CAPTURED,)):
        # the first vendor collection answers; the rest are silent
        first = next(e for e in entries
                     if (e["usage_page"], e["usage"]) == (0xFF00, 0x0002))
        return {e["path"]: FakeCollection(frames if e["path"] == first["path"] else ())
                for e in entries}

    # ---------------------------------------------------------------- parsing

    def test_the_captured_frame_reads_100_discharging(self):
        self.assertEqual(R.parse_report(CAPTURED), (100, False))

    def test_a_charging_frame_reads_charging(self):
        frame = [0xBB, 0xB0, 0x42, 0x20, 0x03, 0x02, 0x50]
        self.assertEqual(R.parse_report(frame), (80, True))

    def test_a_transition_frame_is_not_a_reading(self):
        frame = [0xBB, 0xB0, 0x42, 0x20, 0x03, 0x00, 0x40]
        self.assertIsNone(R.parse_report(frame))

    def test_a_10_byte_report_is_not_a_reading(self):
        frame = [0xBC, 0xB0, 0x01, 0x02, 0x03, 0x01, 0x64, 0, 0, 0]
        self.assertIsNone(R.parse_report(frame))

    def test_wrong_flags_are_refused(self):
        frame = [0xBB, 0xC0, 0x42, 0x20, 0x03, 0x01, 0x64]
        self.assertIsNone(R.parse_report(frame))

    def test_a_level_above_100_is_refused(self):
        frame = [0xBB, 0xB0, 0x42, 0x20, 0x03, 0x01, 0x65]
        self.assertIsNone(R.parse_report(frame))

    # ---------------------------------------------------------------- polling

    def test_the_captured_mouse_is_read_listen_only(self):
        entries = receiver_entries()
        out = self.poll(entries, self.cols(entries))
        self.assertEqual(len(out), 1)
        s = out[0]
        self.assertEqual((s.key, s.name, s.level, s.charging, s.online, s.source, s.kind),
                         ("rapoo:185a", "Rapoo VT9 Pro", 100, False, True, "rapoo", "mouse"))

    def test_junk_before_the_frame_is_skipped(self):
        junk = [0xBC, 0xB0, 0x01, 0x02, 0x03, 0x01, 0x64, 0, 0, 0]
        transition = [0xBB, 0xB0, 0x42, 0x20, 0x03, 0x00, 0x40]
        entries = receiver_entries()
        out = self.poll(entries, self.cols(entries, frames=(junk, transition, CAPTURED)))
        self.assertEqual(out[0].level, 100)

    def test_the_second_vendor_collection_answers_when_the_first_is_silent(self):
        entries = receiver_entries()
        second = next(e for e in entries if (e["usage_page"], e["usage"]) == (0xFF00, 0x000E))
        cols = {e["path"]: FakeCollection((CAPTURED,) if e["path"] == second["path"] else ())
                for e in entries}
        out = self.poll(entries, cols)
        self.assertEqual(out[0].level, 100)
        self.assertEqual(self.provider._chosen, second["path"])

    def test_the_answered_collection_is_tried_first_next_poll(self):
        entries = receiver_entries()
        second = next(e for e in entries if (e["usage_page"], e["usage"]) == (0xFF00, 0x000E))
        cols = {e["path"]: FakeCollection((CAPTURED,) if e["path"] == second["path"] else ())
                for e in entries}
        self.poll(entries, cols)
        self.order.clear()
        self.poll(entries, cols)
        self.assertEqual(self.order[0], second["path"])

    def test_silence_keeps_the_last_level_greyed_out(self):
        entries = receiver_entries()
        self.poll(entries, self.cols(entries))
        self.now[0] += 60
        out = self.poll(entries, {e["path"]: FakeCollection() for e in entries})
        self.assertEqual((out[0].level, out[0].online), (100, False))

    def test_silence_after_the_keep_window_drops_the_device(self):
        entries = receiver_entries()
        self.poll(entries, self.cols(entries))
        self.now[0] += R.ASLEEP_KEEP + 1
        out = self.poll(entries, {e["path"]: FakeCollection() for e in entries})
        self.assertEqual(out, [])

    def test_no_reading_yet_gives_nothing(self):
        entries = receiver_entries()
        out = self.poll(entries, {e["path"]: FakeCollection() for e in entries})
        self.assertEqual(out, [])

    def test_no_receiver_gives_nothing(self):
        out = self.poll([], {})
        self.assertEqual(out, [])

    # ---------------------------------------------------------------- order

    def test_candidates_put_the_vendor_collections_first_and_dedupe(self):
        cand = R.candidates(receiver_entries())
        usages = [(d["usage_page"], d["usage"]) for d in cand]
        self.assertEqual(usages[:2], [(0xFF00, 0x0002), (0xFF00, 0x000E)])
        self.assertEqual(len(set(usages)), len(usages))          # the repeated ff00:0002 is one
        self.assertNotIn((0x0001, 0x0002), usages)               # the mouse is never opened


if __name__ == "__main__":
    unittest.main()
