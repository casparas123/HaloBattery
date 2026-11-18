"""ATK Zero on its 8K receiver (373B:1155; wired 373B:1154), #457.

The Zero uses the Nordic 54L15 64-byte ``0x72`` register protocol, not the older
17-byte Compx report-0x08 exchange.  Recovered from the ATK web HUB and from
zeon256/atkzero-battery; the exchange is:

    request  08 <sum of the body> 72 02 00 <bank> <addr> <len> 00 ...
    reply    08 72 00 <len> 00 <bank?> <addr> <value> ...

The vendor collection is ``0xFF05:0x0001``, report id 8, 64-byte report.  The
bank is 1 behind the 2.4 GHz receiver and 0 on the cable; the battery register
is 0x07 and the charging register is 0x0F.  The dongle's reply to a sleeping
mouse has a non-zero status or does not echo the address, so it is refused
rather than shown as 0 %.

The source (zeon256/atkzero-battery) masks the battery byte's top bit and reads
the charging register separately; this file does the same.  A charge reply that
does not arrive leaves the level valid but the charger unknown instead of
guessing.
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

VID = 0x373B
RECEIVER_PID = 0x1155
WIRED_PID = 0x1154
PAGE, USAGE = 0xFF05, 0x0001
REPORT_ID = 0x08
REPORT_LEN = 64
MARKER = 0x72

BATTERY_ADDR = 0x07
CHARGING_ADDR = 0x0F
BANK_WIRELESS = 0x01
BANK_WIRED = 0x00

NAME = "ATK Zero"
KEY = "atk_zero:1155"

ATTEMPTS = 3
READ_TIMEOUT_MS = 250
FLUSH_TIMEOUT_MS = 30


def make_request(bank: int, address: int, length: int = 1) -> List[int]:
    """The 64-byte register read, with the checksum the dongle expects."""
    body = [MARKER, 0x02, 0x00, bank, address, length]
    pkt = [REPORT_ID, sum(body) & 0xFF] + body
    return pkt + [0x00] * (REPORT_LEN - len(pkt))


def parse_value(r, address: int) -> Optional[int]:
    """The first byte of a register reply, or None when it is not our answer."""
    if not r or len(r) < 8:
        return None
    if r[0] != REPORT_ID or r[1] != MARKER or r[2] != 0x00 or r[5] != address:
        return None
    return r[7]


class AtkZeroProvider(Provider):
    name = "atk_zero"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, bool, float]] = None

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        try:
            infos = hidlist.enumerate(VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(373b): %s", e)
            return []

        receiver = None
        wired = None
        for d in infos:
            pid = d.get("product_id")
            if pid == WIRED_PID:
                wired = d
            elif pid == RECEIVER_PID and (d.get("usage_page"), d.get("usage")) == (PAGE, USAGE):
                receiver = d
        candidates = [d for d in (wired, receiver) if d is not None]
        if not candidates:
            return []

        got = None
        target = None
        for d in candidates:
            target = d
            bank = BANK_WIRED if d.get("product_id") == WIRED_PID else BANK_WIRELESS
            self._diag.append(f"[ATK Zero] pid=373b:{d.get('product_id'):04x} "
                              f"'{d.get('product_string') or ''}' "
                              f"iface={d.get('interface_number')} "
                              f"{PAGE:04x}:{USAGE:04x} bank={bank}")
            got = self._read(d["path"], bank)
            if got is not None:
                break
            if len(candidates) > 1:
                self._diag.append("  no answer; trying the other transport")

        if got is None:
            self._diag.append("  no answer (mouse asleep, off, or out of range)")
            if self._last and time.time() - self._last[1] < 300:
                level, charging, _ = self._last
                return [DeviceStatus(KEY, NAME, level, charging, False, "atk_zero",
                                     kind="mouse", wake_on_input=True,
                                     charging_known=False)]
            return [DeviceStatus(KEY, NAME, None, False, False, "atk_zero",
                                 kind="mouse", wake_on_input=True,
                                 charging_known=False)]

        level, charging = got[0], got[1]
        charging_known = got[2]
        self._diag.append(f"  -> {level}%"
                          f"{' (charging)' if charging else ''}"
                          f"{'' if charging_known else ' (charger unknown)'}")
        self._last = (level, charging, time.time())
        return [DeviceStatus(KEY, NAME, level, charging, True, "atk_zero",
                             kind="mouse", wake_on_input=True,
                             charging_known=charging_known)]

    def _read(self, path, bank: int) -> Optional[Tuple[int, bool, bool]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            self._drain(dev)
            level = self._query(dev, bank, BATTERY_ADDR)
            if level is None:
                # The dongle can keep a stale answer after a cable swap; the
                # source tries the other bank before declaring it silent.
                other = BANK_WIRELESS if bank == BANK_WIRED else BANK_WIRED
                level = self._query(dev, other, BATTERY_ADDR)
                if level is not None:
                    bank = other
            if level is None:
                return None
            level &= 0x7F
            if not 1 <= level <= 100:
                self._diag.append(f"  battery byte out of range: {level}")
                return None
            charge = self._query(dev, bank, CHARGING_ADDR)
            if charge is None:
                return level, False, False
            return level, bool(charge), True
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _query(self, dev, bank: int, address: int) -> Optional[int]:
        for _ in range(ATTEMPTS):
            dev.write(make_request(bank, address))
            r = dev.read(REPORT_LEN, READ_TIMEOUT_MS)
            if not r:
                continue
            self._diag.append(f"  reply addr=0x{address:02x}: {hexdump(r, 12)}")
            value = parse_value(r, address)
            if value is not None:
                return value
        return None

    def _drain(self, dev) -> None:
        for _ in range(4):
            if not dev.read(REPORT_LEN, FLUSH_TIMEOUT_MS):
                return

    def diagnostics(self) -> List[str]:
        return list(self._diag)
