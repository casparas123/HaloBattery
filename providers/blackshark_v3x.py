"""Razer BlackShark V3 X HyperSpeed on its 2.4 GHz receiver (1532:057d, #280).

This is the newer BlackShark protocol, not the 2023 "PA" frames and not the
BlackShark V3's 0x02/ff14 exchange.  It was reverse-engineered byte-for-byte
from Synapse captures by everyoneexe/blackshark-linux (`PROTOCOL.md`,
`blackshark-rs/src/protocol.rs`, MIT) for the Xbox edition (1532:0A5B).  The
receiver publishes the same vendor pages on the PC edition's interface 0; this
provider is the test for that hardware.

Every frame is a 64-byte feature report with report id 0x07:

    [0]=0x07  [1]=0  [2]=transaction id
    [6]=argument count  [7]=class  [8]=command  [9..]=arguments
    [62]=XOR of bytes 3..60

Battery is a read-only GET: class 0x07, command 0x80, one zero argument.
The reply keeps the same fields; byte 10 is the percentage (0..100).

The repository opens the first interface that accepts the feature report; a
receiver may expose several vendor pages, so all of them are tried here.  No
configuration, firmware or lighting command is ever sent.
"""
from __future__ import annotations

from typing import List, Optional

try:
    import hid  # type: ignore
except Exception:  # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

VID = 0x1532
PID = 0x057D
NAME = "Razer BlackShark V3 X HyperSpeed"
KEY = f"blackshark_v3x:{PID:04x}"

REPORT_ID = 0x07
REPORT_LEN = 64
TRANSACTION = 0x03
CRC_FIRST, CRC_LAST, CRC_INDEX = 3, 60, 62
CLASS_BATTERY = 0x07
CMD_BATTERY = 0x80


def checksum(frame) -> int:
    crc = 0
    for b in frame[CRC_FIRST:CRC_LAST + 1]:
        crc ^= b
    return crc


def make_query(transaction: int = TRANSACTION) -> bytes:
    frame = bytearray(REPORT_LEN)
    frame[0] = REPORT_ID
    frame[2] = transaction
    frame[6] = 0x02          # argument count: one zero byte
    frame[7] = CLASS_BATTERY
    frame[8] = CMD_BATTERY
    frame[9] = 0x00
    frame[CRC_INDEX] = checksum(frame)
    return bytes(frame)


def parse_reply(data) -> Optional[int]:
    """Battery percentage from the reply, or None when it is not one."""
    d = list(data or [])
    if len(d) <= 10:
        return None
    # hidapi on Windows returns the report id first; the repo's layout includes it.
    if d[0] != REPORT_ID:
        return None
    if d[6] != 0x02 or d[7] != CLASS_BATTERY or d[8] != CMD_BATTERY:
        return None
    level = d[10]
    if 0 <= level <= 100:
        return level
    return None


def candidates(infos: List[dict]) -> List[dict]:
    """The command collection(s), vendor pages first, then the audio pages."""
    mine = [d for d in infos if d.get("product_id") == PID]
    vendor = [d for d in mine if (d.get("usage_page") or 0) >= 0xFF00]
    other = [d for d in mine if d not in vendor]
    return vendor + other


class BlackSharkV3XProvider(Provider):
    name = "blackshark_v3x"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[int] = None

    def _ask(self, path, diag: List[str]) -> Optional[int]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            diag.append(f"    open: {e}")
            return None
        try:
            sent = dev.send_feature_report(make_query())
            if sent is not None and sent < 0:
                diag.append(f"    feature write refused ({sent})")
                return None
            r = dev.get_feature_report(REPORT_ID, REPORT_LEN)
            if r:
                diag.append(f"    reply: {hexdump(r, 12)}")
            return parse_reply(r)
        except (OSError, IOError, ValueError) as e:
            diag.append(f"    error: {e}")
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
            log.warning("hid.enumerate(blackshark_v3x): %s", e)
            return []
        if not any(d.get("product_id") == PID for d in infos):
            return []
        self._diag.append(f"[BlackShark V3 X] pid={PID:04x} '{NAME}'")
        for d in candidates(infos):
            self._diag.append(f"  trying iface={d.get('interface_number')} "
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
            level = self._ask(d["path"], self._diag)
            if level is not None:
                self._last = level
                self._diag.append(f"  -> {level}%")
                return [DeviceStatus(KEY, NAME, level, False, True, self.name,
                                     kind="headset", charging_known=False)]
        self._diag.append("  no battery reply (wrong channel, headset off or asleep)")
        if self._last is not None:
            return [DeviceStatus(KEY, NAME, self._last, False, False, self.name,
                                 kind="headset", charging_known=False)]
        return [DeviceStatus(KEY, NAME, None, False, False, self.name,
                             kind="headset", charging_known=False)]

    def diagnostics(self) -> List[str]:
        return list(self._diag)
