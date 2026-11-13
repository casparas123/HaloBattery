"""Tests for providers/am_infinity.py. No hardware is needed.

The fake receiver has the collections of the AM Infinity unit from the
diagnostics report in issue #72 (3151:5007; 'ffff:0001' on interface 1 and
'ffff:0002' on interface 2). Two battery layouts live behind that shared id:

* the AM Infinity's own report 0xF7 - `F7 00 64 ...` with the main charge in
  byte 2 and the dock's spare cell in byte 10, from TsFreddie/powermiao8k;
* the AJAZZ AJ159 APEX's report 0x05 - `05 00 00 64 ...`, which stays as a
  fallback for the sibling firmware.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import am_infinity as A  # noqa: E402

# the AM Infinity's 0xF7 report: main 100 %, spare 55 % at byte 10
AM100_SPARE55 = [0xF7, 0x00, 0x64] + [0x00] * 7 + [0x37]
AM100 = [0xF7, 0x00, 0x64]                       # short: no spare byte
AM_STRIPPED = [0x00, 0x64] + [0x00] * 7 + [0x37]  # hidapi without the report id
AM_JUNK = [0xF7, 0xAD, 0x63]                      # non-zero padding: refused
AM_ZERO = [0xF7, 0x00, 0x00]                      # link not up yet
AM_OVER = [0xF7, 0x00, 0xFA]                      # above 100: the driver's unknown

# the AJAZZ sibling's 0x05 report, as hidapi returns it on Windows
AJAZZ100 = [0x05, 0x00, 0x00, 0x64, 0x01, 0x01, 0x01, 0x02]
AJAZZ_JUNK = [0x05, 0xAD, 0x04, 0x63, 0x01, 0x01, 0x01, 0x02]


class FakeCollection:
    """replies: {report_id: [frame, ...]}, each padded to 65; empty = zeros."""

    def __init__(self, replies=None):
        self.queues = {rid: list(frames) for rid, frames in (replies or {}).items()}
        self.sent = []
        self.reads = []
        self.opened = 0

    def reply(self, report_id):
        self.reads.append(report_id)
        queue = self.queues.get(report_id) or []
        frame = queue.pop(0) if queue else None
        if frame is None:
            return [0x00] * 65
        return list(frame) + [0x00] * (65 - len(frame))


def fake_device_class(cols):
    class FakeDevice:
        def open_path(self, path):
            self.c = cols[path]
            self.c.opened += 1

        def send_feature_report(self, data):
            self.c.sent.append(list(data))
            return len(data)

        def get_feature_report(self, report_id, n):
            return self.c.reply(report_id)

        def close(self):
            pass
    return FakeDevice


def infinity_entries(pid=A.PID):
    # the six collections of the AM Infinity receiver in the #72 diagnostics
    shape = [(0, 0x0001, 0x02), (1, 0x000C, 0x01), (1, 0x0001, 0x80),
             (1, 0xFFFF, 0x01), (1, 0x0001, 0x06), (2, 0xFFFF, 0x02)]
    return [{"product_id": pid, "interface_number": i, "usage_page": p, "usage": u,
             "path": b"am-%d-%04x-%02x" % (i, p, u), "product_string": "AM INFINITY 8K MOUSE"}
            for i, p, u in shape]


class PollTest(unittest.TestCase):
    def setUp(self):
        self._saved = (A.hid, A.hidlist, A.time)
        A.time = types.SimpleNamespace(time=time.time, sleep=lambda s: None)
        self.provider = A.AmInfinityProvider()

    def tearDown(self):
        A.hid, A.hidlist, A.time = self._saved

    def poll(self, entries, cols):
        A.hid = types.SimpleNamespace(device=fake_device_class(cols))
        A.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(entries))
        return self.provider.poll()

    def cols(self, entries, replies=None):
        return {e["path"]: FakeCollection(replies or {}) for e in entries}

    def test_the_am_report_is_read_first(self):
        entries = infinity_entries()
        cols = self.cols(entries, {0xF7: [AM100]})
        res = self.poll(entries, cols)
        self.assertEqual([(r.key, r.name, r.level, r.charging, r.online, r.kind) for r in res],
                         [("am_infinity", "AM Infinity 8K Mouse", 100, False, True, "mouse")])
        self.assertFalse(res[0].charging_known)
        self.assertEqual(cols[entries[5]["path"]].reads, [0xF7])

    def test_the_dock_spare_cell_is_logged(self):
        entries = infinity_entries()
        cols = self.cols(entries, {0xF7: [AM100_SPARE55]})
        res = self.poll(entries, cols)
        self.assertEqual([r.level for r in res], [100])
        self.assertIn("spare cell on the dock: 55%", "\n".join(self.provider.diagnostics()))

    def test_the_ajazz_report_is_the_fallback(self):
        entries = infinity_entries()
        cols = self.cols(entries, {0x05: [AJAZZ100]})
        res = self.poll(entries, cols)
        self.assertEqual([r.level for r in res], [100])
        self.assertEqual(cols[entries[5]["path"]].reads, [0xF7, 0x05])
        self.assertIn("AJAZZ sibling", "\n".join(self.provider.diagnostics()))

    def test_without_the_report_id_the_main_charge_is_byte_1(self):
        entries = infinity_entries()
        res = self.poll(entries, self.cols(entries, {0xF7: [AM_STRIPPED]}))
        self.assertEqual([(r.level, r.online) for r in res], [(100, True)])

    def test_junk_padding_is_refused(self):
        entries = infinity_entries()
        res = self.poll(entries, self.cols(entries, {0xF7: [AM_JUNK, AM_JUNK],
                                                     0x05: [AJAZZ_JUNK, AJAZZ_JUNK]}))
        self.assertEqual(res, [])

    def test_zero_or_over_100_is_unknown_not_a_level(self):
        entries = infinity_entries()
        for frame in (AM_ZERO, AM_OVER):
            res = self.poll(entries, self.cols(entries, {0xF7: [frame, frame]}))
            self.assertEqual(res, [], frame)

    def test_only_ffff_0002_gets_the_poll(self):
        entries = infinity_entries()
        cols = self.cols(entries, {0xF7: [AM100]})
        self.poll(entries, cols)
        touched = [n for n, e in enumerate(entries) if cols[e["path"]].opened]
        self.assertEqual(touched, [5])                 # not the ffff:0001 on interface 1
        self.assertEqual(cols[entries[5]["path"]].sent,
                         [[0x00, 0xF7] + [0x00] * 63])  # one 65-byte zero-payload poll

    def test_second_buffer_length_is_tried(self):
        entries = infinity_entries()
        cols = self.cols(entries, {0xF7: [AM_JUNK, AM100]})   # 65 gets junk, 67 answers
        res = self.poll(entries, cols)
        self.assertEqual([r.level for r in res], [100])
        self.assertEqual([len(p) for p in cols[entries[5]["path"]].sent], [65, 67])

    def test_missing_collection_sends_nothing(self):
        entries = [e for e in infinity_entries() if e["usage_page"] != 0xFFFF]
        cols = self.cols(entries)
        self.assertEqual(self.poll(entries, cols), [])
        self.assertTrue(all(not c.opened for c in cols.values()))

    def test_other_pids_are_ignored(self):
        entries = [dict(e, product_id=0x5008) for e in infinity_entries()]
        cols = self.cols(entries)
        self.assertEqual(self.poll(entries, cols), [])
        self.assertTrue(all(not c.opened for c in cols.values()))

    def test_silent_receiver_keeps_the_last_value_greyed(self):
        entries = infinity_entries()
        cols = self.cols(entries, {0xF7: [AM100]})
        self.poll(entries, cols)
        for c in cols.values():
            c.queues = {}
            c.sent.clear()
        res = self.poll(entries, cols)
        self.assertEqual([(r.level, r.online) for r in res], [(100, False)])
        self.assertFalse(res[0].charging_known)
        self.provider._last = (100, time.time() - A.ASLEEP_KEEP)
        self.assertEqual(self.poll(entries, cols), [])


if __name__ == "__main__":
    unittest.main()
