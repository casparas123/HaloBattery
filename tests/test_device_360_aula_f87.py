"""AULA F87 Pro on the Compx receiver (3554:FA09, #360)."""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import aula_f87 as A  # noqa: E402
from providers import pulsar as P  # noqa: E402


def reply(level=74, state=A.STATE_BATTERY, cmd=0x4A, bad_sum=False):
    frame = bytearray(20)
    frame[0:5] = bytes([0x13, cmd, 0x01, 0x00, 0x02])
    frame[5], frame[6] = level, state
    frame[19] = sum(frame[:19]) & 0xFF
    if bad_sum:
        frame[19] ^= 0xFF
    return bytes(frame)


class FrameTests(unittest.TestCase):
    def test_the_request_is_the_documented_frame(self):
        # Radiance-Philippines/aula-f87-control, docs/PROTOCOL.md: `13 4A 00 ... 5D`.
        q = A.make_request()
        self.assertEqual(20, len(q))
        self.assertEqual(bytes([0x13, 0x4A, 0x00, 0x00]), q[:4])
        self.assertEqual(0x5D, q[19])
        self.assertEqual(sum(q[:19]) & 0xFF, q[19])
        self.assertEqual(0xBA, sum(q) & 0xFF)   # two 0x5D halves
        self.assertTrue(all(b == 0 for b in q[4:19]))

    def test_the_captured_shape_reads_the_level(self):
        self.assertEqual((74, False, True), A.parse_reply(reply(74)))

    def test_the_cable_state_has_no_level(self):
        # while the cable is in the keyboard reports 100 regardless, so the level is dropped
        self.assertEqual((None, True, True), A.parse_reply(reply(100, A.STATE_CABLE)))

    def test_unknown_states_checksums_and_commands_are_refused(self):
        for frame in (reply(74, state=0x05), reply(0), reply(101),
                      reply(74, bad_sum=True), reply(74, cmd=0x4B), reply(74)[:19]):
            with self.subTest(frame=frame[:8].hex(" ")):
                self.assertIsNone(A.parse_reply(frame))

    def test_the_vendor_collection_is_preferred(self):
        infos = [dict(path=b"a", product_id=A.PID, usage_page=0xFF04, usage=0x0002, interface_number=1),
                 dict(path=b"b", product_id=A.PID, usage_page=0xFF02, usage=0x0002, interface_number=1),
                 dict(path=b"c", product_id=A.PID, usage_page=0x000C, usage=0x0001, interface_number=1)]
        self.assertEqual([b"b", b"a"], [d["path"] for d in A.candidates(infos)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ReadAulaTest(unittest.TestCase):
    """The fallback that pulsar.py runs when the Compx frame gets nothing."""

    class Fake:
        def __init__(self, replies):
            self.replies = list(replies)
            self.writes = []

        def open_path(self, path):
            pass

        def write(self, data):
            self.writes.append(bytes(data))
            return len(data)

        def read(self, length, timeout_ms):
            return self.replies.pop(0) if self.replies else []

        def close(self):
            pass

    def read(self, infos, dev, sizes=None):
        sizes = sizes or {}
        self.provider = P.PulsarProvider()
        saved, P.hid = P.hid, types.SimpleNamespace(device=lambda: dev)
        try:
            with mock.patch.object(P.aula_f87, "report_lengths",
                                   side_effect=lambda path: sizes.get(path, (20, 20))):
                return self.provider._read_aula(infos)
        finally:
            P.hid = saved

    def test_the_f87_frame_is_sent_and_read(self):
        dev = self.Fake([[], reply(74)])   # the drain reads once, then the answer
        reading = self.read([dict(path=b"x", product_id=A.PID, usage_page=0xFF02,
                                  usage=0x0002, interface_number=1)], dev)
        self.assertEqual((74, False, True), reading)
        self.assertEqual([A.make_request()], dev.writes)

    def test_a_silent_receiver_returns_none(self):
        self.assertIsNone(self.read([dict(path=b"x", product_id=A.PID, usage_page=0xFF02,
                                          usage=0x0002, interface_number=1)], self.Fake([])))

    def test_a_collection_too_small_for_the_frame_is_never_written_to(self):
        # 3554:fa09 also exposes ff04:0002; Windows refuses a 20-byte write on a
        # collection with a shorter output report, and the transport fails the poll.
        infos = [dict(path=b"small", product_id=A.PID, usage_page=0xFF04, usage=0x0002,
                      interface_number=1),
                 dict(path=b"good", product_id=A.PID, usage_page=0xFF02, usage=0x0002,
                      interface_number=1)]
        dev = self.Fake([[], reply(74)])
        reading = self.read(infos, dev, {b"small": (2, 2), b"good": (20, 20)})
        self.assertEqual((74, False, True), reading)
        self.assertEqual(1, len(dev.writes))     # only the usable collection was queried
        diag = " ".join(self.provider._diag)
        self.assertIn("ff04:0002 in=2 out=2", diag)
        self.assertIn("cannot carry the 20-byte frame; skipped", diag)

    def test_a_collection_with_unreadable_sizes_is_tried_last(self):
        infos = [dict(path=b"unknown", product_id=A.PID, usage_page=0xFF02, usage=0x0002,
                      interface_number=1)]
        dev = self.Fake([[], reply(74)])
        self.provider = P.PulsarProvider()
        saved, P.hid = P.hid, types.SimpleNamespace(device=lambda: dev)
        try:
            with mock.patch.object(P.aula_f87, "report_lengths", return_value=None):
                reading = self.provider._read_aula(infos)
        finally:
            P.hid = saved
        self.assertEqual((74, False, True), reading)
        self.assertIn("report sizes unreadable; tried last", " ".join(self.provider._diag))

    def test_a_usable_collection_is_read_before_one_with_unreadable_sizes(self):
        # ff02 sorts first; its sizes are unknown, ff04's are readable. The readable
        # one must be asked first, so the unknown one cannot answer with a stale level.
        infos = [dict(path=b"unknown", product_id=A.PID, usage_page=0xFF02, usage=0x0002,
                      interface_number=1),
                 dict(path=b"good", product_id=A.PID, usage_page=0xFF04, usage=0x0002,
                      interface_number=1)]

        class Routing:
            def __init__(self):
                self.path = None
                self.writes = []
                self.answers = {b"unknown": [reply(50)], b"good": [reply(74)]}

            def open_path(self, path):
                self.path = path

            def write(self, data):
                self.writes.append(self.path)
                return len(data)

            def read(self, length, timeout_ms):
                if length != A.REPORT_LEN:
                    return []
                queue = self.answers.get(self.path, [])
                return queue.pop(0) if queue else []

            def close(self):
                pass

        dev = Routing()
        self.provider = P.PulsarProvider()
        saved, P.hid = P.hid, types.SimpleNamespace(device=lambda: dev)
        try:
            with mock.patch.object(P.aula_f87, "report_lengths",
                                   side_effect=lambda path: {b"unknown": None,
                                                             b"good": (20, 20)}[path]):
                reading = self.provider._read_aula(infos)
        finally:
            P.hid = saved
        self.assertEqual((74, False, True), reading)
        self.assertEqual([b"good"], dev.writes)

    def test_a_failing_collection_does_not_stop_the_next_one(self):
        infos = [dict(path=b"bad", product_id=A.PID, usage_page=0xFF02, usage=0x0002,
                      interface_number=1),
                 dict(path=b"good", product_id=A.PID, usage_page=0xFF04, usage=0x0002,
                      interface_number=1)]

        class Routing:
            def __init__(self):
                self.path = None
                self.writes = []
                self.answers = {b"good": [reply(74)]}

            def open_path(self, path):
                self.path = path

            def write(self, data):
                if self.path == b"bad":
                    raise OSError("refused")
                self.writes.append((self.path, bytes(data)))
                return len(data)

            def read(self, length, timeout_ms):
                if length != A.REPORT_LEN:      # the drain asks with the Compx length
                    return []
                queue = self.answers.get(self.path, [])
                return queue.pop(0) if queue else []

            def close(self):
                pass

        dev = Routing()
        self.assertEqual((74, False, True), self.read(infos, dev))
        self.assertEqual([(b"good", A.make_request())], dev.writes)
        self.assertIn("aula query error: refused", " ".join(self.provider._diag))
