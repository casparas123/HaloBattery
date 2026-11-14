"""MCHOSE G98 V3 wireless keyboard (3837:1015) over its 2.4 GHz receiver.

The keyboard's QHW exchange is read-only and independent of the MCHOSE mouse
protocol: output report 0x4D, command 0x0904, and an input report carrying
``charge level``.  The protocol facts were recovered from M HUB's own code by
the maintainers of ``wushuai0802/myfinder-device-battery`` (AGPL-3.0; their
implementation is not copied here) and are documented in their
``engine/research/batch2-protocols.md`` and ``upstream-protocols.md``.

Only the exact product string, collection and report id are used.  Both HID
report capacities are checked with HidP_GetCaps before any I/O, because the
official SDK pads the 8-byte request to the descriptor's output size and the
reply length is descriptor-dependent.  Unknown or mismatched collections are
left alone rather than guessed.

Request payload:  ``01 03 00 04 09 00 <seq> <0E ^ seq>``
Reply payload:    ``01 03 02 04 09 00 <seq> <charge> <level> <check>``
                  charge 0 discharging, 1 charging, 2 full (not charging).
The reply check byte's algorithm is not established, so it is not validated
rather than rejected by a guessed XOR.
"""
from __future__ import annotations

import ctypes
import sys
import time
from typing import List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

VID = 0x3837
PID = 0x1015
PRODUCT = "MCHOSE G98 V3 2.4G"
PAGE, USAGE = 0xFF80, 0x0001
REPORT_ID = 0x4D
TIMEOUT = 1.0
HEADER = (1, 3, 2, 4, 9, 0)


class _HIDP_CAPS(ctypes.Structure):
    """The report-length fields of HIDP_CAPS (Windows)."""

    _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
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
                ("NumberFeatureDataIndices", ctypes.c_ushort)]


def report_lengths(path) -> Optional[Tuple[int, int]]:
    """(input, output) report byte lengths, including the report-id byte."""
    if sys.platform != "win32":
        return None
    try:
        p = path.decode("utf-8", "ignore") if isinstance(path, (bytes, bytearray)) else str(path)
        k32, hidd = ctypes.windll.kernel32, ctypes.windll.hid
        k32.CreateFileW.restype = ctypes.c_void_p
        handle = k32.CreateFileW(p, 0, 3, None, 3, 0, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            return None
        try:
            pp = ctypes.c_void_p()
            if not hidd.HidD_GetPreparsedData(ctypes.c_void_p(handle), ctypes.byref(pp)):
                return None
            try:
                caps = _HIDP_CAPS()
                if hidd.HidP_GetCaps(pp, ctypes.byref(caps)) != 0x00110000:
                    return None
                return caps.InputReportByteLength, caps.OutputReportByteLength
            finally:
                hidd.HidD_FreePreparsedData(pp)
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return None


def make_request(sequence: int, output_len: int) -> List[int]:
    """The request padded to the descriptor's output length (id included)."""
    if not 0 <= sequence <= 255:
        raise ValueError("invalid sequence")
    payload = [1, 3, 0, 4, 9, 0, sequence, 0x0E ^ sequence]
    frame = [REPORT_ID] + payload
    if output_len < len(frame):
        raise ValueError("output report too short")
    return frame + [0x00] * (output_len - len(frame))


def parse_reply(raw, sequence: int) -> Optional[Tuple[int, bool]]:
    """(level, charging) from a QHW reply, or None if it is not one."""
    data = list(raw or [])
    if not data:
        return None
    if data[0] == REPORT_ID:
        data = data[1:]
    if len(data) < 10 or tuple(data[:6]) != HEADER or data[6] != sequence:
        return None
    if data[2] != 2:                      # declared data length: charge + level
        return None
    charge, level = data[7], data[8]
    if charge not in (0, 1, 2) or not 0 <= level <= 100:
        return None
    return level, charge == 1


class MchoseQhwProvider(Provider):
    name = "mchose_qhw"

    def __init__(self):
        self._diag: List[str] = []
        self._sequence = 0

    def _read(self, info: dict) -> Optional[Tuple[int, bool]]:
        lengths = report_lengths(info["path"])
        if lengths is None:
            self._diag.append("    report lengths unavailable; not opening")
            return None
        input_len, output_len = lengths
        if not (10 <= input_len <= 4096 and 9 <= output_len <= 4096):
            self._diag.append(f"    report lengths input={input_len} output={output_len}; refused")
            return None
        dev = hid.device()
        try:
            dev.open_path(info["path"])
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            for _ in range(2):
                sequence = self._sequence
                self._sequence = (sequence + 1) & 0xFF
                try:
                    frame = make_request(sequence, output_len)
                    sent = dev.write(frame)
                except (OSError, ValueError) as e:
                    self._diag.append(f"    write: {e}")
                    return None
                if sent is not None and sent < 0:
                    self._diag.append(f"    write refused ({sent})")
                    return None
                deadline = time.monotonic() + TIMEOUT
                while time.monotonic() < deadline:
                    try:
                        raw = bytes(dev.read(input_len, max(1, int((deadline - time.monotonic()) * 1000))))
                    except (OSError, ValueError) as e:
                        self._diag.append(f"    read: {e}")
                        return None
                    got = parse_reply(raw, sequence)
                    if got is not None:
                        self._diag.append(f"    answer: {hexdump(raw, 12)} -> {got[0]}% "
                                          f"charging={got[1]}")
                        return got
            self._diag.append("    no reply (keyboard off or asleep)")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:  # pragma: no cover
            return []
        try:
            infos = hidlist.enumerate(VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(mchose_qhw): %s", e)
            return []
        out: List[DeviceStatus] = []
        for d in infos:
            if d.get("product_id") != PID:
                continue
            if (d.get("usage_page"), d.get("usage")) != (PAGE, USAGE):
                continue
            name = (d.get("product_string") or "").strip()
            if name != PRODUCT:
                self._diag.append(f"[MCHOSE G98] product {name!r} is not the G98 V3; skipped")
                continue
            self._diag.append(f"[MCHOSE G98] pid={PID:04x} '{name}' iface={d.get('interface_number')}")
            got = self._read(d)
            if got is not None:
                level, charging = got
                out.append(DeviceStatus("mchose_qhw:1015", name, level, charging, True,
                                        "mchose_qhw", kind="keyboard"))
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
