"""Tests for providers/ninjutso_sprime.py. No hardware is needed."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import ninjutso_sprime as N  # noqa: E402


def reply(level=50, charging=0, full=0, online=1):
    r = [N.REPORT, N.COMMAND] + [0] * 30
    r[9] = level
    r[10] = charging
    r[11] = full
    r[12] = online
    return r


class FakeDevice:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.closed = 0

    def open_path(self, path):
        self.path = path

    def send_feature_report(self, data):
        self.sent.append(list(data))
        return len(data)

    def get_feature_report(self, report_id, length):
        if not self.replies:
            return []
        return self.replies.pop(0)

    def close(self):
        self.closed += 1


def entry(pid, page=None, usage=None):
    # a real Windows entry: hidapi does not return feature_report_byte_length (#528)
    return {"vendor_id": N.VID, "product_id": pid, "usage_page": page or N.PAGE,
            "usage": N.USAGE if usage is None else usage,
            "path": b"nj-%04x" % pid, "product_string": "Ninjutso"}


def fake_hid(dev):
    return types.SimpleNamespace(device=lambda: dev)


class ParseTests(unittest.TestCase):
    def test_level_zero_is_100(self):
        self.assertEqual(N.parse_reply(reply(0), False), (100, False, True))

    def test_sora_level_and_online(self):
        self.assertEqual(N.parse_reply(reply(42, 1), False), (42, True, True))
        self.assertEqual(N.parse_reply(reply(42, online=0), False), (42, False, False))

    def test_sprime_full(self):
        self.assertEqual(N.parse_reply(reply(88, 1, full=1), False, True), (100, False, True))

    def test_bad(self):
        self.assertIsNone(N.parse_reply([], False))
        self.assertIsNone(N.parse_reply(reply(101), False))


class PollTest(unittest.TestCase):
    def setUp(self):
        self._saved = (N.hid, N.hidlist)
        self.provider = N.NinjutsoSprimeProvider()

    def tearDown(self):
        N.hid, N.hidlist = self._saved

    def poll(self, infos, dev):
        N.hid = fake_hid(dev)
        N.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(infos))
        return self.provider.poll()

    def test_sora_receiver(self):
        dev = FakeDevice([reply(55)])
        res = self.poll([entry(N.SORA_RECEIVER)], dev)
        self.assertEqual([(r.key, r.name, r.level, r.via) for r in res],
                         [("ninjutso:sora_v2", "Ninjutso Sora V2", 55, "2.4 GHz receiver")])
        self.assertEqual(len(dev.sent[0]), 32)
        self.assertEqual(dev.sent[0][:2], [0x05, 0x15])

    def test_sprime_cable(self):
        dev = FakeDevice([reply(70, charging=1)])
        res = self.poll([entry(0xAC11)], dev)
        self.assertEqual([(r.key, r.level, r.charging, r.via) for r in res],
                         [("sprime:pm1", 70, True, "USB cable")])

    def test_the_8k_receivers_are_read(self):
        # #528: the Sora V2 8K receiver reports its vendor collection on interface 1
        # with no report length in the enumeration, and used to be skipped twice over
        for pid in (0xAE8C, 0xAE8A):
            dev = FakeDevice([reply(64, charging=1)])
            res = self.poll([entry(pid)], dev)
            self.assertEqual([(r.key, r.name, r.level, r.charging, r.via) for r in res],
                             [("ninjutso:sora_v2", "Ninjutso Sora V2", 64, True,
                               "2.4 GHz receiver")], hex(pid))

    def test_the_request_is_the_vendors_frame(self):
        dev = FakeDevice([reply(55)])
        self.poll([entry(0xAE8C)], dev)
        self.assertEqual(32, len(dev.sent[0]))
        self.assertEqual([0x05, 0x15, 0x00, 0x00, 0x01, 0x00, 0x00, 0x04],
                         dev.sent[0][:8])
        self.assertEqual(0, any(dev.sent[0][8:]))

    def test_a_wrong_collection_is_not_opened(self):
        dev = FakeDevice([])
        self.assertEqual(self.poll([entry(0xAE8C, page=0x0001, usage=0x0006)], dev), [])
        self.assertEqual(dev.sent, [])
        self.assertIn("no ffa0:0001 collection", "\n".join(self.provider.diagnostics()))

    def test_an_unknown_ninjutso_product_is_ignored(self):
        dev = FakeDevice([reply(50)])
        self.assertEqual(self.poll([entry(0x9999)], dev), [])
        self.assertEqual([], dev.sent)


if __name__ == "__main__":
    unittest.main()
