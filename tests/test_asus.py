"""Tests for providers/asus.py. No hardware is needed.

The fake mouse answers the battery command 12 07 the way G-Helper's AsusMouse.cs
reads it: the echo, the battery in byte 5 and charging in byte 10 of the report
(bytes 4 and 9 once hidapi leaves out report id 0).

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import asus as A  # noqa: E402


def reply(level, charging=0, report_id=False):
    r = [0x12, 0x07, 0x00, 0x00, level, 0x00, 0x00, 0x00, 0x00, charging] + [0] * 54
    return ([0x00] + r) if report_id else r


class FakeMouse:
    """mode: "answer", "error" (ff aa), "zeros", "silent", "events" (button reports first)."""

    def __init__(self, level=87, charging=0, mode="answer", report_id=False, answer_on=1):
        self.level, self.charging, self.mode = level, charging, mode
        self.report_id = report_id
        self.answer_on = answer_on      # answer the n-th request only
        self.writes = []
        self.queue = [[0x12, 0x01, 0x00, 0x04]]      # a stale button report, to be drained
        self.nonblocking = False
        self.opened = 0

    def on_write(self, data):
        self.writes.append(list(data))
        if list(data[:3]) != A.REQUEST or len(self.writes) < self.answer_on:
            return
        if self.mode == "answer":
            self.queue.append(reply(self.level, self.charging, self.report_id))
        elif self.mode == "events":
            self.queue += [[0x12, 0x01, 0, 0], [0x12, 0x00, 0x00, 0x00, 0x02],
                           reply(self.level, self.charging)]
        elif self.mode == "error":
            self.queue.append([0xFF, 0xAA] + [0] * 62)
        elif self.mode == "zeros":
            self.queue.append([0] * 64)

    def on_read(self):
        return self.queue.pop(0) if self.queue else []


class FakeBus:
    def __init__(self, mice):
        self.mice = mice

    def device_class(self):
        bus = self

        class FakeDevice:
            def open_path(self, path):
                self.m = bus.mice[path]
                self.m.opened += 1

            def set_nonblocking(self, on):
                self.m.nonblocking = bool(on)

            def write(self, data):
                self.m.on_write(data)
                return len(data)

            def read(self, n, timeout=None):
                return self.m.on_read()

            def close(self):
                pass

        return FakeDevice


def issue_81_entries(pid=0x1A72):
    # the collections of the Gladius III Wireless AimPoint in issue #81
    return [
        {"product_id": pid, "interface_number": 0, "usage_page": 0xFF01, "usage": 1,
         "path": b"if0-ff01", "product_string": "ROG GIII WIRELESS AIMPOINT"},
        {"product_id": pid, "interface_number": 2, "usage_page": 0xFFC1, "usage": 1,
         "path": b"if2-ffc1", "product_string": "ROG GIII WIRELESS AIMPOINT"},
        {"product_id": pid, "interface_number": 2, "usage_page": 0x0001, "usage": 6,
         "path": b"if2-kbd", "product_string": "ROG GIII WIRELESS AIMPOINT"},
    ]


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self._saved = (A.hid, A.hidlist)

    def tearDown(self):
        A.hid, A.hidlist = self._saved

    def poll(self, entries, mice):
        A.hid = types.SimpleNamespace(device=FakeBus(mice).device_class())
        A.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(entries))
        return A.AsusProvider().poll()


class ParseTest(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(A.parse_reply(reply(87), A.PERCENT), (87, False, ""))
        self.assertEqual(A.parse_reply(reply(40, 1), A.PERCENT), (40, True, ""))

    def test_report_id_in_front(self):
        self.assertEqual(A.parse_reply(reply(87, report_id=True), A.PERCENT), (87, False, ""))

    def test_steps(self):
        self.assertEqual(A.parse_reply(reply(3), A.STEPS), (75, False, "about 75%"))
        self.assertEqual(A.parse_reply(reply(4, 1), A.STEPS), (100, True, "about 100%, charging"))
        self.assertIsNone(A.parse_reply(reply(5), A.STEPS))

    def test_standby_is_not_empty(self):
        # G-Helper: battery 0 without charging = the mouse is in standby
        self.assertIsNone(A.parse_reply(reply(0), A.PERCENT))
        self.assertEqual(A.parse_reply(reply(0, 1), A.PERCENT), (0, True, ""))

    def test_other_reports_are_not_levels(self):
        self.assertIsNone(A.parse_reply([0x12, 0x01, 0, 0, 55, 0, 0, 0, 0, 0], A.PERCENT))
        self.assertIsNone(A.parse_reply([0xFF, 0xAA] + [0] * 20, A.PERCENT))
        self.assertIsNone(A.parse_reply(reply(101), A.PERCENT))
        self.assertIsNone(A.parse_reply([0x12, 0x07, 0, 0], A.PERCENT))     # too short

    def test_every_model_has_a_known_scale(self):
        for pid, (name, scale) in A.KNOWN.items():
            self.assertIn(scale, (A.PERCENT, A.STEPS), hex(pid))


class PollTest(ProviderTest):
    def test_gladius_iii_aimpoint_issue_81(self):
        mouse = FakeMouse(level=87)
        res = self.poll(issue_81_entries(), {b"if0-ff01": mouse})
        self.assertEqual([(r.name, r.level, r.charging, r.kind, r.source) for r in res],
                         [("ROG Gladius III Aimpoint", 87, False, "mouse", "asus")])
        self.assertEqual(len(mouse.writes[0]), 65)
        self.assertEqual(mouse.writes[0][:3], [0x00, 0x12, 0x07])
        self.assertEqual(set(mouse.writes[0][3:]), {0})
        self.assertEqual(len(mouse.writes), 1)

    def test_only_the_vendor_collection_of_interface_0_is_opened(self):
        # a mouse collection on interface 0 as well: it must not get the request
        entries = [dict(issue_81_entries()[0], usage_page=0x0001, usage=2, path=b"if0-mouse")]
        entries += issue_81_entries()
        mice = {p: FakeMouse() for p in (b"if0-mouse", b"if0-ff01", b"if2-ffc1", b"if2-kbd")}
        self.poll(entries, mice)
        self.assertEqual({p: m.opened for p, m in mice.items()},
                         {b"if0-mouse": 0, b"if0-ff01": 1, b"if2-ffc1": 0, b"if2-kbd": 0})

    def test_button_and_profile_reports_are_skipped(self):
        res = self.poll(issue_81_entries(), {b"if0-ff01": FakeMouse(level=64, mode="events")})
        self.assertEqual([r.level for r in res], [64])

    def test_error_zeros_and_silence_give_no_icon(self):
        for mode in ("error", "zeros", "silent"):
            mouse = FakeMouse(mode=mode)
            self.assertEqual(self.poll(issue_81_entries(), {b"if0-ff01": mouse}), [], mode)
            self.assertLessEqual(len(mouse.writes), A.WRITE_ATTEMPTS, mode)

    def test_error_and_zeros_are_final_answers(self):
        # "ff aa" (command not known) and all zeros (asleep) are not repeated
        for mode in ("error", "zeros"):
            mouse = FakeMouse(mode=mode)
            self.poll(issue_81_entries(), {b"if0-ff01": mouse})
            self.assertEqual(len(mouse.writes), 1, mode)

    def test_second_request_is_answered(self):
        mouse = FakeMouse(level=50, answer_on=2)
        res = self.poll(issue_81_entries(), {b"if0-ff01": mouse})
        self.assertEqual([r.level for r in res], [50])
        self.assertEqual(len(mouse.writes), 2)

    def test_cable_and_receiver_share_one_icon(self):
        rx = issue_81_entries(0x1A72)[0]
        cable = dict(issue_81_entries(0x1A70)[0], path=b"cable")
        for order in ([rx, cable], [cable, rx]):          # the charging reading wins either way
            res = self.poll(order, {b"if0-ff01": FakeMouse(level=80),
                                    b"cable": FakeMouse(level=81, charging=1)})
            self.assertEqual([(r.level, r.charging) for r in res], [(81, True)])

    def test_older_model_shows_an_approximate_level(self):
        e = [dict(issue_81_entries()[0], product_id=0x1960)]    # ROG Keris Wireless
        res = self.poll(e, {b"if0-ff01": FakeMouse(level=2)})
        self.assertEqual([(r.name, r.level, r.approx) for r in res],
                         [("ROG Keris Wireless", 50, "about 50%")])

    def test_unknown_asus_devices_are_not_opened(self):
        e = [dict(issue_81_entries()[0], product_id=0x1ACE)]    # OMNI receiver: not included
        mouse = FakeMouse()
        self.assertEqual(self.poll(e, {b"if0-ff01": mouse}), [])
        self.assertEqual(mouse.opened, 0)


# -------------------------------------------------------- the ROG Strix Go 2.4 (#190)

def headset_reply(lsb=0x3B, msb=0x0F, charging=False):
    """The reporter's live frame (#190), byte for byte: the battery voltage at
    bytes 11-12 (LSB first; MSB 0x0F = the 75 % step Armoury Crate shows), byte 9
    the charging state, and byte 13 the constant 0x40 the first port read as the
    level."""
    r = [0xFF, 0x1B, 0x05, 0xFE, 0x12, 0x04, 0x1F, 0x14, 0x01,
         0x0A if charging else 0x03, 0x05, lsb, msb, 0x40, 0x12, 0x01,
         0x00, 0x17, 0x25, 0x05, 0x20, 0xB4, 0x00, 0x0A, 0xFD]
    return r + [0] * (64 - len(r))


class FakeHeadset:
    """A dongle answering the headset exchange with feature reports."""

    def __init__(self, mode="answer", lsb=0x3B, msb=0x0F, charging=False, reply=None):
        self.mode, self.lsb, self.msb = mode, lsb, msb
        self.charging, self.reply = charging, reply
        self.sent, self.opened, self.reads = [], 0, 0

    def on_send(self, data):
        self.sent.append(list(data))

    def on_get(self):
        self.reads += 1
        if self.mode == "silent":
            return []
        if self.mode == "error":
            # the marker at bytes 1-2, with a plausible-looking byte 13 behind it:
            # an error frame must be refused whatever follows it
            return [0xFF, 0xFF, 0xAA] + [0] * 10 + [0x64] + [0] * (64 - 14)
        if self.mode == "zeros":
            # all-zero bytes 1-3 are "off or asleep" even when byte 13 is not zero
            return [0xFF] + [0] * 12 + [0x64] + [0] * (64 - 14)
        if self.mode == "slow" and self.reads == 1:
            return []
        if self.reply is not None:
            return list(self.reply)
        return headset_reply(self.lsb, self.msb, self.charging)


class HeadsetTest(unittest.TestCase):
    def setUp(self):
        self._saved = (A.hid, A.hidlist, A.feature_length)
        self.hs = FakeHeadset()

    def tearDown(self):
        A.hid, A.hidlist, A.feature_length = self._saved

    def headset_entries(self):
        # the MI_03 collections from #190's dump
        return [
            {"product_id": A.HEADSET_PID, "interface_number": 3, "usage_page": 0xFF00,
             "usage": 1, "path": b"mi03-ff00", "product_string": "Hid Interface"},
            {"product_id": A.HEADSET_PID, "interface_number": 3, "usage_page": 0x000C,
             "usage": 1, "path": b"mi03-000c", "product_string": "Hid Interface"},
            {"product_id": A.HEADSET_PID, "interface_number": 3, "usage_page": 0xFFC0,
             "usage": 1, "path": b"mi03-ffc0", "product_string": "Hid Interface"},
        ]

    def poll(self, lengths=None, hs=None, entries=None):
        hs = hs if hs is not None else self.hs
        if entries is None:
            entries = self.headset_entries()
        lengths = lengths if lengths is not None else {b'mi03-ff00': 64, b'mi03-000c': 4,
                                                       b'mi03-ffc0': 64}
        self.opened = None
        test = self
        A.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(entries))
        A.feature_length = lambda path: lengths.get(path)

        class FakeDevice:
            def open_path(self, path):
                test.opened = path
                hs.opened += 1

            def send_feature_report(self, data):
                hs.on_send(data)
                return len(data)

            def get_feature_report(self, rid, n):
                return hs.on_get()

            def close(self):
                pass

        A.hid = types.SimpleNamespace(device=lambda: FakeDevice())
        self.provider = A.AsusProvider()
        return self.provider.poll()

    def test_the_request_is_the_reference_packet(self):
        found = self.poll()
        self.assertEqual(1, len(found))
        self.assertEqual([0xFF, 0x08, 0x00, 0xFD, 0x04, 0x12, 0xF1, 0x03, 0x52, 0x01]
                         + [0x00] * 54, self.hs.sent[0])
        self.assertEqual(64, len(self.hs.sent[0]))

    def test_the_level_is_the_voltage_step_not_the_constant_byte_13(self):
        found = self.poll()
        d = found[0]
        self.assertEqual("asus:rog-strix-go-2.4", d.key)
        self.assertEqual("ROG Strix Go 2.4", d.name)
        self.assertEqual(75, d.level)         # 0x0F3B = 3899 mV -> the 75 % step
        self.assertFalse(d.charging)          # byte 9 is 0x03: on battery
        self.assertEqual("headset", d.kind)
        self.assertIn("-> 75%", "\n".join(self.provider.diagnostics()))

    def test_each_voltage_band_maps_to_its_armoury_step(self):
        # the reference's map (#6067): MSB 0x10 / >= 4100 mV -> 100, MSB 0x0F /
        # >= 3850 -> 75, >= 3700 -> 50, MSB 0x0E / >= 3500 -> 25, lower -> 10
        for lsb, msb, level in ((0x68, 0x10, 100),   # 4200 mV
                                (0x00, 0x0F, 75),    # 3840 mV, the MSB guard alone
                                (0xA6, 0x0E, 50),    # 3750 mV: the voltage arm
                                (0x10, 0x0E, 25),    # 3600 mV, the 0x0E guard
                                (0x48, 0x0D, 10)):   # 3400 mV: the critical step
            with self.subTest(msb=msb, lsb=lsb):
                found = self.poll(hs=FakeHeadset(lsb=lsb, msb=msb))
                self.assertEqual([level], [d.level for d in found])

    def test_charging_comes_from_byte_9(self):
        found = self.poll(hs=FakeHeadset(charging=True))
        self.assertEqual((found[0].level, found[0].charging), (75, True))
        self.assertIn("-> 75% charging", "\n".join(self.provider.diagnostics()))

    def test_the_wired_id_always_charges_and_shares_the_icon(self):
        entries = [{"product_id": A.HEADSET_WIRED_PID, "interface_number": 3,
                    "usage_page": 0xFF00, "usage": 1, "path": b"mi03-ff00",
                    "product_string": "Hid Interface"}]
        found = self.poll(entries=entries)
        d = found[0]
        self.assertEqual((d.key, d.name, d.charging),
                         ("asus:rog-strix-go-2.4", "ROG Strix Go 2.4", True))

    def test_a_frame_without_the_live_header_is_not_read(self):
        # the reference's disconnect signal: byte 1 != 0x1B means the headset is gone
        frame = headset_reply()
        frame[1] = 0x60
        self.assertEqual([], self.poll(hs=FakeHeadset(reply=frame)))
        self.assertEqual([], self.poll(hs=FakeHeadset(reply=headset_reply()[:12])))

    def test_the_collection_is_the_first_with_a_64_byte_feature_report(self):
        self.poll()
        self.assertEqual(b"mi03-ff00", self.opened)
        self.poll(lengths={b'mi03-ff00': 32, b'mi03-000c': 4, b'mi03-ffc0': 64})
        self.assertEqual(b"mi03-ffc0", self.opened)     # ff00 too short: skipped

    def test_no_64_byte_collection_sends_nothing(self):
        found = self.poll(lengths={b'mi03-ff00': 32, b'mi03-000c': 4, b'mi03-ffc0': 8})
        self.assertEqual([], found)
        self.assertEqual([], self.hs.sent)

    def test_error_zeros_and_silence_give_no_icon(self):
        for mode in ("error", "zeros", "silent"):
            with self.subTest(mode=mode):
                hs = FakeHeadset(mode=mode)
                self.assertEqual([], self.poll(hs=hs))

    def test_a_reply_without_the_report_id_is_accepted(self):
        class Ridless(FakeHeadset):
            def on_get(self):
                return headset_reply(self.lsb, self.msb, self.charging)[1:]
        found = self.poll(hs=Ridless())
        self.assertEqual([d.level for d in found], [75])

    def test_a_timeout_is_retried(self):
        hs = FakeHeadset(mode="slow")
        found = self.poll(hs=hs)
        self.assertEqual([d.level for d in found], [75])
        self.assertEqual(2, len(hs.sent))


if __name__ == "__main__":
    unittest.main()
