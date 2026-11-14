"""Razer Barracuda X (2022) with the YS-Tech bridge (1532:0550, #264).

The 0x0550 variant is not the Macronix 0x0552 bridge: its vendor report id is
0x02 and every RACE command is wrapped in

    02 80 <len> <crcLo> <crcHi> 50 41 <group> <seq> <body...>

with CRC-16/XMODEM over the whole 64-byte report, and the dongle acknowledges
with an input report `02 'O'` (or `02 'F'` while it is not ready).  This is
mirrored from mehdibouchami/razer-barracuda-x-battery (`Protocol.cs`, MIT),
which credits KhromotozzDevOut/razer-barracuda-battery-tray for the framing.

The headset reports voltage only on 2.4 GHz; the percentage is interpolated on
a Li-ion curve and charging is inferred from the spread of several samples,
exactly as the reference does.  Read-only: remote mode, GET_BATTERY samples,
local mode.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

try:
    import hid  # type: ignore
except Exception:  # pragma: no cover
    hid = None

VID = 0x1532
PID = 0x0550
NAME = "Razer Barracuda X (2022)"
KEY = f"barracuda-x:{PID:04x}"

VENDOR_PAGE = 0xFF00
REPORT_SIZE = 64
REPORT_ID = 0x02
ACK_READY = ord("O")
ACK_BUSY = ord("F")

RACE_MAGIC = b"PI"                # 0x50 0x49 in replies
HEADER_LEN = 10
REMOTE_MODE = (0x0E, (0x02, 0xE1, 0x01))
LOCAL_MODE = (0x0E, (0x02, 0xE1, 0x00))
GET_BATTERY = (0x06, (0x01, 0x00, 0x31))

SAMPLES = 7
SAMPLE_GAP_S = 0.35
CHARGE_JITTER_MV = 12
CHARGE_VOLTAGE_MV = 4180
MV_MIN, MV_MAX = 2500, 4500
CURVE = (3300, 3680, 3750, 3790, 3830, 3870, 3910, 3960, 4020, 4080, 4150)
READ_TIMEOUT_MS = 800
ASLEEP_KEEP = 300

_sequence = 0


def next_sequence() -> int:
    global _sequence
    _sequence = _sequence % 127 + 1
    return _sequence


def crc16_xmodem(data) -> int:
    crc = 0
    for b in data:
        crc ^= (b << 8) & 0xFFFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def make_frame(race, length: int = REPORT_SIZE) -> bytes:
    frame = bytearray(length)
    frame[0] = REPORT_ID
    frame[1] = 0x80
    frame[2] = len(race)
    frame[5:5 + len(race)] = bytes(race)
    crc = crc16_xmodem(frame)
    frame[3] = crc & 0xFF
    frame[4] = (crc >> 8) & 0xFF
    return bytes(frame)


def race_request(group: int, seq: int, body) -> List[int]:
    return [0x50, 0x41, group, seq] + list(body)


def find_frame(report, offset: int, length: int, group: int, seq: int):
    end = min(offset + length, len(report))
    pos = offset
    while pos + HEADER_LEN <= end:
        if report[pos] != RACE_MAGIC[0] or report[pos + 1] != RACE_MAGIC[1]:
            return None
        payload_len = report[pos + 8] | (report[pos + 9] << 8)
        frame_end = pos + HEADER_LEN + payload_len
        if frame_end > end:
            return None
        payload = list(report[pos + HEADER_LEN:frame_end])
        if len(payload) >= 2 and payload[0] == group and payload[1] == (0x80 | seq):
            return payload
        pos = frame_end
    return None


def battery_mv(payload) -> Optional[int]:
    if not payload or len(payload) < 5 or payload[0] != GET_BATTERY[0]:
        return None
    if payload[2] != 0x00:
        return None
    mv = payload[3] | (payload[4] << 8)
    return mv if MV_MIN <= mv <= MV_MAX else None


def to_percent(mv: int) -> int:
    if mv <= CURVE[0]:
        return 0
    for i in range(1, len(CURVE)):
        if mv < CURVE[i]:
            return (i - 1) * 10 + (mv - CURVE[i - 1]) * 10 // (CURVE[i] - CURVE[i - 1])
    return 100


def is_charging(samples) -> bool:
    return bool(samples) and (max(samples) - min(samples) >= CHARGE_JITTER_MV
                             or min(samples) >= CHARGE_VOLTAGE_MV)


def pick(infos: List[dict]) -> Optional[dict]:
    vendor = [d for d in infos if d.get("usage_page") == VENDOR_PAGE
              and (d.get("output_report_byte_length") or 0) > 0
              and (d.get("input_report_byte_length") or 0) > 0]
    if vendor:
        vendor.sort(key=lambda d: 0 if (
            d.get("input_report_byte_length") == REPORT_SIZE
            and d.get("output_report_byte_length") == REPORT_SIZE) else 1)
        return vendor[0]
    return next((d for d in infos if d.get("usage_page") == VENDOR_PAGE), None)


class BarracudaX0550Provider(Provider):
    name = "barracuda_x_0550"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, float]] = None

    def _race(self, dev, group: int, body) -> Optional[List[int]]:
        seq = next_sequence()
        frame = make_frame(race_request(group, seq, body))
        # The dongle may answer 'F' (not ready) a few times before 'O'.
        accepted = False
        deadline = time.time() + READ_TIMEOUT_MS / 1000.0
        for attempt in range(5):
            if attempt:
                time.sleep(0.05)
            sent = dev.write(frame)
            if sent is not None and sent < 0:
                self._diag.append(f"  write refused ({sent})")
                return None
            end = time.time() + READ_TIMEOUT_MS / 1000.0
            while time.time() < end:
                r = dev.read(REPORT_SIZE, 100)
                if not r:
                    continue
                if r[0] != REPORT_ID or len(r) < 2:
                    continue
                if r[1] == ACK_READY:
                    accepted = True
                    break
                if r[1] != ACK_BUSY:
                    return None
            if accepted:
                break
        if not accepted:
            self._diag.append("  dongle never acknowledged the frame")
            return None

        deadline = time.time() + READ_TIMEOUT_MS / 1000.0
        while time.time() < deadline:
            r = dev.read(REPORT_SIZE, 100)
            if not r or r[0] != REPORT_ID or len(r) < 2:
                continue
            length = r[1]
            if length > len(r) - 2:
                continue
            payload = find_frame(r, 2, length, group, seq)
            if payload is not None:
                return payload
        return None

    def _ask(self, d: dict) -> Optional[Tuple[int, bool]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(d["path"])
        except Exception as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            remote = self._race(dev, *REMOTE_MODE)
            if remote is None or len(remote) < 3 or remote[2] != 0x00:
                self._diag.append("  remote mode refused")
                return None
            samples = []
            try:
                for i in range(SAMPLES):
                    if i:
                        time.sleep(SAMPLE_GAP_S)
                    reply = self._race(dev, *GET_BATTERY)
                    mv = battery_mv(reply)
                    if mv is not None:
                        samples.append(mv)
            finally:
                self._race(dev, *LOCAL_MODE)
            if not samples:
                self._diag.append("  no voltage in any sample")
                return None
            mv = min(samples)
            charging = is_charging(samples)
            self._diag.append(f"  {mv} mV (samples {min(samples)}..{max(samples)}), "
                              f"{to_percent(mv)}%{' charging' if charging else ''}")
            return to_percent(mv), charging
        except Exception as e:  # pragma: no cover
            self._diag.append(f"  error: {e}")
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
            log.warning("hid.enumerate(barracuda x 0550): %s", e)
            return []
        mine = [d for d in infos if d.get("product_id") == PID]
        if not mine:
            return []
        d = pick(mine)
        if d is None:
            return []
        self._diag.append(f"[Barracuda X 0550] pid={PID:04x} '{NAME}' "
                          f"iface={d.get('interface_number')} "
                          f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
        parsed = self._ask(d)
        if parsed is not None:
            level, charging = parsed
            self._last = (level, time.time())
            self._diag.append(f"  -> {level}%{' charging' if charging else ''}")
            return [DeviceStatus(KEY, NAME, level, charging, True, self.name,
                                 kind="headset")]
        self._diag.append("  no answer (headset off, out of range, or the dongle refused)")
        if self._last and time.time() - self._last[1] < ASLEEP_KEEP:
            return [DeviceStatus(KEY, NAME, self._last[0], False, False, self.name,
                                 kind="headset")]
        return [DeviceStatus(KEY, NAME, None, False, False, self.name, kind="headset")]

    def diagnostics(self) -> List[str]:
        return list(self._diag)
