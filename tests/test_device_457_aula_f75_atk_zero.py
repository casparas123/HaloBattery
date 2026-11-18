"""Tests for the AULA F75 and ATK Zero readers, #457; no hardware needed.

The F75 receiver is 3554:fa09 with a 20-byte FF02:0002 output report and the
0x13/0x4A exchange measured in ushineko/sanshoku#36.  The ATK Zero receiver is
373b:1155 with a 64-byte FF05:0001 report and the 0x72 register protocol from
zeon256/atkzero-battery (battery register 0x07, charger 0x0F).

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import atk_zero as Z  # noqa: E402
from providers import aula_f75 as F  # noqa: E402


# --- AULA F75 ---------------------------------------------------------------

def f75_reply(level=97, state=0x01, command=0x4A):
    r = [0x00] * 20
    r[0], r[1], r[2], r[3], r[4], r[5], r[6] = 0x13, command, 0x01, 0x00, 0x02, level, state
    r[-1] = sum(r[:-1]) & 0xFF
    return r


def f75_entries(output_length=20):
    return [{"vendor_id": 0x3554, "product_id": 0xFA09, "interface_number": 1,
             "usage_page": 0xFF02, "usage": 0x0002,
             "path": b"f75", "product_string": "2.4G Wireless Receiver",
             "output_length": output_length}]


def wired_entries():
    return [{"vendor_id": 0x258A, "product_id": 0x010C, "interface_number": 1,
             "usage_page": 0xFF00, "usage": 0x0001, "path": b"f75-wired",
             "product_string": "Gaming Keyboard"}]


class FakeDev:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.sent = []
        self.path = None

    def open_path(self, path):
        self.path = path

    def close(self):
        pass

    def write(self, data):
        self.sent.append(bytes(data))
        return len(data)

    def read(self, size, timeout_ms=0):
        if not self.sent:
            return []            # nothing asked yet: no stale frame
        return self.replies.pop(0) if self.replies else []


class F75ParseTests(unittest.TestCase):
    def test_the_request_is_the_twenty_byte_battery_query(self):
        req = F.make_request()
        self.assertEqual(len(req), 20)
        self.assertEqual((req[0], req[1]), (0x13, 0x4A))
        self.assertEqual(req[-1], sum(req[:-1]) & 0xFF)

    def test_a_measured_reply_reads_the_level_and_state(self):
        self.assertEqual(F.parse_reply(f75_reply(97, 0x01)), (97, 0x01))
        self.assertEqual(F.parse_reply(f75_reply(100, 0x10)), (100, 0x10))

    def test_a_status_push_or_bad_checksum_is_refused(self):
        self.assertIsNone(F.parse_reply(f75_reply(command=0x0A)))
        self.assertIsNone(F.parse_reply(f75_reply()[:-1] + [0]))
        self.assertIsNone(F.parse_reply([]))
        self.assertIsNone(F.parse_reply(f75_reply(0)))
        self.assertIsNone(F.parse_reply(f75_reply(101)))


class F75PollTests(unittest.TestCase):
    def setUp(self):
        self._hid = F.hid
        self._enumerate = F.hidlist.enumerate
        self._length = F.compx.output_length

    def tearDown(self):
        F.hid = self._hid
        F.hidlist.enumerate = self._enumerate
        F.compx.output_length = self._length

    def poll(self, fake, length=20):
        F.hid = types.SimpleNamespace(device=lambda: fake)
        F.hidlist.enumerate = lambda vid=0: [e for e in f75_entries(length)
                                              if e["vendor_id"] == vid]
        F.compx.output_length = lambda path: length
        p = F.AulaF75Provider()
        return p.poll(), p.diagnostics()

    def test_the_f75_is_read_as_a_keyboard_on_battery(self):
        fake = FakeDev([f75_reply(97, 0x01)])
        out, diag = self.poll(fake)
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.charging_known, s.kind)
                          for s in out],
                         [("aula:f75", "AULA F75", 97, False, True, "keyboard")])
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(len(fake.sent[0]), 20)
        self.assertEqual((fake.sent[0][0], fake.sent[0][1]), (0x13, 0x4A))

    def test_the_cable_state_keeps_the_last_real_level(self):
        fake = FakeDev([f75_reply(97, 0x01)])
        F.hid = types.SimpleNamespace(device=lambda: fake)
        F.hidlist.enumerate = lambda vid=0: f75_entries()
        F.compx.output_length = lambda path: 20
        provider = F.AulaF75Provider()
        self.assertEqual(provider.poll()[0].level, 97)

        fake = FakeDev([f75_reply(100, 0x10)])
        F.hid = types.SimpleNamespace(device=lambda: fake)
        out, diag = provider.poll(), provider.diagnostics()
        self.assertEqual(out[0].level, 97)
        self.assertTrue(out[0].charging)
        self.assertTrue(out[0].charging_known)

    def test_the_cable_state_without_a_known_level_shows_charging_only(self):
        out, _ = self.poll(FakeDev([f75_reply(100, 0x10)]))
        self.assertIsNone(out[0].level)
        self.assertTrue(out[0].charging)
        self.assertTrue(out[0].charging_known)

    def test_no_answer_keeps_the_receiver_keyboard_visible_greyed_out(self):
        out, diag = self.poll(FakeDev([]))
        self.assertEqual([(s.key, s.name, s.level, s.online, s.charging_known)
                          for s in out],
                         [("aula:f75", "AULA F75", None, False, False)])
        self.assertTrue(any("no answer" in line for line in diag))

    def test_a_later_silence_keeps_the_last_level_without_claiming_charging(self):
        fake = FakeDev([f75_reply(97, 0x01)])
        F.hid = types.SimpleNamespace(device=lambda: fake)
        F.hidlist.enumerate = lambda vid=0: f75_entries()
        F.compx.output_length = lambda path: 20
        provider = F.AulaF75Provider()
        self.assertEqual(provider.poll()[0].level, 97)

        fake = FakeDev([])
        F.hid = types.SimpleNamespace(device=lambda: fake)
        out = provider.poll()
        self.assertEqual((out[0].level, out[0].online, out[0].charging,
                          out[0].charging_known), (97, False, False, False))

    def test_the_wired_identity_turns_silence_into_charging(self):
        fake = FakeDev([])
        F.hid = types.SimpleNamespace(device=lambda: fake)
        F.hidlist.enumerate = lambda vid=0: (
            f75_entries() if vid == 0x3554 else
            wired_entries() if vid == 0x258A else [])
        F.compx.output_length = lambda path: 20
        provider = F.AulaF75Provider()
        provider._last_level = 97
        out = provider.poll()
        self.assertEqual((out[0].level, out[0].charging, out[0].charging_known,
                          out[0].online), (97, True, True, True))
        self.assertTrue(any("wired identity 258a:010c" in line for line in provider.diagnostics()))

    def test_the_wired_identity_overrides_an_on_battery_reply(self):
        fake = FakeDev([f75_reply(100, 0x01)])
        F.hid = types.SimpleNamespace(device=lambda: fake)
        F.hidlist.enumerate = lambda vid=0: (
            f75_entries() if vid == 0x3554 else
            wired_entries() if vid == 0x258A else [])
        F.compx.output_length = lambda path: 20
        out = F.AulaF75Provider().poll()
        self.assertEqual((out[0].level, out[0].charging, out[0].charging_known),
                         (100, True, True))

    def test_a_wired_keyboard_without_an_aula_receiver_is_not_claimed(self):
        fake = FakeDev([])
        F.hid = types.SimpleNamespace(device=lambda: fake)
        F.hidlist.enumerate = lambda vid=0: wired_entries() if vid == 0x258A else []
        F.compx.output_length = lambda path: 20
        self.assertEqual(F.AulaF75Provider().poll(), [])


# --- ATK Zero ---------------------------------------------------------------

def atk_reply(address=0x07, value=83, status=0):
    r = [0x00] * 64
    r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7] = 0x08, 0x72, status, 0x3A, 0x00, address, 1, value
    return r


def atk_entries(pid=0x1155):
    return [{"vendor_id": 0x373B, "product_id": pid, "interface_number": 3,
             "usage_page": 0xFF05, "usage": 0x0001,
             "path": b"atk", "product_string": "Wireless mouse 8k dongle-L1"}]


class ATKParseTests(unittest.TestCase):
    def test_the_request_carries_the_72_register_frame(self):
        req = Z.make_request(1, 0x07)
        self.assertEqual(len(req), 64)
        self.assertEqual((req[0], req[1], req[2], req[3], req[4], req[5], req[6], req[7]),
                         (0x08, sum(req[2:8]) & 0xFF, 0x72, 0x02, 0x00, 1, 0x07, 1))

    def test_a_reply_must_echo_the_register_and_status(self):
        self.assertEqual(Z.parse_value(atk_reply(0x07, 83), 0x07), 83)
        self.assertEqual(Z.parse_value(atk_reply(0x0F, 1), 0x0F), 1)
        self.assertIsNone(Z.parse_value(atk_reply(0x07, 83, status=1), 0x07))
        self.assertIsNone(Z.parse_value(atk_reply(0x0F, 1), 0x07))
        self.assertIsNone(Z.parse_value([], 0x07))


class ATKPollTests(unittest.TestCase):
    def setUp(self):
        self._hid = Z.hid
        self._enumerate = Z.hidlist.enumerate

    def tearDown(self):
        Z.hid = self._hid
        Z.hidlist.enumerate = self._enumerate

    def poll(self, fake, pid=0x1155):
        Z.hid = types.SimpleNamespace(device=lambda: fake)
        Z.hidlist.enumerate = lambda vid=0: [e for e in atk_entries(pid)
                                              if e["vendor_id"] == vid]
        p = Z.AtkZeroProvider()
        return p.poll(), p.diagnostics()

    def test_the_zero_is_read_and_charging_comes_from_register_0f(self):
        fake = FakeDev([atk_reply(0x07, 83), atk_reply(0x0F, 1)])
        out, diag = self.poll(fake)
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.charging_known, s.kind)
                          for s in out],
                         [("atk_zero:1155", "ATK Zero", 83, True, True, "mouse")])
        self.assertEqual([s[5] for s in fake.sent[:1]], [1])   # wireless bank
        self.assertEqual([s[6] for s in fake.sent[:1]], [0x07])
        self.assertEqual([s[6] for s in fake.sent[1:]], [0x0F])
        self.assertTrue(any("bank=1" in line for line in diag))

    def test_a_missing_charge_reply_leaves_the_charger_unknown(self):
        fake = FakeDev([atk_reply(0x07, 83)])
        out, _ = self.poll(fake)
        self.assertEqual(out[0].level, 83)
        self.assertFalse(out[0].charging_known)

    def test_an_asleep_status_is_refused(self):
        fake = FakeDev([atk_reply(0x07, 83, status=1)] * 6)
        out, diag = self.poll(fake)
        self.assertEqual([(s.level, s.online) for s in out], [(None, False)])
        self.assertTrue(any("no answer" in line for line in diag))

    def test_the_wired_id_uses_bank_zero(self):
        fake = FakeDev([atk_reply(0x07, 60), atk_reply(0x0F, 0)])
        out, _ = self.poll(fake, pid=0x1154)
        self.assertEqual(out[0].level, 60)
        self.assertEqual(fake.sent[0][5], 0)

    def test_a_zero_battery_byte_is_refused(self):
        fake = FakeDev([atk_reply(0x07, 0)] * 6)
        out, _ = self.poll(fake)
        self.assertEqual([(s.level, s.online, s.charging_known) for s in out],
                         [(None, False, False)])


if __name__ == "__main__":
    unittest.main()
