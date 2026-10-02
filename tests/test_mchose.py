"""Tests for providers/mchose.py. No hardware: the receivers are fakes.

The newer 0x3837 devices (#189) answer on *output* report 0x55: the frame is `65 01`
followed by zeros, byte for byte what M HUB's own driver sends, and the answer is
`65 <level> <state>` on an input report 0x55 - hidapi puts the report id in front of
it, WebHID does not, so both shapes are pinned. The older families keep their feature
exchange (0x11/0x12, command 0x06, every payload byte inverted).

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import mchose as M  # noqa: E402


class FakeReceiver:
    """One receiver on one path, both channels.

    new_answer: (level, state) for the report-0x55 ask, or None for a device that never
    answers it. legacy_answer: (level, charge) for the feature exchange, or None.
    noise_first: a frame that is not the answer before the real one. ridless: answers
    without the report id, the way WebHID sees them.
    """

    def __init__(self, vid, new_answer=(57, M.STATE_DISCHARGE), legacy_answer=None,
                 noise_first=False, ridless=False, model=0x0031, flags=0x09):
        self.vid, self.model, self.flags = vid, model, flags
        self.new_answer, self.legacy_answer = new_answer, legacy_answer
        self.noise_first, self.ridless = noise_first, ridless
        self.writes, self.feature_sends, self.opened, self.reads = [], [], 0, 0

    def write(self, data):
        data = list(data)
        self.writes.append(data)
        return len(data)

    def read(self, n, timeout=None):
        if self.new_answer is None:
            return []
        self.reads += 1
        if self.noise_first and self.reads == 1:
            return [M.STATUS_REPORT, 0x00, 0x05, 0x03] + [0x00] * 8
        level, state = self.new_answer
        frame = [M.STATUS_MARK, level, state] + [0x00] * 9
        return ([M.STATUS_REPORT] + frame) if not self.ridless else frame

    def send_feature_report(self, data):
        data = list(data)
        self.feature_sends.append(data)
        return len(data)

    def get_feature_report(self, rid, n):
        if self.legacy_answer is None:
            return []
        level, charge = self.legacy_answer
        pay = (self.vid.to_bytes(2, "little") + self.model.to_bytes(2, "little")
               + bytes(4) + bytes([self.flags, level, charge]))
        return [rid, M.CMD_STATUS ^ 0xFF] + list(M._invert(pay))


class FakeBus:
    """hid.device() dispatches to the receiver the opened path belongs to."""

    def __init__(self, receivers):
        self.receivers = receivers

    def device_factory(self):
        bus = self

        class FakeDevice:
            def open_path(self, path):
                self.r = bus.receivers[path]
                self.r.opened += 1

            def set_nonblocking(self, flag=True):
                pass

            def write(self, data):
                return self.r.write(data)

            def read(self, n, timeout=None):
                return self.r.read(n)

            def send_feature_report(self, data):
                return self.r.send_feature_report(data)

            def get_feature_report(self, rid, n):
                return self.r.get_feature_report(rid, n)

            def close(self):
                pass

        return FakeDevice()


def iface(vid, pid, iface_n, page, usage, path_suffix, product):
    return {"vendor_id": vid, "product_id": pid, "interface_number": iface_n,
            "usage_page": page, "usage": usage,
            "path": b"%04x-%04x-%s" % (vid, pid, path_suffix),
            "product_string": product, "serial_number": "s"}


def k99_v3_entries():       # the reporter's dump in #189
    return [iface(0x3837, 0x3033, 3, 0xFF70, 0x0071, b"ff70", "MCHOSE K99 V3 2.4G"),
            iface(0x3837, 0x3033, 2, 0xFF31, 0x0074, b"ff31", "MCHOSE K99 V3 2.4G")]


def v7_entries():           # the reporter's dump in #189
    return [iface(0x3837, 0x1016, 2, 0xFF01, 0x0001, b"ff01", "MCHOSE V7"),
            iface(0x3837, 0x1016, 3, 0xFF60, 0x0061, b"ff60", "MCHOSE V7")]


def m7_entries():           # the measured family wins on feature first
    return [iface(0x5253, 0x1020, 1, 0xFF01, 0x0001, b"m7", "MCHOSE M7 Ultra")]


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self._saved = (M.hid, M.hidlist, M.time)
        self.ifaces = []
        self.receivers = {}
        self.bus = FakeBus(self.receivers)
        M.hid = types.SimpleNamespace(device=lambda: FakeBus.device_factory(self.bus))
        M.hidlist = types.SimpleNamespace(
            enumerate=lambda vid: [d for d in self.ifaces if d["vendor_id"] == vid])
        self.clock = [1000.0]
        M.time = types.SimpleNamespace(sleep=lambda s: None,
                                       time=lambda: self.clock[0])

    def tearDown(self):
        M.hid, M.hidlist, M.time = self._saved

    def poll_with(self, entries, receivers):
        """Poll with the given receivers; every other collection is silent."""
        self.ifaces = list(entries)
        self.receivers.clear()
        for e in entries:
            self.receivers.setdefault(e["path"], FakeReceiver(e["vendor_id"], new_answer=None))
        self.receivers.update(receivers)
        return M.MchoseProvider().poll()

    def solo(self, entries, rx):
        """One receiver answering, on the first collection of the device."""
        return self.poll_with(entries, {entries[0]["path"]: rx})


class Status055Tests(ProviderTest):
    def test_the_ask_is_the_bundles_frame(self):
        entries = k99_v3_entries()
        rx = FakeReceiver(0x3837, new_answer=(57, M.STATE_DISCHARGE))
        self.ifaces = list(entries)
        self.receivers.clear()
        self.receivers[entries[0]["path"]] = rx
        self.receivers[entries[1]["path"]] = FakeReceiver(0x3837, new_answer=None)
        provider = M.MchoseProvider()
        found = provider.poll()
        self.assertEqual(1, len(rx.writes))
        self.assertEqual([M.STATUS_REPORT, 0x65, 0x01] + [0x00] * 61, rx.writes[0])
        self.assertEqual(64, len(rx.writes[0]))
        self.assertEqual([], rx.feature_sends)          # the 0x55 answer ends the walk
        self.assertEqual([(s.key, s.name, s.level, s.charging) for s in found],
                         [("mchose:3837:3033", "MCHOSE K99 V3 2.4G", 57, False)])
        self.assertTrue(any("answered on report 0x55" in line
                            for line in provider.diagnostics()))

    def test_states(self):
        for state, charging in ((M.STATE_DISCHARGE, False),
                                (M.STATE_CHARGING, True),
                                (M.STATE_FULL, True)):
            with self.subTest(state=state):
                found = self.solo(v7_entries(), FakeReceiver(0x3837, new_answer=(88, state)))
                self.assertEqual([s.charging for s in found], [charging])

    def test_asleep_is_no_reading(self):
        found = self.solo(k99_v3_entries(),
                          FakeReceiver(0x3837, new_answer=(66, M.STATE_ASLEEP)))
        self.assertEqual([], found)

    def test_a_level_above_100_is_refused(self):
        found = self.solo(k99_v3_entries(),
                          FakeReceiver(0x3837, new_answer=(101, M.STATE_CHARGING)))
        self.assertEqual([], found)

    def test_a_stray_frame_before_the_answer_is_skipped(self):
        rx = FakeReceiver(0x3837, new_answer=(44, M.STATE_CHARGING), noise_first=True)
        found = self.solo(k99_v3_entries(), rx)
        self.assertEqual([s.level for s in found], [44])
        self.assertEqual(2, rx.reads)

    def test_the_answer_without_the_report_id_is_accepted(self):
        found = self.solo(k99_v3_entries(),
                          FakeReceiver(0x3837, new_answer=(72, M.STATE_FULL), ridless=True))
        self.assertEqual([(s.level, s.charging) for s in found], [(72, True)])

    def test_the_second_collection_answers_when_the_first_does_not(self):
        entries = k99_v3_entries()
        first, second = entries[0]["path"], entries[1]["path"]
        rx1 = FakeReceiver(0x3837, new_answer=None)                       # silent
        rx2 = FakeReceiver(0x3837, new_answer=(31, M.STATE_DISCHARGE))    # answers
        found = self.poll_with(entries, {first: rx1, second: rx2})
        self.assertEqual([s.level for s in found], [31])
        self.assertEqual(1, len(rx1.writes))              # asked once, then moved on
        self.assertEqual(1, len(rx2.writes))

    def test_an_older_0x3837_device_falls_back_to_the_feature_channels(self):
        entries = k99_v3_entries()
        rx = FakeReceiver(0x3837, new_answer=None, legacy_answer=(90, 1))
        found = self.poll_with(entries, {entries[0]["path"]: rx})
        self.assertEqual([(s.level, s.charging) for s in found], [(90, True)])
        self.assertTrue(rx.feature_sends)                 # the fallback was actually tried

    def test_the_measured_5253_family_never_gets_the_0x55_frame(self):
        entries = m7_entries()
        rx = FakeReceiver(0x5253, new_answer=(50, M.STATE_CHARGING), legacy_answer=(100, 0))
        found = self.poll_with(entries, {entries[0]["path"]: rx})
        self.assertEqual([(s.level, s.charging) for s in found], [(100, False)])
        self.assertEqual([], rx.writes)                   # the new channel stays off it
        self.assertTrue(rx.feature_sends)

    def test_the_two_reporter_devices_get_their_pictograms(self):
        entries = k99_v3_entries() + v7_entries()
        k99_path, v7_path = entries[0]["path"], entries[2]["path"]
        found = self.poll_with(entries,
                               {k99_path: FakeReceiver(0x3837, new_answer=(57, M.STATE_DISCHARGE)),
                                v7_path: FakeReceiver(0x3837, new_answer=(42, M.STATE_CHARGING))})
        self.assertEqual({s.name: s.kind for s in found},
                         {"MCHOSE K99 V3 2.4G": "keyboard", "MCHOSE V7": "mouse"})
        self.assertEqual({s.key for s in found},
                         {"mchose:3837:3033", "mchose:3837:1016"})   # two devices, two icons


class KindTests(unittest.TestCase):
    def test_mchoses_own_names(self):
        # keyboard names come from M HUB's device lists; everything else is a mouse
        for name, kind in (("MCHOSE K99 V3 2.4G", "keyboard"),
                           ("MCHOSE G98 V3", "keyboard"),
                           ("MCHOSE Ace 68 Air", "keyboard"),
                           ("MCHOSE V7", "mouse"),
                           ("MCHOSE M7 Ultra", "mouse"),
                           ("MCHOSE A7 V2 Ultra", "mouse")):
            with self.subTest(name=name):
                self.assertEqual(M.kind_of(name), kind)


if __name__ == "__main__":
    unittest.main()
