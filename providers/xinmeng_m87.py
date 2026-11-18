"""Xinmeng M87 on its 2.4 GHz receiver (0c45:fefe), #455.

The M87 is the same 0x0C45/FEFE dongle family as the EWEADN X87.  Its vendor
channel is usage page 0xFF60 / usage 0x0061, 32-byte packets with report id 0:

    request   00 | AA 10 18 00 00 00 01 00 ...     read the device-info block
    reply     55 10 18 ...  <level at byte 17> ...

That is the protocol implemented by marcelowbr/eweadn-x87 (the reverse-engineered
web HUB for the X87): commands 0x1X read, the info block keeps the battery at
offset 17; the reply starts with 0x55 and echoes the command.  The same dongle is
sold as a generic "2.4G Dongle", so the product string cannot identify the model;
the answer does echo the wired product id (0x8006 for the M87/X87 family).

The wired PID 0x8006 is also the charging signal: while the keyboard is on its
cable it enumerates as its own HID device.  The receiver protocol has no charge
bit, so on the receiver alone the charger is reported as unknown rather than
guessed (a charge-only cable does not have to enumerate).

Only the 0xFF60:0x61 vendor collection is written to.
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

VID = 0x0C45
RECEIVER_PID = 0xFEFE
WIRED_PID = 0x8006
USAGE_PAGE, USAGE = 0xFF60, 0x0061
REPORT_SIZE = 32
REPORT_ID = 0x00

CMD_INFO = 0x10
PACKET_START = 0xAA
REPLY_START = 0x55
LEVEL_INDEX = 17

NAME = "Xinmeng M87"
KEY = "xinmeng:m87"

ATTEMPTS = 3
READ_TIMEOUT_MS = 180
ASLEEP_KEEP = 300


def _payload() -> bytes:
    """The 32-byte X87 info request; byte 6 marks the last (only) block."""
    p = bytearray(REPORT_SIZE)
    p[0] = PACKET_START
    p[1] = CMD_INFO
    p[2] = 0x18
    p[6] = 1
    return bytes(p)


def make_request() -> bytes:
    """The request with hidapi's report id in front: 00 | AA 10 18 ..."""
    return bytes([REPORT_ID]) + _payload()


def _unframe(resp) -> Optional[bytes]:
    """The 32-byte vendor packet, with or without hidapi's report id."""
    if not resp:
        return None
    r = bytes(resp)
    if len(r) >= REPORT_SIZE + 1 and r[0] == REPORT_ID and r[1] == REPLY_START:
        r = r[1:]
    if len(r) >= REPORT_SIZE and r[0] == REPLY_START:
        return r[:REPORT_SIZE]
    return None


def parse_info(resp) -> Optional[Tuple[int, int, int, bool]]:
    """(level, vendor_no, product_no, charging) from the info block, or None.

    The receiver answers only when the keyboard is awake; a short or a foreign
    reply is refused rather than shown as 0 %.
    """
    p = _unframe(resp)
    if p is None or p[1] != CMD_INFO:
        return None
    level = p[LEVEL_INDEX]
    if level > 100:
        return None
    vendor_no = p[12] | (p[13] << 8)
    product_no = p[14] | (p[15] << 8)
    return level, vendor_no, product_no, False


def _is_x66(product: str) -> bool:
    """The AttackShark X66 shares 0c45:fefe but has its own provider."""
    return "x66" in (product or "").lower()


class XinmengM87Provider(Provider):
    name = "xinmeng_m87"

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
            log.warning("hid.enumerate(xinmeng_m87): %s", e)
            return []

        receiver = None
        wired = None
        for d in infos:
            pid = d.get("product_id")
            product = (d.get("product_string") or "").strip()
            vendor = (d.get("usage_page") or 0) == USAGE_PAGE and (d.get("usage") or 0) == USAGE
            if pid == RECEIVER_PID and vendor and not _is_x66(product):
                receiver = d
            elif pid == WIRED_PID:
                # The wired keyboard can expose several collections; prefer the
                # vendor one, the only one that answers the info query.
                if wired is None or vendor:
                    wired = d

        target = wired or receiver
        if target is None:
            return []

        self._diag.append(
            f"[Xinmeng M87] pid={target.get('product_id'):04x} "
            f"'{target.get('product_string') or ''}' "
            f"usage={(target.get('usage_page') or 0):04x}:{(target.get('usage') or 0):04x}"
        )
        got = self._read_info(target)
        if got is None:
            self._diag.append("  no answer (keyboard asleep or off)")
            if self._last and time.time() - self._last[1] < ASLEEP_KEEP:
                return [DeviceStatus(KEY, NAME, self._last[0], False, False,
                                     "xinmeng_m87", kind="keyboard",
                                     charging_known=False)]
            return [DeviceStatus(KEY, NAME, None, False, False,
                                 "xinmeng_m87", kind="keyboard",
                                 charging_known=False)]

        level, vendor_no, product_no, _ = got
        self._diag.append(
            f"  -> {level}% vendor=0x{vendor_no:04x} product=0x{product_no:04x}"
            f"{' (cable)' if wired is not None else ''}"
        )
        self._last = (level, time.time())
        charging = wired is not None
        return [DeviceStatus(KEY, NAME, level, charging, True, "xinmeng_m87",
                             kind="keyboard", charging_known=charging)]

    def _read_info(self, d: dict) -> Optional[Tuple[int, int, int, bool]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(d["path"])
        except Exception as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            try:
                dev.set_nonblocking(True)
            except Exception:
                pass
            for _ in range(ATTEMPTS):
                try:
                    dev.write(make_request())
                except Exception:
                    # Some hidapi builds want the unnumbered 32-byte report.
                    try:
                        dev.write(_payload())
                    except Exception as e:
                        self._diag.append(f"  write: {e}")
                        return None
                r = dev.read(REPORT_SIZE + 1, READ_TIMEOUT_MS)
                if r:
                    self._diag.append(f"  reply: {hexdump(r, 24)}")
                    got = parse_info(r)
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
