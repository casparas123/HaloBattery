"""Xinmeng Zero 3 (M3) on its 2.4 GHz receiver (3151:5045), #455.

The receiver is the Rongyuan family the URX HUB web driver calls ``rongyuan``.
Its vendor collection is usage page 0xFFFF / usage 0x0002, feature report id 0,
64 bytes.  A status query is a zero-filled 64-byte report with 0xF7 in byte 0;
the answer is parsed by the HUB as:

    byte 0  data ready (1)
    byte 1  keyboard battery
    byte 2  mouse battery          <- the Zero 3 level
    byte 3  keyboard disconnected
    byte 4  mouse disconnected
    byte 5  set-report ready
    byte 6  connected devices
    byte 7  USB mode

This is the ``parseReceiverStatus``/``buildReceiverGetStatus`` exchange in
hub.urx.co.in's bundle (2026-11-18).  The receiver exposes no charge bit, so
charging is taken from the mouse's wired USB identity (3151:5043) when it is
present; on the receiver alone it is reported as unknown instead of guessed.

Only the vendor collection is touched, and only with the read-only 0xF7 query.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

try:
    import hid  # type: ignore
except Exception:  # pragma: no cover - only when the package is missing
    hid = None

VID = 0x3151
RECEIVER_PID = 0x5045
WIRED_PID = 0x5043
USAGE_PAGE, USAGE = 0xFFFF, 0x0002
REPORT_SIZE = 64
REPORT_ID = 0x00

CMD_STATUS = 0xF7
NAME = "Xinmeng Zero 3"
KEY = "xinmeng:zero3"

ATTEMPTS = 3
READ_TIMEOUT_MS = 300
ASLEEP_KEEP = 300


def make_request() -> bytes:
    """64-byte feature report with the status command in byte 0."""
    p = bytearray(REPORT_SIZE)
    p[0] = CMD_STATUS
    return bytes(p)


def parse_status(resp) -> Optional[Tuple[int, int, int]]:
    """(level, usb_mode, connected_devices) from a status reply, or None.

    A disconnected mouse is refused: its stale battery byte must not become a
    live 0 %.  The status is accepted with or without hidapi's report-id byte.
    """
    if not resp:
        return None
    r = bytes(resp)
    if len(r) >= REPORT_SIZE + 1 and r[0] == REPORT_ID:
        r = r[1:]
    if len(r) < 8:
        return None
    if r[4] == 1 or r[6] == 0:
        # Disconnected, or the wired identity echoing the request: neither is
        # a receiver status with a level.
        return None
    level = r[2]
    if level > 100:
        return None
    return level, r[7], r[6]


class XinmengZero3Provider(Provider):
    name = "xinmeng_zero3"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, float]] = None

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        try:
            infos = hidlist.enumerate(VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(xinmeng_zero3): %s", e)
            return []

        receiver = None
        wired = None
        for d in infos:
            pid = d.get("product_id")
            vendor = (d.get("usage_page") or 0) == USAGE_PAGE and (d.get("usage") or 0) == USAGE
            if pid == WIRED_PID:
                # Prefer the wired device's vendor collection if it has one.
                if wired is None or vendor:
                    wired = d
            elif pid == RECEIVER_PID and vendor:
                receiver = d

        # The receiver is the status source even when the cable is plugged in;
        # the wired id is only the charging signal (and echoes zeroes instead
        # of a receiver status).
        target = receiver or wired
        if target is None:
            return []

        self._diag.append(
            f"[Xinmeng Zero 3] pid={target.get('product_id'):04x} "
            f"'{target.get('product_string') or ''}' "
            f"usage={(target.get('usage_page') or 0):04x}:{(target.get('usage') or 0):04x}"
        )
        got = self._read_status(target)
        if got is None:
            self._diag.append("  no answer (mouse asleep, off, or unplugged)")
            level = self._last[0] if self._last and time.time() - self._last[1] < ASLEEP_KEEP else None
            return [DeviceStatus(KEY, NAME, level, wired is not None, False,
                                 "xinmeng_zero3", kind="mouse", wake_on_input=True,
                                 charging_known=wired is not None)]

        level, usb_mode, connected = got
        self._diag.append(f"  -> {level}% usb_mode={usb_mode} connected={connected}"
                          f"{' (cable)' if wired is not None else ''}")
        self._last = (level, time.time())
        charging = wired is not None
        return [DeviceStatus(KEY, NAME, level, charging, True, "xinmeng_zero3",
                             kind="mouse", wake_on_input=True,
                             charging_known=charging)]

    def _read_status(self, d: dict) -> Optional[Tuple[int, int, int]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(d["path"])
        except Exception as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            for _ in range(ATTEMPTS):
                try:
                    # hidapi's feature-report API takes the report id first.
                    dev.send_feature_report(bytes([REPORT_ID]) + make_request())
                except Exception:
                    try:
                        dev.send_feature_report(make_request())
                    except Exception as e:
                        self._diag.append(f"  send feature: {e}")
                        return None
                try:
                    r = dev.get_feature_report(REPORT_ID, REPORT_SIZE + 1)
                except Exception:
                    r = dev.get_feature_report(REPORT_ID, REPORT_SIZE)
                if r:
                    self._diag.append(f"  reply: {hexdump(r, 24)}")
                    got = parse_status(r)
                    if got is not None:
                        return got
        except Exception as e:  # pragma: no cover - hidapi raises OSError
            self._diag.append(f"  error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return None

    def diagnostics(self) -> List[str]:
        return list(self._diag)
