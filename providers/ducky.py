"""Ducky OK-M on its 2.4 GHz dongle (I-CHIP 056d:c077), #545.

The dongle presents three vendor collections; the 64-byte one on interface 1
(usage page 0xFF00, usage 0x0001) takes a 64-byte output report:

    0a 05 00 ...        report id 0x0a, command 0x05, zero padded
    0a 05 ... 50 2a     echoed header; byte 14 = level, byte 15 = volts x 10

The same I-CHIP dongle id is used by the Yunzii B87; its owner documented this
read in jordan-lee-code/yunzii-applet (no licence file, so protocol facts only,
no code copied). That project also notes the dongle caches the level from the
keyboard's last reconnect, so a level can lag until the keyboard is power-cycled.

Charging is not exposed by this dongle. The keyboard on its USB-C data cable
enumerates as a second device (28e9:31e2, "Ducky OK-M-98 Gaming Keyboard"), and
that device's presence is the charging signal; its collection is only enumerated,
never opened. A wall charger that does not enumerate cannot be seen. The wired id
was confirmed by the reporter's diagnostics in #545.
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

VID = 0x056D
PID = 0xC077
NAME = "Ducky OK-M"
KEY = f"ducky:{PID:04x}"

# The keyboard on its USB-C data cable enumerates as a second, differently named
# device (28e9:31e2 'Ducky OK-M-98 Gaming Keyboard', from #545's diagnostics). The
# dongle itself has no charging field, so the cable device's presence is the
# charging signal - the same method the Yunzii B87 owner uses (their wired id is
# 28e9:30ad, a different model).
WIRED_VID = 0x28E9
WIRED_PID = 0x31E2
WIRED_WORDS = ("ducky", "ok-m")

USAGE_PAGE, USAGE = 0xFF00, 0x0001
PRODUCT_WORDS = ("ducky", "ok-m")
REPORT_ID, CMD_BATTERY = 0x0A, 0x05
REPORT_SIZE = 64
LEVEL_BYTE = 14
VOLT_BYTE = 15

READ_ATTEMPTS = 4
READ_TIMEOUT_MS = 250
ASLEEP_KEEP = 300


def make_request() -> List[int]:
    frame = [0] * REPORT_SIZE
    frame[0], frame[1] = REPORT_ID, CMD_BATTERY
    return frame


def parse_reply(r) -> Optional[Tuple[int, int]]:
    """(level, voltage x10) from ``0a 05 ...`` with or without a leading report id."""
    if not r:
        return None
    start = None
    if len(r) > LEVEL_BYTE + 1 and r[0] == REPORT_ID and r[1] == CMD_BATTERY:
        start = 0
    elif (len(r) > LEVEL_BYTE + 2 and r[0] == 0x00 and r[1] == REPORT_ID
          and r[2] == CMD_BATTERY):
        start = 1
    if start is None:
        return None
    level = r[start + LEVEL_BYTE]
    volts = r[start + VOLT_BYTE] if len(r) > start + VOLT_BYTE else 0
    if not 1 <= level <= 100:
        return None
    return level, volts


def pick(infos: List[dict]) -> Optional[dict]:
    for d in infos:
        if d.get("usage_page") == USAGE_PAGE and d.get("usage") == USAGE:
            return d
    return None


class DuckyProvider(Provider):
    name = "ducky"

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
            log.warning("hid.enumerate(ducky): %s", e)
            return []
        mine = [d for d in infos
                if d.get("product_id") == PID
                and all(w in (d.get("product_string") or "").lower() for w in PRODUCT_WORDS)]
        if not mine:
            same_id = [d for d in infos if d.get("product_id") == PID]
            if same_id:
                self._diag.append(
                    "[Ducky] pid=c077 is present but its product string is not a Ducky OK-M; "
                    "not claiming it")
            return []
        d = pick(mine)
        cable = self._cable_present()
        if d is None:
            self._diag.append(f"[Ducky] pid={PID:04x} '{NAME}': no 0xFF00:0x0001 channel")
            return [DeviceStatus(KEY, NAME, None, cable, False, "ducky",
                                 kind="keyboard")]
        self._diag.append(f"[Ducky] pid={PID:04x} '{NAME}' iface={d.get('interface_number')} "
                          f"{USAGE_PAGE:04x}:{USAGE:04x}")
        parsed = self._ask(d)
        if parsed is not None:
            level, volts = parsed
            self._last = (level, time.time())
            self._diag.append(f"  -> {level}%" + (f", {volts / 10:.1f} V" if volts else ""))
            return [DeviceStatus(KEY, NAME, level, cable, True, "ducky",
                                 kind="keyboard")]
        self._diag.append("  no answer (keyboard asleep or off)")
        if self._last and time.time() - self._last[1] < ASLEEP_KEEP:
            return [DeviceStatus(KEY, NAME, self._last[0], cable, False, "ducky",
                                 kind="keyboard")]
        return [DeviceStatus(KEY, NAME, None, cable, False, "ducky",
                             kind="keyboard")]

    def _cable_present(self) -> bool:
        """True when the keyboard's wired USB-C device is enumerated.

        Presence only - the wired collection is never opened. A wall charger that
        does not enumerate cannot be seen, which is the same limit every
        presence-based charging signal in this app has.
        """
        try:
            infos = hidlist.enumerate(WIRED_VID)
        except Exception:  # pragma: no cover - enumeration is best effort
            return False
        for d in infos:
            if d.get("product_id") != WIRED_PID:
                continue
            name = (d.get("product_string") or "").lower()
            if all(word in name for word in WIRED_WORDS):
                self._diag.append(f"[Ducky] wired id {WIRED_VID:04x}:{WIRED_PID:04x} "
                                  f"'{d.get('product_string')}': charging")
                return True
        return False

    def _ask(self, d: dict) -> Optional[Tuple[int, int]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(d["path"])
        except Exception as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            sent = dev.write(bytes(make_request()))
            if sent is not None and sent < 0:
                self._diag.append(f"  write refused ({sent})")
                return None
            for _ in range(READ_ATTEMPTS):
                r = dev.read(REPORT_SIZE + 1, READ_TIMEOUT_MS)
                if not r:
                    continue
                self._diag.append(f"  reply: {hexdump(r, 16)}")
                parsed = parse_reply(r)
                if parsed is not None:
                    return parsed
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
