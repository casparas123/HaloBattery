"""Xinmeng M87 / M87 Lite on their 2.4 GHz receiver (0c45:fefe), #455.

The original M87 is the same 0x0C45/FEFE dongle family as the EWEADN X87.  Its
vendor channel is usage page 0xFF60 / usage 0x0061, 32-byte packets with report
id 0:

    request   AA 10 18 00 00 00 00 00 ...     info block, first 24 bytes
    request   AA 10 08 18 00 00 01 00 ...     info block, bytes 24..31
    reply     55 10 <len> <offset> ...        data starts at byte 8

That is the protocol implemented by marcelowbr/eweadn-x87 (the reverse-engineered
web HUB for the X87): commands 0x1X read, the info block is 32 bytes and is read
in two chunks (24 + 8 bytes), and the battery is at **payload** offset 17.

The M87 Lite (and Attack Shark M87 PRO V2 / C87) use a simpler Xinmeng query on
the same dongle, documented by the MIT omarchy-gadget-batteries collector and
taken from Xinmeng's M87 keyboard application:

    request   20 01 00 00 ...                 output report, 32 bytes
    reply     20 01 ?? <battery>              level at byte 3

The same dongle is sold as a generic "2.4G Dongle", so the product string cannot
identify the model; the reply selects the protocol and therefore the model name.

The wired PID 0x8006 is the charging signal: while the keyboard is on its cable
it enumerates as its own HID device.  The receiver protocol has no charge bit,
so on the receiver alone the charger is reported as unknown rather than guessed
(a charge-only cable does not have to enumerate).  A wired M87 Lite exposes only
its keyboard collection (0001:0006), not the vendor channel; never send the
vendor query to it.  If only that wired identity is present, keep a previous
level if one exists and report charging, but do not invent a read.

Only the 0xFF60:0x61 vendor collection is written to for the M87 protocol.  The
M87 Lite query is sent to the same vendor collection when present.
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
LITE_START = 0x20
LITE_CMD = 0x01
LITE_LEVEL_INDEX = 3

NAME = "Xinmeng M87"
NAME_LITE = "Xinmeng M87 Lite"
KEY = "xinmeng:m87"
KEY_LITE = "xinmeng:m87lite"

ATTEMPTS = 3
LITE_ATTEMPTS = 2
READ_TIMEOUT_MS = 180
LITE_READ_TIMEOUT_MS = 700
ASLEEP_KEEP = 300


def _packet(length: int, offset: int, last: bool) -> bytes:
    """One 32-byte X87 info request packet."""
    p = bytearray(REPORT_SIZE)
    p[0] = PACKET_START
    p[1] = CMD_INFO
    p[2] = length
    p[3] = offset & 0xFF
    p[4] = (offset >> 8) & 0xFF
    p[5] = 0
    p[6] = 1 if last else 0
    return bytes(p)


def make_requests() -> List[bytes]:
    """The two packets that read the 32-byte info block."""
    return [_packet(24, 0, False), _packet(8, 24, True)]


def make_request() -> bytes:
    """Backward-compatible first packet, with hidapi's report id in front."""
    return bytes([REPORT_ID]) + make_requests()[0]


def make_lite_request() -> bytes:
    """The 32-byte M87 Lite / M87 PRO V2 battery request, report id 0."""
    p = bytearray(REPORT_SIZE)
    p[0] = LITE_START
    p[1] = LITE_CMD
    return bytes(p)


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


def parse_chunk(resp) -> Optional[Tuple[int, bytes]]:
    """(payload offset, payload bytes) from one info reply, or None."""
    p = _unframe(resp)
    if p is None or p[1] != CMD_INFO:
        return None
    length = p[2]
    offset = p[3] | (p[4] << 8)
    if length == 0 or length > 24 or 8 + length > REPORT_SIZE:
        return None
    return offset, p[8:8 + length]


def parse_info(data) -> Optional[Tuple[int, int, int, bool]]:
    """(level, vendor_no, product_no, charging) from the assembled 32-byte payload."""
    if not data or len(data) < 18:
        return None
    p = list(data[:32])
    level = p[LEVEL_INDEX]
    if level > 100:
        return None
    vendor_no = p[12] | (p[13] << 8)
    product_no = p[14] | (p[15] << 8)
    return level, vendor_no, product_no, False


def parse_lite(resp) -> Optional[int]:
    """The level from an M87 Lite reply, with or without a leading report id."""
    if not resp:
        return None
    r = bytes(resp)
    if len(r) >= LITE_LEVEL_INDEX + 2 and r[0] == REPORT_ID and r[1] == LITE_START:
        r = r[1:]
    if len(r) < LITE_LEVEL_INDEX + 1 or r[0] != LITE_START or r[1] != LITE_CMD:
        return None
    level = r[LITE_LEVEL_INDEX]
    return level if level <= 100 else None


def _is_x66(product: str) -> bool:
    """The AttackShark X66 shares 0c45:fefe but has its own provider."""
    return "x66" in (product or "").lower()


def _is_lite(product: str) -> bool:
    """The wired identity can name the Lite model; the receiver cannot."""
    return "lite" in (product or "").lower()


class XinmengM87Provider(Provider):
    name = "xinmeng_m87"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, float]] = None
        self._lite = False

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
                # The wired keyboard can expose several collections.  Prefer a
                # vendor collection for the old M87; the Lite has only the
                # keyboard collection, which must never receive the vendor
                # query.  The wired identity is used for charging regardless.
                if wired is None or vendor:
                    wired = d

        target = receiver
        if target is None and wired is not None:
            wired_vendor = ((wired.get("usage_page") or 0) == USAGE_PAGE and
                            (wired.get("usage") or 0) == USAGE)
            if wired_vendor:
                target = wired
        if target is None and wired is None:
            return []

        if target is None:
            if wired is None:
                return []
            self._diag.append(
                f"[Xinmeng M87] wired-only identity pid={wired.get('product_id'):04x} "
                f"'{wired.get('product_string') or ''}' "
                f"usage={(wired.get('usage_page') or 0):04x}:{(wired.get('usage') or 0):04x}"
            )
            self._diag.append("  no vendor collection; cable is the charging signal only")
            level = self._last[0] if self._last and time.time() - self._last[1] < ASLEEP_KEEP else None
            name, key = (NAME_LITE, KEY_LITE) if _is_lite(wired.get("product_string") or "") else (NAME, KEY)
            return [DeviceStatus(key, name, level, True, False,
                                 "xinmeng_m87", kind="keyboard", charging_known=True)]

        self._diag.append(
            f"[Xinmeng M87] pid={target.get('product_id'):04x} "
            f"'{target.get('product_string') or ''}' "
            f"usage={(target.get('usage_page') or 0):04x}:{(target.get('usage') or 0):04x}"
        )
        got = self._read_info(target)
        if got is None:
            got = self._read_lite(target)
            if got is not None:
                self._lite = True
        if got is None:
            self._diag.append("  no answer (keyboard asleep or off)")
            name, key = (NAME_LITE, KEY_LITE) if self._lite or _is_lite(
                (wired or {}).get("product_string") or "") else (NAME, KEY)
            level = self._last[0] if self._last and time.time() - self._last[1] < ASLEEP_KEEP else None
            return [DeviceStatus(key, name, level, wired is not None, False,
                                 "xinmeng_m87", kind="keyboard",
                                 charging_known=wired is not None)]

        if isinstance(got, int):
            level, vendor_no, product_no = got, 0, 0
            self._lite = True
            self._diag.append(f"  -> {level}% (M87 Lite protocol)")
        else:
            level, vendor_no, product_no, _ = got
            self._lite = False
            self._diag.append(
                f"  -> {level}% vendor=0x{vendor_no:04x} product=0x{product_no:04x}"
                f"{' (cable)' if wired is not None else ''}"
            )
        name, key = (NAME_LITE, KEY_LITE) if self._lite or _is_lite(
            (wired or {}).get("product_string") or "") else (NAME, KEY)
        self._last = (level, time.time())
        charging = wired is not None
        return [DeviceStatus(key, name, level, charging, True, "xinmeng_m87",
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
            # The X87 HUB drains stale input reports before a read; without
            # this the first answer can be a queued packet from the last poll.
            for _ in range(8):
                try:
                    if not dev.read(REPORT_SIZE + 1, 0):
                        break
                except Exception:
                    break
            data = bytearray(REPORT_SIZE)
            for packet in make_requests():
                payload = None
                for _ in range(ATTEMPTS):
                    try:
                        dev.write(bytes([REPORT_ID]) + packet)
                    except Exception:
                        # Some hidapi builds want the unnumbered 32-byte report.
                        try:
                            dev.write(packet)
                        except Exception as e:
                            self._diag.append(f"  write: {e}")
                            return None
                    r = dev.read(REPORT_SIZE + 1, READ_TIMEOUT_MS)
                    if not r:
                        continue
                    self._diag.append(f"  reply: {hexdump(r, 24)}")
                    parsed = parse_chunk(r)
                    if parsed is not None:
                        payload = parsed
                        break
                if payload is None:
                    return None
                offset, chunk = payload
                if offset >= REPORT_SIZE:
                    return None
                data[offset:offset + len(chunk)] = chunk[:REPORT_SIZE - offset]
            return parse_info(data)
        except Exception as e:  # pragma: no cover - hidapi raises OSError
            self._diag.append(f"  error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return None

    def _read_lite(self, d: dict) -> Optional[int]:
        """The Xinmeng M87 Lite / M87 PRO V2 query on the same receiver."""
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
            request = make_lite_request()
            for _ in range(LITE_ATTEMPTS):
                try:
                    dev.write(bytes([REPORT_ID]) + request)
                except Exception:
                    try:
                        dev.write(request)
                    except Exception as e:
                        self._diag.append(f"  lite write: {e}")
                        return None
                r = dev.read(REPORT_SIZE + 1, LITE_READ_TIMEOUT_MS)
                if not r:
                    continue
                self._diag.append(f"  lite reply: {hexdump(r, 24)}")
                level = parse_lite(r)
                if level is not None:
                    return level
        except Exception as e:  # pragma: no cover - hidapi raises OSError
            self._diag.append(f"  lite error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return None

    def diagnostics(self) -> List[str]:
        return list(self._diag)
