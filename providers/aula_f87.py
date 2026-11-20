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

from typing import List, Optional, Tuple

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


def candidates(infos: List[dict]) -> List[dict]:
    """Vendor collections of this receiver, `ff02:0002` first."""
    mine = [d for d in infos
            if d.get("product_id") == PID and (d.get("usage_page") or 0) >= 0xFF00]
    return sorted(mine, key=lambda d: (0 if (d.get("usage_page"), d.get("usage")) == (0xFF02, 0x0002)
                                       else 1, d.get("interface_number") or 99))
