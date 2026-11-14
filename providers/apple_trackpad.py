"""Apple Magic Trackpad 2 battery over its vendor HID collection.

The trackpad exposes a vendor collection ``FF00:0014`` (on the Bluetooth
``004C:0265`` and USB ``05AC:0265`` ids) that answers a read-only input-report
request for id ``0x90``:

    [0] 0x90 report id
    [1] battery flags
    [2] charge status, percent

Source: imbushuo/mac-precision-touchpad, ``Mt2BatteryStatusReport`` and the
trackpad settings page (MIT), which reads the same report from Windows. Only
this read is sent; no gesture, pairing or firmware command is written.

The input-report request is a GET; some Windows driver stacks expose it as a
feature report instead, so that form is the read-only fallback. **Unverified on
this reporter's hardware** until the test build is run.
"""
from __future__ import annotations

import time
from typing import List, Optional

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

VID_BT = 0x004C
VID_USB = 0x05AC
PID = 0x0265
PAGE, USAGE = 0xFF00, 0x0014
REPORT_ID = 0x90
REPORT_LEN = 3
LEVEL_AT = 2
READ_TIMEOUT_MS = 100
ATTEMPTS = 2


def parse_report(r) -> Optional[int]:
    """Percent from the battery report, or None if this is not one."""
    if not r or len(r) <= LEVEL_AT:
        return None
    if r[0] != REPORT_ID:
        return None
    level = r[LEVEL_AT]
    return level if 0 <= level <= 100 else None


class AppleTrackpadProvider(Provider):
    name = "apple_trackpad"

    def __init__(self):
        self._diag: List[str] = []

    def _read(self, path) -> Optional[int]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            for attempt in range(ATTEMPTS):
                if attempt:
                    time.sleep(0.1)
                r = None
                try:
                    r = dev.get_input_report(REPORT_ID, REPORT_LEN)
                except (OSError, IOError, ValueError) as e:
                    self._diag.append(f"  attempt {attempt + 1} input: {e}")
                if not r:
                    # The UWP driver reads this as an input report; hidapi on
                    # some Windows stacks only exposes the same GET as feature.
                    try:
                        r = dev.get_feature_report(REPORT_ID, REPORT_LEN)
                    except (OSError, IOError, ValueError) as e:
                        self._diag.append(f"  attempt {attempt + 1} feature: {e}")
                if not r:
                    self._diag.append(f"  attempt {attempt + 1}: no reply")
                    continue
                self._diag.append(f"  attempt {attempt + 1} reply: {hexdump(r, 8)}")
                level = parse_report(r)
                if level is not None:
                    return level
            self._diag.append("  no usable level")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        out: List[DeviceStatus] = []
        seen = set()
        for vid in (VID_BT, VID_USB):
            try:
                infos = hidlist.enumerate(vid)
            except Exception as e:  # pragma: no cover
                log.warning("hid.enumerate(apple trackpad): %s", e)
                continue
            for d in infos:
                if d.get("product_id") != PID:
                    continue
                if (d.get("usage_page"), d.get("usage")) != (PAGE, USAGE):
                    continue
                path = d.get("path")
                if path in seen:
                    continue
                seen.add(path)
                name = (d.get("product_string") or "").strip() or "Apple Magic Trackpad 2"
                self._diag.append(f"[Magic Trackpad 2] {name} vid={vid:04x} pid={PID:04x} "
                                  f"iface={d.get('interface_number')}")
                level = self._read(path)
                if level is None:
                    continue
                out.append(DeviceStatus("apple_trackpad:0265", "Apple Magic Trackpad 2",
                                        level, False, True, "apple_trackpad", kind="mouse"))
                break
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
