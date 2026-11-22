"""Turtle Beach "Stellar Wireless" headsets over their 2.4 GHz receivers.

The ROCCAT Syn Max Air (``1e7d:3a3f``) and the Turtle Beach Stealth 700 Gen 2
(``10f5:218c``) share one receiver design: interface 0 exposes a vendor
collection ``ff00:0001`` with 64-byte unnumbered input/output reports. The
headset's battery is read by writing one 64-byte request and matching the
tagged reply:

    request  00 8A 07 06 01 00 ...            (64 bytes, no report id)
    reply    8A 83 06 02 LL 00 V1 V1 V2 V2 CC
             LL    coarse level 0..5 (6 while charging)
             V1V1  cell voltage, little-endian mV (20 mV steps)
             V2V2  second, filtered voltage
             CC    01 while charging / on the dock

The receiver also pushes unsolicited ``E0``-``E6`` event reports (mic arm,
wheels); they are ignored and the read waits for the ``8A`` tag. No reply within
1.5 s means the headset is off or asleep while the receiver stays enumerated.

Source: protocol captured from ROCCAT Swarm (USBPcap) and documented by
@BrooklynBanana in HaloBattery #206, with a reference implementation at
github.com/ferloom/Wireless-Device-Battery-Monitor (GPL-3.0). The frames and the
voltage curve are facts measured on hardware; no code is copied. The Syn Max Air
curve is that unit's measured discharge (16 h under load); the Stealth 700 Gen 2
is a different battery, so it shows the receiver's coarse level instead of a
voltage-derived percentage until an owner confirms the curve.

Only the read-only status request is sent; no settings are written.
Unverified on both models until their owners run a test build.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump

VENDOR_USAGE = (0xFF00, 0x0001)
QUERY = bytes([0x00, 0x8A, 0x07, 0x06, 0x01]).ljust(64, b"\0")
TAG = 0x8A
REPLY_KIND = (0x8A, 0x83, 0x06, 0x02)
READ_MS = 200
READS = 8
ASLEEP_KEEP = 300

# Voltage (mV) -> percent, measured on the Syn Max Air under load.
DISCHARGE = (
    (4080, 100), (4040, 97), (4000, 92), (3960, 86), (3920, 78), (3900, 74),
    (3880, 70), (3840, 66), (3820, 63), (3800, 60), (3780, 53), (3760, 45),
    (3740, 34), (3720, 26), (3700, 20), (3680, 15), (3660, 11), (3640, 7),
    (3600, 3), (3500, 0),
)

Reading = Tuple[Optional[int], bool, bool, str]   # level, charging, known, approx


@dataclass(frozen=True)
class Source:
    vid: int
    pid: int
    name: str
    key: str
    products: tuple
    curve: bool


SOURCES = (
    Source(0x1E7D, 0x3A3F, "ROCCAT Syn Max Air", "roccat:3a3f", ("syn max air",), True),
    Source(0x10F5, 0x218C, "Turtle Beach Stealth 700 Gen 2", "turtlebeach:stealth700g2",
           ("stealth 700 g2", "stealth 700 gen 2"), False),
)


def percent_from_mv(mv: int) -> int:
    """Piecewise-linear discharge curve; clamped at both ends."""
    if mv >= DISCHARGE[0][0]:
        return DISCHARGE[0][1]
    if mv <= DISCHARGE[-1][0]:
        return DISCHARGE[-1][1]
    for (v1, p1), (v2, p2) in zip(DISCHARGE, DISCHARGE[1:]):
        if v2 <= mv <= v1:
            return p2 + (mv - v2) * (p1 - p2) // (v1 - v2)
    return 0


def decode(resp, source: Source) -> Optional[Reading]:
    """Decode the 8A 83 06 02 status reply, or None for another frame."""
    if not isinstance(resp, (bytes, bytearray, list, tuple)) or len(resp) < 11:
        return None
    data = [int(b) for b in resp]
    if tuple(data[:4]) != REPLY_KIND:
        return None
    coarse = data[4]
    mv = data[6] | (data[7] << 8)
    charging = data[10] == 0x01
    if charging:
        if mv >= 4180:
            return 100, True, True, ""
        if coarse <= 5:
            pct = coarse * 20
            return pct, True, True, f"about {pct}% (coarse, charging)"
        return None, True, True, "charging (level unavailable)"
    if source.curve and mv:
        return percent_from_mv(mv), False, True, ""
    pct = min(coarse, 5) * 20
    return pct, False, True, f"about {pct}% (coarse)"


def matches(info: dict, source: Source) -> bool:
    return (info.get("vendor_id") == source.vid and info.get("product_id") == source.pid
            and isinstance(info.get("product_string"), str)
            and any(p in info["product_string"].strip().lower() for p in source.products))


def candidate(infos, source: Source) -> Optional[dict]:
    for d in infos:
        if (matches(d, source) and isinstance(d.get("path"), (bytes, str)) and d["path"]
                and (d.get("usage_page"), d.get("usage")) == VENDOR_USAGE):
            return d
    return None


class StellarProvider(Provider):
    name = "stellar"

    def __init__(self):
        self._diag: List[str] = []
        self._last = {}      # key -> (level, charging, seen, known, approx)

    def _query_source(self, path, source: Source) -> Optional[Reading]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            metadata = getattr(dev, "get_report_lengths", None)
            if metadata is not None:
                caps = metadata() if callable(metadata) else None
                if (not isinstance(caps, dict)
                        or caps.get("output") != 64 or caps.get("input") != 64):
                    self._diag.append(f"  incompatible report lengths: {caps}")
                    return None
            for _ in range(4):
                if not dev.read(64, 20):
                    break
            sent = dev.write(QUERY)
            if type(sent) is not int or sent != 64:
                self._diag.append(f"  status request write refused or incomplete ({sent})")
                return None
            for attempt in range(READS):
                reply = dev.read(64, READ_MS)
                if not reply:
                    continue
                self._diag.append(f"  frame {attempt + 1}: {hexdump(reply, 16)}")
                parsed = decode(reply, source)
                if parsed is not None:
                    return parsed
            return None
        except Exception as e:  # a broken collection must not fail the provider
            self._diag.append(f"  query error: {e}")
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
        out: List[DeviceStatus] = []
        for source in SOURCES:
            try:
                infos = hidlist.enumerate(source.vid)
            except Exception as e:  # pragma: no cover - one vendor must not hide the other
                self._diag.append(f"  enumerate {source.vid:04x}: {e}")
                continue
            d = candidate(infos, source)
            if d is None:
                if self._last.get(source.key):
                    level, charging, seen, known, approx = self._last[source.key]
                    if time.time() - seen < ASLEEP_KEEP:
                        out.append(DeviceStatus(source.key, source.name, level, charging, False,
                                                self.name, approx=approx, kind="headset",
                                                charging_known=known))
                continue
            self._diag.append(f"[Stellar] {source.name} pid={source.pid:04x} "
                              f"iface={d.get('interface_number')} "
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
            parsed = self._query_source(d["path"], source)
            if parsed is None:
                self._diag.append("  no status reply (headset off or asleep)")
                if self._last.get(source.key):
                    level, charging, seen, known, approx = self._last[source.key]
                    if time.time() - seen < ASLEEP_KEEP:
                        out.append(DeviceStatus(source.key, source.name, level, charging, False,
                                                self.name, approx=approx, kind="headset",
                                                charging_known=known))
                else:
                    out.append(DeviceStatus(source.key, source.name, None, False, False,
                                            self.name, kind="headset", charging_known=False))
                continue
            level, charging, known, approx = parsed
            self._last[source.key] = (level, charging, time.time(), known, approx)
            self._diag.append(f"  -> {level if level is not None else '?'}%"
                              f"{' charging' if charging else ''}")
            out.append(DeviceStatus(source.key, source.name, level, charging, True,
                                    self.name, approx=approx, kind="headset",
                                    charging_known=known))
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
