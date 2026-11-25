"""ROCCAT Vulcan II Mini Air on its 2.4 GHz receiver (#429).

The receiver is `1e7d:3aa9` (Turtle Beach-branded units report `1e7d:501e`) and it
exposes a vendor collection on usage page `0xFF00`, usage `0x0001`: unnumbered
64-byte input reports, plus a 63-byte feature report with id 7.

The exchange is Turtle Beach Swarm's own, from the reporter's USB capture:

    SET_REPORT feature id 7, 64 bytes:   07 04 88 00 01 00 ...
    input report, same collection:       07 04 88 00 41 <level>

A frame is `07 <payload length> <command> <sub-command> <args>`. `0x41` marks a
one-byte answer, and the answer here is a plain percentage. Replies are matched on
command and sub-command, never on "the next report": the same stream carries
`07 04 33 01 41 7f`-style setting answers (DPI, brightness, polling) whose values
also sit in 0-100 and would otherwise read as a level.

The keyboard also pushes `09 0a 88 05 <value> ... <byte 8>` every ten seconds. The
two captures differ in byte 8 (25-26 on battery, 36-38 on the cable), but it also
rises by one every ten seconds while on battery, so it is recorded as a second
candidate and not decoded yet. Charging is therefore reported as unknown until
the two candidates are told apart on hardware.
"""
from __future__ import annotations

from typing import List, Optional

try:
    import hid  # type: ignore
except Exception:  # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

VID = 0x1E7D
RECEIVER_PIDS = (0x3AA9, 0x501E)
USAGE_PAGE = 0xFF00
USAGE = 0x0001
REPORT_LEN = 64
FEATURE_ID = 0x07
PAYLOAD_LEN = 0x04
CMD_BATTERY = 0x88
SUB_LEVEL = 0x00
SUB_PUSH = 0x05
MARKER_ONE_BYTE = 0x41
PUSH_TYPE = 0x09
READ_TIMEOUT_MS = 300
READ_ATTEMPTS = 6
NAME = "ROCCAT Vulcan II Mini Air"
KEY = "roccat:vulcan-mini-air"


def make_query() -> bytes:
    """Swarm's percentage query: feature report id 7, then `04 88 00 01`."""
    frame = bytearray(REPORT_LEN)
    frame[0] = FEATURE_ID
    frame[1] = PAYLOAD_LEN
    frame[2] = CMD_BATTERY
    frame[3] = SUB_LEVEL
    frame[4] = 0x01
    return bytes(frame)


def parse_reply(data) -> Optional[int]:
    """The percentage from a `07 04 88 00 41 <level>` answer, or None."""
    d = list(data or [])
    if len(d) < 6:
        return None
    if d[0] != 0x07 or d[1] < 4:
        return None
    if d[2] != CMD_BATTERY or d[3] != SUB_LEVEL or d[4] != MARKER_ONE_BYTE:
        return None
    level = d[5]
    return level if 0 <= level <= 100 else None


def parse_push(data) -> Optional[int]:
    """Candidate B: byte 8 of the `09 0a 88 05 ...` status push, or None."""
    d = list(data or [])
    if len(d) < 9 or d[0] != PUSH_TYPE or d[2] != CMD_BATTERY or d[3] != SUB_PUSH:
        return None
    return d[8]


def candidates(infos: List[dict]) -> List[dict]:
    """The vendor collection of the receiver, lowest interface first."""
    mine = [d for d in infos
            if d.get("product_id") in RECEIVER_PIDS
            and (d.get("usage_page"), d.get("usage")) == (USAGE_PAGE, USAGE)]
    return sorted(mine, key=lambda d: d.get("interface_number") or 99)


class RoccatProvider(Provider):
    name = "roccat"

    def __init__(self):
        self._diag: List[str] = []

    def _read(self, d: dict) -> Optional[int]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(d["path"])
            sent = dev.send_feature_report(make_query())
            if sent is None or sent < 0:
                self._diag.append(f"  feature write refused ({sent})")
                return None
            for attempt in range(READ_ATTEMPTS):
                r = dev.read(REPORT_LEN, READ_TIMEOUT_MS)
                if not r:
                    continue
                self._diag.append(f"  report {attempt + 1} ({len(r)} B): {hexdump(r, 12)}")
                pushed = parse_push(r)
                if pushed is not None:
                    self._diag.append(f"  candidate B (push byte 8): {pushed}")
                level = parse_reply(r)
                if level is not None:
                    return level
            self._diag.append("  no level reply")
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
            log.warning("hid.enumerate(roccat): %s", e)
            return []
        found = candidates(infos)
        if not found:
            return []
        for d in found:
            self._diag.append(f"[ROCCAT] pid={d['product_id']:04x} "
                              f"iface={d.get('interface_number')} "
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
            level = self._read(d)
            if level is not None:
                self._diag.append(f"  -> candidate A (0x88 reply): {level}%")
                return [DeviceStatus(KEY, NAME, level, False, True, self.name,
                                     kind="keyboard", charging_known=False)]
        return []

    def diagnostics(self) -> List[str]:
        return list(self._diag)
