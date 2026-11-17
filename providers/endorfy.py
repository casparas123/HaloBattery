"""ENDORFY LIV Plus Wireless through its 2.4 GHz receiver (3299:00a7) or
USB cable (3299:00a6).

The mouse is a Compx ODM: both identities publish the shared `ff02:0002`
control collection and answer the family's 17-byte report-0x08 battery query
(level byte 6, cable/charging flag byte 7, millivolts bytes 8-9).  The
protocol is implemented by `providers/compx.py`; this module only owns the
vendor ids, names and the one-icon policy.

The receiver is tried first.  When the mouse is on its cable the receiver
goes quiet and the cable identity answers instead; both identities therefore
share the `endorfy:livplus` icon.  No setting, lighting or firmware command is
sent.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

try:
    import hid  # type: ignore
except Exception:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log
from .compx import (CONTROL_USAGE, PAYLOAD_LEN, READ_ATTEMPTS, READ_TIMEOUT_MS,
                    make_request, output_length, parse_power, voltage_mv)

VID = 0x3299
PIDS: Dict[int, str] = {
    0x00A7: "ENDORFY LIV Plus Wireless",
    0x00A6: "ENDORFY LIV Plus Wireless",
}
NAME = "ENDORFY LIV Plus Wireless"
KEY = "endorfy:livplus"
ASLEEP_KEEP = 300


def pick(infos: List[dict]) -> Optional[dict]:
    """The ff02:0002 collection that can carry the 17-byte frame, else a fallback."""
    control = [d for d in infos
               if (d.get("usage_page"), d.get("usage")) == CONTROL_USAGE]
    for d in control + infos:
        if output_length(d["path"]) in (None, PAYLOAD_LEN):
            return d
    return control[0] if control else (infos[0] if infos else None)


class EndorfyProvider(Provider):
    name = "endorfy"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, bool, float]] = None

    def _query(self, path: bytes) -> Optional[Tuple[int, bool, Optional[int]]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            # drain events left by the receiver before asking
            for _ in range(4):
                if not dev.read(PAYLOAD_LEN, 30):
                    break
            sent = dev.write(make_request())
            if sent is not None and sent < 0:
                self._diag.append(f"    write refused ({sent})")
                return None
            for attempt in range(READ_ATTEMPTS):
                r = dev.read(PAYLOAD_LEN, READ_TIMEOUT_MS)
                if not r:
                    continue
                self._diag.append(f"    reply {attempt + 1}: {hexdump(r)}")
                parsed = parse_power(r)
                if parsed is not None:
                    return parsed[0], parsed[1], voltage_mv(r)
            self._diag.append("    no power reply")
            return None
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"    error: {e}")
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
            log.warning("hid.enumerate(endorfy): %s", e)
            return []
        present = [d for d in infos if d.get("product_id") in PIDS]
        if not present:
            return []
        self._diag.append("[ENDORFY] " + ", ".join(
            f"pid={d.get('product_id', 0):04x} if={d.get('interface_number')}"
            for d in present[:6]))
        for pid in (0x00A7, 0x00A6):             # receiver first, cable second
            mine = [d for d in infos if d.get("product_id") == pid]
            if not mine:
                continue
            d = pick(mine)
            if d is None:
                self._diag.append(f"  pid={pid:04x}: no collection")
                continue
            self._diag.append(f"  pid={pid:04x} iface={d.get('interface_number')} "
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
            parsed = self._query(d["path"])
            if parsed is None:
                continue
            level, charging, mv = parsed
            if mv is not None:
                self._diag.append(f"  {mv} mV")
            self._last = (level, charging, time.time())
            self._diag.append(f"  -> {level}%{' charging' if charging else ''}")
            return [DeviceStatus(KEY, NAME, level, charging, True, self.name,
                                 kind="mouse")]
        self._diag.append("  no battery answer (asleep, off, or on a cable)")
        if self._last:
            level, charging, seen = self._last
            if time.time() - seen < ASLEEP_KEEP:
                return [DeviceStatus(KEY, NAME, level, charging, False, self.name,
                                     kind="mouse", wake_on_input=True)]
        return [DeviceStatus(KEY, NAME, None, False, False, self.name, kind="mouse")]

    def diagnostics(self) -> List[str]:
        return list(self._diag)
