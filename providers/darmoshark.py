"""Darmoshark (MOJI) 2.4 GHz receiver battery read (#430).

The receiver is `1915:0723` and the wired mouse `1915:073c`. Both expose the
non-numbered 64-byte vendor report pair on usage page `0xff0a`, usage `0x0001`.
The frame layout and power offsets follow the OpenLogi Darmoshark driver:

    [0] operation type; bit 6 set for the 2.4 GHz receiver
    [1] packet index
    [2] command opcode 129 (BASE_INFO)
    [3] sub-command 1 (power)
    ... zero payload ...
    [63] checksum; the whole report sums to 0xa1 mod 256

The answer is a 64-byte input report with the same operation/sub-command after
masking bit 6. Two layouts exist: the legacy one carries the percentage at
byte 6; the v2 layout marks itself with `15 19` at bytes 6-7 and carries it at
byte 11. The charging state is not decoded, so the app reports the level with
no charging ring instead of guessing. Confirmed on hardware in #430: 63%,
matching the mouse.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

try:
    import hid  # type: ignore
except Exception:  # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

VID = 0x1915
RECEIVER_PID = 0x0723
WIRED_PID = 0x073C
PIDS = {RECEIVER_PID, WIRED_PID}
USAGE_PAGE = 0xFF0A
USAGE = 0x0001
REPORT_LEN = 64
CHECKSUM_TARGET = 0xA1
WIRELESS_FLAG = 0x40
TYPE_POWER = 0x01
OP_BASE_INFO = 129
SUB_POWER = 1
# A sleeping mouse can ignore the first request; the receiver acknowledges it
# (`1f 00 81 01`) and forwards nothing, so the query is repeated a few times
# within one poll instead of waiting a full poll interval. See #430.
READ_TIMEOUT_MS = 400
READ_ATTEMPTS = 3
QUERY_ROUNDS = 3
ROUND_GAP_S = 0.15
NAME = "Darmoshark N5"
KEY = "darmoshark:n5"


def checksum(frame) -> int:
    return (CHECKSUM_TARGET - (sum(frame[:REPORT_LEN - 1]) & 0xFF)) & 0xFF


def make_query(wireless: bool) -> bytes:
    frame = bytearray(REPORT_LEN)
    frame[0] = TYPE_POWER | (WIRELESS_FLAG if wireless else 0)
    frame[2] = OP_BASE_INFO
    frame[3] = SUB_POWER
    frame[REPORT_LEN - 1] = checksum(frame)
    return bytes(frame)


def parse_power(data) -> Optional[Tuple[int, bool, bool]]:
    """(level, charging, charging_known) from a power response, or None.

    The level sits at byte 6 (legacy) or byte 11 (v2, marked by `15 19` at
    bytes 6-7). The state byte is byte 5, or byte 10 on the v2 layout; the
    Darmoshark web app shows "Charging" for 1 and 3, "Fully charged" for 2 and
    the percentage for 0, so those four values are known and anything else is
    reported as unknown rather than guessed.
    """
    d = list(data or [])
    if len(d) < 12:
        return None
    if (d[0] & 0x3F) != TYPE_POWER or d[3] != SUB_POWER:
        return None
    v2 = d[6] == 0x15 and d[7] == 0x19
    level = d[11] if v2 else d[6]
    if not 0 <= level <= 100:
        return None
    state = d[10] if v2 else d[5]
    if v2 and state == 3:
        state = 0
    if state in (1, 3):
        return level, True, True
    if state in (0, 2):
        return level, False, True
    return level, False, False


def parse_reply(data) -> Optional[int]:
    """The level alone, for callers that only need the percentage."""
    parsed = parse_power(data)
    return None if parsed is None else parsed[0]


def candidates(infos: List[dict]) -> List[dict]:
    """The wired body (073C) first, then the receiver; only the documented vendor collection."""
    mine = [d for d in infos
            if d.get("product_id") in PIDS
            and (d.get("usage_page"), d.get("usage")) == (USAGE_PAGE, USAGE)]
    return sorted(mine, key=lambda d: (0 if d.get("product_id") == WIRED_PID else 1,
                                       d.get("interface_number") or 99))


class DarmosharkProvider(Provider):
    name = "darmoshark"

    def __init__(self):
        self._diag: List[str] = []

    def _read(self, d: dict) -> Optional[Tuple[int, bool, bool]]:
        wireless = d.get("product_id") == RECEIVER_PID
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(d["path"])
            for round_ in range(1, QUERY_ROUNDS + 1):
                sent = dev.write(make_query(wireless))
                if sent is None or sent < 0:
                    self._diag.append(f"  write refused ({sent})")
                    return None
                for attempt in range(READ_ATTEMPTS):
                    r = dev.read(REPORT_LEN, READ_TIMEOUT_MS)
                    if r:
                        self._diag.append(f"  query {round_} reply {attempt + 1} "
                                          f"({len(r)} B): {hexdump(r, 14)}")
                    parsed = parse_power(r)
                    if parsed is not None:
                        return parsed
                if round_ < QUERY_ROUNDS:
                    time.sleep(ROUND_GAP_S)
            self._diag.append(f"  no power reply after {QUERY_ROUNDS} queries")
            return None
        except (OSError, IOError, ValueError) as e:
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
            log.warning("hid.enumerate(darmoshark): %s", e)
            return []
        found = candidates(infos)
        if not found:
            return []
        for d in found:
            self._diag.append(f"[Darmoshark] pid={d['product_id']:04x} "
                              f"iface={d.get('interface_number')} "
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
            parsed = self._read(d)
            if parsed is not None:
                level, charging, charging_known = parsed
                name = NAME if d["product_id"] == RECEIVER_PID else NAME + " (USB cable)"
                self._diag.append(f"  -> {level}% charging={charging} "
                                  f"known={charging_known}")
                return [DeviceStatus(KEY, name, level, charging, True, self.name,
                                     kind="mouse", charging_known=charging_known)]
        return []

    def diagnostics(self) -> List[str]:
        return list(self._diag)
