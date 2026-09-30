"""Rapoo gaming mice over their 2.4 GHz receivers, without Rapoo's software.

The receiver pushes the battery by itself - there is nothing to ask and nothing
is ever written, so this provider only listens. Protocol from zbndev/rapoo_battery,
which reads these mice (and the VT3 family) on Linux, confirmed against a USBPcap
capture of the reporter's own receiver in #109 (24ae:185a, a VT9 Pro receiver):
a 7-byte report arrives about every 3 seconds:

    bb b0 42 20 03 01 64
    |  |  |  |  |  |  +-- byte 6: battery percent 0..100 (0x64 = 100 %)
    |  |  |  |  |  +----- byte 5: 0x00 transition (ignored), 0x01 discharging,
    |  |  |  |  |              0x02 charging
    |  |  |  |  +-------- byte 4: connection type, 0x03 = 2.4 GHz
    |  |  |  +----------- byte 3: polling/connection info
    |  |  +-------------- byte 2: mode flags (varies; 0x42 here)
    |  +----------------- byte 1: status flags, 0xB0
    +-------------------- byte 0: report id, 0xBB

The firmware refreshes the percentage only every 5-10 minutes, so identical
reports repeat in between; a 10-byte report (0xBC) appears during cable
plug/unplug transitions and carries no level. Anything that is not the exact
0xBB shape is logged raw and refused rather than shown as a level.

The capture shows no host reads at all while the pushes arrive, and which
collection on the receiver's vendor interface carries report 0xBB is not
visible in it (the Linux reference reads the whole interface), so the vendor
collections are listened on in turn and the one that answers is remembered.

Only 24ae:185a is claimed - the receiver the #109 capture came from. The
reference lists more ids for the family (VT3 Pro / Pro Max 1215/1244, VT9 Pro
Mini 3103), and the two public references disagree about 1417, so no other id
is claimed until one is tested on hardware.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

RAPOO_VID = 0x24AE
VT9_PRO_RECEIVER = 0x185A
PIDS = {VT9_PRO_RECEIVER: "Rapoo VT9 Pro"}

VENDOR_PAGE = 0xFF00
PREFERRED_USAGES = (0x0002, 0x000E)   # the receiver's vendor collections in the #109 dump
MOUSE_USAGE = (0x0001, 0x0002)        # never opened: the OS owns the pointer stream

REPORT_ID = 0xBB
REPORT_FLAGS = 0xB0
CHARGE_INDEX = 5
LEVEL_INDEX = 6
CHARGING = 0x02
DISCHARGING = 0x01

READ_ATTEMPTS = 16                    # ~4 s per collection: the push comes every ~3 s
READ_TIMEOUT_MS = 250
MAX_CANDIDATES = 2                    # the dump shows two vendor collections
ASLEEP_KEEP = 300                     # s, as in the other receiver providers

Reading = Tuple[int, bool]


def parse_report(r) -> Optional[Reading]:
    """(level, charging) from a 0xBB battery report, or None when it is not one."""
    if not r or len(r) <= LEVEL_INDEX:
        return None
    if r[0] != REPORT_ID or r[1] != REPORT_FLAGS:
        return None
    charge = r[CHARGE_INDEX]
    if charge not in (CHARGING, DISCHARGING):
        return None                   # a transition frame; a settled one follows within ~3 s
    level = r[LEVEL_INDEX]
    if not 0 <= level <= 100:
        return None
    return level, charge == CHARGING


def candidates(ifaces: List[dict]) -> List[dict]:
    """The collections to listen on, best first: vendor page, then the rest.

    Report 0xBB rides the receiver's vendor interface, but which of its
    collections Windows hands it out on is not known from the capture, so
    every collection is a candidate except the plain mouse one. The same
    usage listed twice (the dump has ff00:0002 again at the end) is one
    collection; the vendor usages are kept in a fixed order.
    """
    def rank(d: dict) -> tuple:
        page = d.get("usage_page", 0)
        usage = d.get("usage", 0)
        if page == VENDOR_PAGE and usage in PREFERRED_USAGES:
            return (0, PREFERRED_USAGES.index(usage))
        if page == VENDOR_PAGE:
            return (1, 0)
        return (2, 0)

    seen = set()
    out = []
    for d in ifaces:
        if (d.get("usage_page"), d.get("usage")) == MOUSE_USAGE:
            continue
        key = (d.get("usage_page"), d.get("usage"))
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return sorted(out, key=rank)


class RapooProvider(Provider):
    name = "rapoo"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, bool, float]] = None
        self._chosen: Optional[bytes] = None

    def _listen(self, path: bytes) -> Optional[Reading]:
        """Read what the receiver pushes; nothing is ever sent."""
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            for _ in range(READ_ATTEMPTS):
                r = dev.read(64, READ_TIMEOUT_MS)
                if not r:
                    continue              # quiet until the next ~3 s push
                reading = parse_report(r)
                self._diag.append(f"    report: {hexdump(r, 8)}"
                                  + ("" if reading else "  (not the 0xBB battery shape)"))
                if reading is not None:
                    return reading
                time.sleep(0.02)
            self._diag.append("    nothing in ~4 s (mouse off, or another collection)")
            return None
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"    read error: {e}")
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
            infos = hidlist.enumerate(RAPOO_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(rapoo): %s", e)
            return []
        out: List[DeviceStatus] = []
        for pid, name in PIDS.items():
            mine = [d for d in infos if d["product_id"] == pid]
            if not mine:
                continue
            key = f"rapoo:{pid:04x}"
            self._diag.append(f"[Rapoo] pid={pid:04x} '{name}' "
                              f"'{(mine[0].get('product_string') or '').strip()}'")
            order = candidates(mine)
            if self._chosen is not None:
                order = sorted(order, key=lambda d: d["path"] != self._chosen)
            got = None
            for d in order[:MAX_CANDIDATES]:
                self._diag.append(f"  listening on iface={d.get('interface_number')} "
                                  f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
                got = self._listen(d["path"])
                if got is not None:
                    self._chosen = d["path"]
                    break
            if got is not None:
                level, charging = got
                self._last = (level, charging, time.time())
                out.append(DeviceStatus(key, name, level, charging, True, "rapoo",
                                        kind="mouse"))
                continue
            if self._last and time.time() - self._last[2] < ASLEEP_KEEP:
                self._diag.append("  nothing heard; keeping the last level, greyed out")
                out.append(DeviceStatus(key, name, self._last[0], self._last[1], False,
                                        "rapoo", kind="mouse"))
            else:
                self._diag.append("  nothing heard yet and no earlier level")
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
