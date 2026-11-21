"""AULA F87 Pro keyboard on its Compx 2.4 GHz receiver (3554:FA09, #360).

The same receiver id covers the AULA F75, which answers the Compx 17-byte
`08 04` power frame that providers/pulsar.py reads. The F87 Pro does not: it
answers the Sinowealth-style 20-byte `13 4A` frame, the same command set the
MCHOSE K87 uses, tunneled through the Compx receiver.

Reference: Radiance-Philippines/aula-f87-control, `docs/PROTOCOL.md` and
`aula_f87/device.py` (`DongleLink.read_battery`):

    request  13 4A 00 00 00 ... checksum   (checksum = sum(bytes 0..18) & 0xFF)
    reply    13 4A 01 00 02 <level> <state> ... checksum

`level` is 1-100 on battery. `state` is `0x01` on battery and `0x10` with the
cable plugged in; while the cable is in the keyboard reports 100 regardless, so
the level is reported as unknown and the device as charging. Any other state is
refused rather than guessed.

Only the 2.4 GHz receiver is read; the wired interface has no battery report.
"""
from __future__ import annotations

import ctypes
import sys
from typing import Dict, List, Optional, Tuple

VID = 0x3554
PID = 0xFA09
REPORT_LEN = 20
CMD = 0x4A
NAME = "AULA F87 Pro"

STATE_BATTERY = 0x01
STATE_CABLE = 0x10


def make_request() -> bytes:
    """`13 4A 00 ...`, with the checksum over the first 19 bytes."""
    frame = bytearray(REPORT_LEN)
    frame[0] = 0x13
    frame[1] = CMD
    frame[REPORT_LEN - 1] = sum(frame[:REPORT_LEN - 1]) & 0xFF
    return bytes(frame)


def _checksum_ok(d: List[int]) -> bool:
    return (sum(d[:REPORT_LEN - 1]) & 0xFF) == d[REPORT_LEN - 1]


def parse_reply(data) -> Optional[Tuple[Optional[int], bool, bool]]:
    """(level, charging, charging_known) from a `13 4A 01` answer, or None."""
    d = list(data or [])
    if len(d) < REPORT_LEN:
        return None
    if d[0] != 0x13 or (d[1] & 0x7F) != CMD:
        return None
    if (d[2], d[3], d[4]) != (0x01, 0x00, 0x02):
        return None
    if not _checksum_ok(d):
        return None
    level, state = d[5], d[6]
    if state == STATE_CABLE:
        return None, True, True
    if state == STATE_BATTERY and 1 <= level <= 100:
        return level, False, True
    return None


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


_CAPS: Dict[bytes, Optional[Tuple[int, int]]] = {}


def report_lengths(path) -> Optional[Tuple[int, int]]:
    """(input, output) report byte lengths of one collection, or None.

    Read-only HidP_GetCaps, the same probe providers/compx.py uses for the output
    length, cached per path (the paths change when the receiver is re-plugged).

    Why this exists: Windows refuses a write that is larger than a collection's
    output report, and the app's HID transport records any refused native call as
    a failed poll for the whole provider - even when the provider catches the
    error. Testing a collection that cannot carry the frame therefore greys every
    icon that provider owns (#360, the reporter's charging run: the AULA reader
    probed the wrong collection, the transport failed the poll, and the keyboard
    fell back to "last known").
    """
    if path in _CAPS:
        return _CAPS[path]
    value: Optional[Tuple[int, int]] = None
    if sys.platform == "win32":
        try:
            p = path.decode("utf-8", "ignore") if isinstance(path, (bytes, bytearray)) else str(path)
            k32, hidd = ctypes.windll.kernel32, ctypes.windll.hid
            k32.CreateFileW.restype = ctypes.c_void_p
            handle = k32.CreateFileW(p, 0, 3, None, 3, 0, None)
            if handle not in (None, ctypes.c_void_p(-1).value):
                try:
                    pp = ctypes.c_void_p()
                    if hidd.HidD_GetPreparsedData(ctypes.c_void_p(handle), ctypes.byref(pp)):
                        try:
                            caps = _HIDP_CAPS()
                            if hidd.HidP_GetCaps(pp, ctypes.byref(caps)) == 0x00110000:
                                value = (caps.InputReportByteLength,
                                         caps.OutputReportByteLength)
                        finally:
                            hidd.HidD_FreePreparsedData(pp)
                finally:
                    k32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            value = None
    _CAPS[path] = value
    return value


def can_carry(path, length: int = REPORT_LEN) -> Optional[bool]:
    """True/False from the collection's caps; None when Windows cannot say."""
    sizes = report_lengths(path)
    if sizes is None:
        return None
    return sizes[0] >= length and sizes[1] >= length


def candidates(infos: List[dict]) -> List[dict]:
    """Vendor collections of this receiver, `ff02:0002` first."""
    mine = [d for d in infos
            if d.get("product_id") == PID and (d.get("usage_page") or 0) >= 0xFF00]
    return sorted(mine, key=lambda d: (0 if (d.get("usage_page"), d.get("usage")) == (0xFF02, 0x0002)
                                       else 1, d.get("interface_number") or 99))
