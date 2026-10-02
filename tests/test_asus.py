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


# ------------------------------------------------------------ the ROG Pelta (#199)
def pelta_reply(command, level=0, charging=0, report_id=True, nak=False):
    """One reply as the Pelta frames it: 12 <command> echoed in bytes 1-2 of the
    buffer whose byte 0 is the report id (or in bytes 0-1 without one), the level in
    byte 6, charging in byte 5, FF AA in bytes 1-2 as a NAK."""
    if nak:
        r = [0xFF, 0xAA] + [0] * 62
        return ([0xCC] + r) if report_id else r
    if command == A.PELTA_CMD_BATTERY:
        r = [0x12, command, 0x00, 0x00, 0x05, level, 0x14, 0x01] + [0] * 56
    else:
        r = [0x12, command, 0x00, 0x00, charging] + [0] * 59
    return ([0xCC] + r) if report_id else r


def pelta_entries(usage_page=0xFF00, path=b"pelta-ff00"):
    return [{"product_id": A.PELTA_PID, "interface_number": 3, "usage_page": usage_page,
             "usage": 1, "path": path, "product_string": "ROG PELTA (2.4GHz)"}]


class FakePelta:
    """mode: "answer", "no_id" (replies carry no report id), "nak" (FF AA),
    "junk" (two other frames first), "silent"."""

    def __init__(self, level=77, charging=0, mode="answer"):
        self.level, self.charging, self.mode = level, charging, mode
        self.writes = []
        self.queue = [[0xCC, 0x12, 0x01, 0x00, 0x04]]     # a stale volume event
        self.opened = 0

    def on_write(self, data):
        self.writes.append(list(data))
        if list(data[:2]) != [0xCC, 0x12] or self.mode == "silent":
            return
        command = data[2]
        if self.mode == "junk":
            self.queue += [[0xCC, 0x12, 0x01, 0x00, 0x77],
                           [0xCC, 0x12, 0x02, 0x00, 0x01]]
        if self.mode == "nak":
            self.queue.append(pelta_reply(command, nak=True))
        elif command == A.PELTA_CMD_BATTERY:
            self.queue.append(pelta_reply(command, level=self.level,
                                          report_id=(self.mode != "no_id")))
        else:
            self.queue.append(pelta_reply(command, charging=self.charging,
                                          report_id=(self.mode != "no_id")))

    def on_read(self):
        return self.queue.pop(0) if self.queue else []


class PeltaParseTest(unittest.TestCase):
    def test_the_level_and_the_charging_byte(self):
        self.assertEqual(A.parse_pelta_reply(pelta_reply(0x07, level=77), 0x07), 77)
        self.assertEqual(A.parse_pelta_reply(pelta_reply(0x08, charging=1), 0x08), 1)
        self.assertEqual(A.parse_pelta_reply(pelta_reply(0x08, charging=0), 0x08), 0)

    def test_a_reply_without_the_report_id(self):
        self.assertEqual(A.parse_pelta_reply(
            pelta_reply(0x07, level=64, report_id=False), 0x07), 64)

    def test_key_updates_and_other_commands_are_not_replies(self):
        self.assertIsNone(A.parse_pelta_reply([0xCC, 0x12, 0x01, 0x00, 0x55], 0x07))
        self.assertIsNone(A.parse_pelta_reply([], 0x07))
        self.assertIsNone(A.parse_pelta_reply([0xCC, 0x12], 0x07))

    def test_nak_in_both_shapes(self):
        self.assertIsNone(A.parse_pelta_reply(pelta_reply(0x07, nak=True), 0x07))
        echo_then_nak = [0xCC, 0x12, 0x07, 0x00, 0x00, 0xFF, 0xAA] + [0] * 57
        self.assertIsNone(A.parse_pelta_reply(echo_then_nak, 0x07))
        # on the charging command the NAK is what separates "no answer" (None) from
        # a plain "not charging" (0) - without it this would read as a real 0
        charging_nak = [0xCC, 0x12, 0x08, 0x00, 0x00, 0xFF, 0xAA] + [0] * 57
        self.assertIsNone(A.parse_pelta_reply(charging_nak, 0x08))
        self.assertEqual(A.parse_pelta_reply(pelta_reply(0x08, charging=0), 0x08), 0)

    def test_out_of_range_is_refused(self):
        self.assertEqual(A.parse_pelta_reply(pelta_reply(0x07, level=0), 0x07), 0)
        self.assertIsNone(A.parse_pelta_reply(pelta_reply(0x07, level=101), 0x07))
        self.assertIsNone(A.parse_pelta_reply(pelta_reply(0x07, level=255), 0x07))


class PeltaPollTest(ProviderTest):
    def test_pelta_reads_level_and_charging(self):
        pelta = FakePelta(level=77, charging=1)
        res = self.poll(pelta_entries(), {b"pelta-ff00": pelta})
        self.assertEqual([(r.key, r.name, r.level, r.charging, r.kind) for r in res],
                         [("asus:rog-pelta", "ROG Pelta", 77, True, "headset")])
        self.assertEqual(len(pelta.writes), 2)
        self.assertEqual(pelta.writes[0][:3], [0xCC, 0x12, 0x07])
        self.assertEqual(pelta.writes[1][:3], [0xCC, 0x12, 0x08])
        self.assertEqual(len(pelta.writes[0]), A.PELTA_PACKET_LENGTH)
        self.assertEqual(set(pelta.writes[0][3:]), {0})

    def test_pelta_reply_without_report_id_is_read_too(self):
        res = self.poll(pelta_entries(), {b"pelta-ff00": FakePelta(mode="no_id")})
        self.assertEqual([r.level for r in res], [77])

    def test_pelta_event_frames_are_skipped(self):
        pelta = FakePelta(level=64, mode="junk")
        res = self.poll(pelta_entries(), {b"pelta-ff00": pelta})
        self.assertEqual([r.level for r in res], [64])

    def test_pelta_nak_and_silence_keep_the_icon_without_a_level(self):
        for mode in ("nak", "silent"):
            pelta = FakePelta(mode=mode)
            res = self.poll(pelta_entries(), {b"pelta-ff00": pelta})
            self.assertEqual([(r.name, r.level, r.kind) for r in res],
                             [("ROG Pelta", None, "headset")], mode)

    def test_pelta_uses_the_ff00_collection_only(self):
        entries = pelta_entries(usage_page=0xFF07, path=b"pelta-ff07") + \
            pelta_entries()
        wrong = FakePelta()
        pelta = FakePelta(level=90)
        res = self.poll(entries, {b"pelta-ff07": wrong, b"pelta-ff00": pelta})
        self.assertEqual([r.level for r in res], [90])
        self.assertEqual((wrong.opened, pelta.opened), (0, 1))

    def test_pelta_without_a_vendor_collection_shows_the_device(self):
        res = self.poll(pelta_entries(usage_page=0xFF07, path=b"pelta-ff07"), {})
        self.assertEqual([(r.name, r.level) for r in res], [("ROG Pelta", None)])


if __name__ == "__main__":
    unittest.main()
