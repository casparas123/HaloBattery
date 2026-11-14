"""MCHOSE G98 V3 QHW keyboard (3837:1015): frame and guards. No hardware."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import mchose_qhw as M  # noqa: E402


def reply(sequence, level=87, charge=1):
    return bytes([M.REPORT_ID, 1, 3, 2, 4, 9, 0, sequence, charge, level, 0x00, 0x00])


class FakeDev:
    def __init__(self, frames=None, write_result=None):
        self.frames = list(frames or [])
        self.write_result = write_result
        self.writes = []
        self.closed = False

    def open_path(self, path):
        self.path = path

    def write(self, data):
        self.writes.append(list(data))
        return len(data) if self.write_result is None else self.write_result

    def read(self, length, timeout_ms):
        return list(self.frames.pop(0)) if self.frames else []

    def close(self):
        self.closed = True


INFO = {"product_id": M.PID, "usage_page": M.PAGE, "usage": M.USAGE,
        "product_string": M.PRODUCT, "interface_number": 2, "path": b"g98"}


class QhwTest(unittest.TestCase):
    def setUp(self):
        self._saved = (M.hid, M.hidlist, M.report_lengths)

    def tearDown(self):
        M.hid, M.hidlist, M.report_lengths = self._saved

    def poll(self, dev, infos=None):
        M.hid = types.SimpleNamespace(device=lambda: dev)
        M.hidlist = types.SimpleNamespace(enumerate=lambda vid: list(infos or [INFO]))
        M.report_lengths = lambda path: (11, 9)
        return M.MchoseQhwProvider()

    def test_request_is_the_qhw_battery_command(self):
        frame = M.make_request(7, 9)
        self.assertEqual(frame, [0x4D, 1, 3, 0, 4, 9, 0, 7, 0x09])

    def test_level_and_charging(self):
        dev = FakeDev([reply(0, 64, 1)])
        [st] = self.poll(dev).poll()
        self.assertEqual((st.level, st.charging, st.kind, st.name),
                         (64, True, "keyboard", "MCHOSE G98 V3 2.4G"))
        self.assertEqual(dev.writes[0], [0x4D, 1, 3, 0, 4, 9, 0, 0, 0x0E])

    def test_full_is_not_charging(self):
        [st] = self.poll(FakeDev([reply(0, 100, 2)])).poll()
        self.assertEqual((st.level, st.charging), (100, False))

    def test_sequence_is_echoed(self):
        provider = self.poll(FakeDev([reply(1, 75, 0)]))
        provider._sequence = 1
        [st] = provider.poll()
        self.assertEqual(st.level, 75)

    def test_a_stale_sequence_is_not_a_reading(self):
        self.assertEqual(self.poll(FakeDev([reply(3, 75, 0)])).poll(), [])

    def test_only_the_exact_product_and_collection(self):
        for infos in ([dict(INFO, product_string="MCHOSE G98 V3")],
                      [dict(INFO, usage=2)],
                      [dict(INFO, product_id=0x3033)]):
            dev = FakeDev([reply(0)])
            self.assertEqual(self.poll(dev, infos).poll(), [])
            self.assertEqual(dev.writes, [])

    def test_unknown_length_is_not_opened(self):
        dev = FakeDev([reply(0)])
        provider = self.poll(dev)
        M.report_lengths = lambda path: None
        self.assertEqual(provider.poll(), [])
        self.assertEqual(dev.writes, [])

    def test_parse_rejects_bad_values(self):
        self.assertIsNone(M.parse_reply(reply(0, 101, 0), 0))
        self.assertIsNone(M.parse_reply(reply(0, 50, 3), 0))
        self.assertIsNone(M.parse_reply([0x4D, 1, 3, 1, 4, 9, 0, 0, 1, 50], 0))


if __name__ == "__main__":
    unittest.main()
