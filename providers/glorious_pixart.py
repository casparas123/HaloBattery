"""Glorious Model D2 Wireless over its Pixart receiver (#281).

The D2 Wireless is not the Sinowealth Model O: it enumerates as Pixart's
`093a:824d` with a 64-byte vendor feature collection (`ff00:0001`) and an input
collection (`0001:0002`).  GloriousBatteryMonitor-Go's `HidDeviceService.cs`
documents the matching path as `Pixart CandidateD`: the collection rejects
SetFeature, but a plain GetFeature populates a reply that contains the battery
level.  That project scans report ids 0x00..0x0f and bytes 1..10, rejecting
answers with only one or two non-zero data bytes because those are firmware
status, not a battery frame.

The same project detects charging from the pid change (`824d` wireless ->
`824a` on the cable), not from a charge bit, so the cable's presence is used
here too.  The wired id is also tried when it is the only one present.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log
from .gwolves import feature_length

try:
    import hid  # type: ignore
except Exception:  # pragma: no cover - only when the package is missing
    hid = None

VID = 0x093A
PID_RECEIVER = 0x824D
PID_CABLE = 0x824A
PIDS = (PID_RECEIVER, PID_CABLE)
NAME = "Glorious Model D2 Wireless"
KEY = "glorious:d2"

REPORT_IDS = range(0x10)
LEVEL_FIRST = 1
LEVEL_LAST = 10
ASLEEP_KEEP = 300


def parse_feature(r) -> Optional[Tuple[int, bool]]:
    """(level, charging) from a CandidateD feature reply, or None.

    A reply with one or two non-zero bytes is a firmware status, not a battery
    frame (GloriousBatteryMonitor-Go rejects those).  The charge byte is only a
    hint; the caller also treats the cable's pid as charging.
    """
    if not r or len(r) < 3:
        return None
    data = list(r)
    if sum(1 for b in data[1:] if b) <= 2:
        return None
    for i in range(LEVEL_FIRST, min(LEVEL_LAST + 1, len(data))):
        level = data[i]
        if 1 <= level <= 100:
            charging = ((i > 0 and data[i - 1] == 0x01)
                        or (i + 1 < len(data) and data[i + 1] == 0x01))
            return level, charging
    return None


def pick(infos: List[dict]) -> List[dict]:
    """Vendor collections that can carry a feature report, longest first."""
    vendor = [d for d in infos if (d.get("usage_page") or 0) >= 0xFF00]
    with_len = [d for d in vendor if (feature_length(d["path"]) or 0) >= 8]
    return with_len + [d for d in vendor if d not in with_len]


class GloriousD2Provider(Provider):
    name = "glorious_pixart"

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
            log.warning("hid.enumerate(glorious pixart): %s", e)
            return []
        mine = [d for d in infos if d.get("product_id") in PIDS]
        if not mine:
            return []
        self._diag.append(f"[Glorious D2] pid(s)="
                          f"{', '.join(f'{d.get('product_id', 0):04x}' for d in mine)}")
        receiver = [d for d in mine if d.get("product_id") == PID_RECEIVER]
        cable = [d for d in mine if d.get("product_id") == PID_CABLE]
        if cable:
            self._diag.append(f"  on the cable: {PID_CABLE:04x}")

        for group, plugged in ((receiver, False), (cable, True)):
            for d in pick(group):
                length = feature_length(d["path"]) or 65
                self._diag.append(f"  trying pid={d.get('product_id', 0):04x} "
                                  f"iface={d.get('interface_number')} "
                                  f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x} "
                                  f"feature={length}")
                parsed = self._ask(d["path"], length)
                if parsed is not None:
                    level, charge_bit = parsed
                    charging = charge_bit or plugged or bool(cable)
                    self._last = (level, time.time())
                    self._diag.append(f"  -> {level}%{' charging' if charging else ''}")
                    return [DeviceStatus(KEY, NAME, level, charging, True,
                                         "glorious", kind="mouse")]

        self._diag.append("  no battery answer (asleep, off, or not this collection)")
        # On the cable the mouse may stop answering the receiver but is still
        # present and charging; keep the last level if there is one.
        if cable:
            level = self._last[0] if self._last else None
            return [DeviceStatus(KEY, NAME, level, True, True, "glorious", kind="mouse")]
        if self._last and time.time() - self._last[1] < ASLEEP_KEEP:
            return [DeviceStatus(KEY, NAME, self._last[0], False, False,
                                 "glorious", kind="mouse")]
        return [DeviceStatus(KEY, NAME, None, False, False, "glorious", kind="mouse")]

    def _ask(self, path, length: int) -> Optional[Tuple[int, bool]]:
        """Read feature reports without writing anything to the mouse."""
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            for rid in REPORT_IDS:
                try:
                    r = dev.get_feature_report(rid, length)
                except (OSError, IOError, ValueError) as e:
                    self._diag.append(f"    rid={rid:#04x}: {e}")
                    continue
                if r:
                    self._diag.append(f"    rid={rid:#04x}: {hexdump(r, 12)}")
                parsed = parse_feature(r)
                if parsed is not None:
                    return parsed
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return None

    def diagnostics(self) -> List[str]:
        return list(self._diag)
