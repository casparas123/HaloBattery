"""Tests for providers/sinowealth_rx.py: the 258a:002f receiver's short status reports.

No hardware. The frames are the ones OpenMouse captured from a Redragon M690 Pro
on this receiver (`docs/redragon-m690-pro-testing.md`): a link reply `05 80 01 01`,
a wireless battery reply `05 90 11 <level>` and the cable statuses `05 90 10 01`
(charging) / `05 90 10 02` (charged).

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import sinowealth_rx as S  # noqa: E402


def link(up=True):
    return [0x05, 0x80, 0x01 if up else 0x00, 0x01 if up else 0x02]


def battery(level):
    return [0x05, 0x90, 0x11, level]


def wired(state):
    return [0x05, 0x90, 0x10, state]


class FakeReceiver:
    """One collection path: answers a command with the queued reply for it."""

    def __init__(self, replies, open_error=None, read_error=None):
        self.replies = {cmd: list(q) for cmd, q in replies.items()}
        self.open_error = open_error
        self.read_error = read_error          # get_feature_report raises
        self.sent = []
        self.pending = []

    def open_path(self, path):
        if self.open_error is not None:
            raise self.open_error

    def send_feature_report(self, data):
        self.sent.append(bytes(data))
        queue = self.replies.get(data[1], [])
        self.pending.append(queue.pop(0) if queue else [])
        return len(data)

    def get_feature_report(self, report_id, length):
        if self.read_error is not None:
            raise self.read_error
        return self.pending.pop(0) if self.pending else []

    def close(self):
        pass


def entry(path, interface=1, page=0xFF00, usage=1, pid=0x002F, product="2.4G Wireless Receiver"):
    return {"path": path, "interface_number": interface, "usage_page": page,
            "usage": usage, "product_id": pid, "product_string": product}


class PollTest(unittest.TestCase):
    def setUp(self):
        self._saved = (S.hid, S.hidlist, S.time)
        self.now = [1000.0]
        S.time = types.SimpleNamespace(time=lambda: self.now[0], sleep=lambda s: None)
        self.receivers = {}
        self.order = []

        def device():
            return _Device(self.receivers, self.order)

        S.hid = types.SimpleNamespace(device=device)
        self.provider = S.SinowealthReceiverProvider()

    def tearDown(self):
        S.hid, S.hidlist, S.time = self._saved

    def poll(self, entries, receivers):
        self.receivers = receivers
        S.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(entries))
        return self.provider.poll()

    # ---------------------------------------------------------------- parsing

    def test_the_captured_receiver_frames_read_the_level(self):
        self.assertEqual(S.parse_link(link(True)), True)
        self.assertEqual(S.parse_link(link(False)), False)
        self.assertEqual(S.parse_battery(battery(70)), (70, False, False))

    def test_the_cable_statuses_are_charging(self):
        self.assertEqual(S.parse_battery(wired(S.CHARGING)), (None, True, True))
        self.assertEqual(S.parse_battery(wired(S.CHARGED)), (100, True, True))

    def test_undocumented_replies_are_refused(self):
        self.assertIsNone(S.parse_battery([0x05, 0x90, 0x12, 70]))      # other reply type
        self.assertIsNone(S.parse_battery([0x05, 0x90, 0x11, 0]))       # link not up yet
        self.assertIsNone(S.parse_battery([0x05, 0x90, 0x11, 101]))
        self.assertIsNone(S.parse_battery([0x05, 0x90, 0x11]))          # short
        self.assertIsNone(S.parse_battery([0x04, 0x90, 0x11, 70]))      # other report id
        self.assertIsNone(S.parse_battery([0x05, 0x91, 0x11, 70]))      # other command
        self.assertIsNone(S.parse_battery([0x05, 0x90, 0x10, 0x03]))    # unknown cable state
        self.assertIsNone(S.parse_battery([]))
        self.assertIsNone(S.parse_link([0x05, 0x80, 0x02, 0x01]))
        self.assertIsNone(S.parse_link([0x05, 0x81, 0x01, 0x01]))

    # ---------------------------------------------------------------- polling

    def test_a_wireless_receiver_gives_the_level_and_no_charging_claim(self):
        receiver = FakeReceiver({0x80: [link(True)], 0x90: [battery(70)]})
        out = self.poll([entry(b"rx")], {b"rx": receiver})
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.online, s.charging_known,
                           s.wake_on_input, s.kind) for s in out],
                         [("sinowealth:002f", "Sinowealth wireless mouse", 70, False, True,
                           False, False, "mouse")])
        self.assertEqual([bytes(S.make_request(0x80)), bytes(S.make_request(0x90))],
                         receiver.sent)
        self.assertIn("-> 70% (charging not reported)", "\n".join(self.provider.diagnostics()))

    def test_the_link_is_read_before_the_battery(self):
        receiver = FakeReceiver({0x80: [link(True)], 0x90: [battery(80)]})
        self.poll([entry(b"rx")], {b"rx": receiver})
        self.assertEqual([0x80, 0x90], [data[1] for data in receiver.sent])
        self.assertEqual([S.REPORT_ID] * 2, [data[0] for data in receiver.sent])
        self.assertEqual(S.REQUEST_SIZE, {len(data) for data in receiver.sent}.pop())

    def test_a_cable_status_is_reported_as_charging(self):
        out = self.poll([entry(b"rx")],
                        {b"rx": FakeReceiver({0x80: [link(True)], 0x90: [wired(S.CHARGING)]})})
        self.assertEqual([(s.level, s.charging, s.charging_known) for s in out],
                         [(None, True, True)])
        out = self.poll([entry(b"rx")],
                        {b"rx": FakeReceiver({0x80: [link(True)], 0x90: [wired(S.CHARGED)]})})
        self.assertEqual([(s.level, s.charging) for s in out], [(100, True)])

    def test_a_dormant_receiver_gets_one_retry(self):
        receiver = FakeReceiver({0x80: [[], link(True)], 0x90: [battery(64)]})
        out = self.poll([entry(b"rx")], {b"rx": receiver})
        self.assertEqual(64, [s.level for s in out][0])
        self.assertEqual([0x80, 0x80, 0x90], [data[1] for data in receiver.sent])

    def test_the_settings_collection_is_skipped_and_the_feature_one_used(self):
        # the descriptor lists ff00:0001 three times: report 8 (settings), the input
        # report, and the 7-byte feature report this exchange needs
        settings = FakeReceiver({}, read_error=OSError("wrong report"))
        input_only = FakeReceiver({}, open_error=OSError("no feature reports"))
        good = FakeReceiver({0x80: [link(True)], 0x90: [battery(42)]})
        out = self.poll([entry(b"settings"), entry(b"input"), entry(b"features")],
                        {b"settings": settings, b"input": input_only, b"features": good})
        self.assertEqual(42, [s.level for s in out][0])
        self.assertEqual([b"settings", b"input", b"features"], self.order)
        diag = "\n".join(self.provider.diagnostics())
        self.assertIn("trying iface=1 ff00:0001", diag)
        self.assertIn("collection: iface=1 ff00:0001", diag)
        self.assertEqual(2, len(good.sent))

    def test_an_off_mouse_keeps_the_last_level_greyed(self):
        receiver = FakeReceiver({0x80: [link(True)], 0x90: [battery(55)]})
        self.poll([entry(b"rx")], {b"rx": receiver})
        self.now[0] += 60
        asleep = FakeReceiver({0x80: [link(False)]})
        out = self.poll([entry(b"rx")], {b"rx": asleep})
        self.assertEqual([(s.level, s.online, s.charging, s.wake_on_input, s.charging_known)
                          for s in out], [(55, False, False, True, False)])
        self.assertEqual([bytes(S.make_request(0x80))], asleep.sent)     # no battery write

    def test_an_off_mouse_after_the_keep_window_has_no_level(self):
        receiver = FakeReceiver({0x80: [link(True)], 0x90: [battery(55)]})
        self.poll([entry(b"rx")], {b"rx": receiver})
        self.now[0] += S.ASLEEP_KEEP + 1
        out = self.poll([entry(b"rx")], {b"rx": FakeReceiver({0x80: [link(False)]})})
        self.assertEqual([(s.level, s.online, s.wake_on_input) for s in out],
                         [(None, False, True)])

    def test_silence_keeps_the_icon_offline(self):
        out = self.poll([entry(b"rx")], {b"rx": FakeReceiver({})})
        self.assertEqual([(s.level, s.online, s.wake_on_input) for s in out],
                         [(None, False, True)])

    def test_a_receiver_without_its_vendor_collection_is_not_opened(self):
        receiver = FakeReceiver({})
        entries = [entry(b"rx", page=0x0001, usage=0x0002),
                   entry(b"rx", page=0x000C, usage=0x0001)]
        out = self.poll(entries, {b"rx": receiver})
        self.assertEqual([], receiver.sent)
        self.assertEqual(1, len(out))
        self.assertIn("nothing opened", "\n".join(self.provider.diagnostics()))

    def test_an_open_error_does_not_raise(self):
        out = self.poll([entry(b"rx")], {b"rx": FakeReceiver({}, open_error=OSError("busy"))})
        self.assertEqual([(s.level, s.online) for s in out], [(None, False)])
        self.assertIn("open: busy", "\n".join(self.provider.diagnostics()))

    def test_another_sinowealth_product_is_ignored(self):
        # 258a:2022 is the Glorious Model O receiver, read by its own provider
        out = self.poll([entry(b"glorious", pid=0x2022)], {})
        self.assertEqual([], out)

    def test_the_mouse_present_but_unpaired_shows_no_level(self):
        out = self.poll([entry(b"rx")],
                        {b"rx": FakeReceiver({0x80: [link(True)], 0x90: [[0x05, 0x90, 0x11, 0]]})})
        self.assertEqual([(s.level, s.online) for s in out], [(None, True)])


class _Device:
    """hid.device() stand-in: routes open_path to the fake for that path."""

    def __init__(self, receivers, order):
        self.receivers = receivers
        self.order = order
        self.current = None

    def open_path(self, path):
        self.order.append(path)
        self.current = self.receivers[path]
        self.current.open_path(path)

    def send_feature_report(self, data):
        assert self.current is not None
        return self.current.send_feature_report(data)

    def get_feature_report(self, report_id, length):
        assert self.current is not None
        return self.current.get_feature_report(report_id, length)

    def close(self):
        assert self.current is not None
        self.current.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
