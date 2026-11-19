"""Royal Kludge S85 over its SINOWEALTH 2.4 GHz receiver (258a:0150).

The receiver exposes the keyboard's control channel as ``ff02:0002`` and answers
the CompX keyboard power query documented by
Rainbow132/2.4G-wireless-device-battery-level-acquisition:

    request  13 05 01 00 ... 00 19       (20 bytes, report id 0x13)
    reply    13 0a ... <level> <linked>  (level at byte 6, linked at byte 7)

The first reply to the query is a short acknowledgment; the status frame with
the level arrives after it, so the receiver is polled for a short window rather
than reading once.  Only the read-only power query is sent.

The receiver does not expose a charging query.  The level is shown without a
charging state instead of guessing one; a cable check is part of the test.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

VID = 0x258A
PID = 0x0150
NAME = "Royal Kludge S85"
KEY = "rk:s85"
CONTROL_USAGE = (0xFF02, 0x0002)
REQUEST = [0x13, 0x05, 0x01] + [0x00] * 16 + [0x19]
REPORT_ID = 0x13
PAYLOAD_LEN = 20
READ_TIMEOUT_MS = 150
WINDOW_S = 2.0
ASLEEP_KEEP = 300


def is_s85(info: dict) -> bool:
    return (info.get("product_id") == PID
            and (info.get("usage_page"), info.get("usage")) == CONTROL_USAGE)


def parse_reply(r) -> Optional[Tuple[int, bool]]:
    """(level, linked) from the S85 status frame, or None."""
    if not r or len(r) <= 7:
        return None
    b = list(r)
    if b[0] != REPORT_ID or b[1] != 0x0A:
        return None
    level = b[6]
    if not 0 <= level <= 100:
        return None
    return level, b[7] != 0


class RkS85Provider(Provider):
    name = "rk_s85"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, float]] = None

    def _read(self, path) -> Optional[Tuple[int, bool]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            deadline = time.monotonic() + WINDOW_S
            last_send = 0.0
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now - last_send >= 0.25:
                    try:
                        dev.write(REQUEST)
                    except (OSError, ValueError) as e:
                        self._diag.append(f"  write: {e}")
                        return None
                    last_send = now
                r = dev.read(64, READ_TIMEOUT_MS)
                if not r:
                    continue
                self._diag.append(f"  reply: {hexdump(r, 12)}")
                parsed = parse_reply(r)
                if parsed is not None:
                    return parsed
            self._diag.append("  no status frame (keyboard asleep or receiver idle)")
            return None
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"  query error: {e}")
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
        try:
            infos = hidlist.enumerate(VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(rk s85): %s", e)
            return []
        control = [d for d in infos if is_s85(d)]
        if not control:
            return []
        self._diag.append(f"[RK S85] pid={PID:04x} control collection")
        parsed = self._read(control[0]["path"])
        if parsed is not None:
            level, linked = parsed
            self._last = (level, time.time())
            self._diag.append(f"  -> {level}% linked={linked}")
            return [DeviceStatus(KEY, NAME, level, False, linked, "rk_s85",
                                 kind="keyboard", charging_known=False,
                                 via="2.4 GHz receiver")]
        if self._last and time.time() - self._last[1] < ASLEEP_KEEP:
            return [DeviceStatus(KEY, NAME, self._last[0], False, False, "rk_s85",
                                 kind="keyboard", charging_known=False,
                                 wake_on_input=True)]
        return [DeviceStatus(KEY, NAME, None, False, False, "rk_s85",
                             kind="keyboard", charging_known=False,
                             wake_on_input=True)]

    def diagnostics(self) -> List[str]:
        return list(self._diag)
