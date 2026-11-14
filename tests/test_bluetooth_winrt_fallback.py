"""Bluetooth: when WinRT fails for every paired device, use the PnP fallback (#366)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import bluetooth as B  # noqa: E402

MAC = "AABBCCDDEEFF"
ROOT = "BTHENUM\\DEV_" + MAC + "\\A&1&0&BLUETOOTHDEVICE_" + MAC


def snapshot(err="Wait failed"):
    return {
        "devs": [{"id": ROOT, "name": "Monster Storm XKH03", "status": "OK"}],
        "props": [{"id": ROOT, "key": B.K_BAT, "data": 90}],
        "aep": [{"addr": MAC, "conn": None, "le": False, "err": err, "cls": None}],
        "aep_count": 1, "pnp_count": 1, "prop_count": 1,
    }


class WinRTFallbackTest(unittest.TestCase):
    def test_all_roots_failing_does_not_hide_a_battery_device(self):
        diag = []
        out = B.group_devices(snapshot(), diag)
        self.assertEqual([(s.name, s.level) for s in out],
                         [("Monster Storm XKH03", 90)])
        self.assertTrue(any("failed for every root" in line for line in diag))

    def test_a_single_device_error_still_hides_an_unconnected_device(self):
        data = snapshot(err="one device failed")
        other = "112233445566"
        data["devs"].append({"id": "BTHENUM\DEV_" + other, "name": "Other", "status": "OK"})
        data["aep"].append({"addr": other, "conn": False, "le": False, "err": None, "cls": None})
        data["aep_count"] = 2
        diag = []
        out = B.group_devices(data, diag)
        # One failing root is not the system-wide case: the old conservative
        # behaviour is kept when there is no PnP connection flag.
        self.assertEqual(out, [])
        self.assertFalse(any("failed for every root" in line for line in diag))


if __name__ == "__main__":
    unittest.main()
