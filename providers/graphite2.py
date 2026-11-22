"""IO by Red Square Graphite 2 Pro Wireless (4c4a:4155 dongle, 0d8c:0379 wired).

Test 2: the headset's battery lives on one of the C-Media vendor collections
``ff90:0001`` / ``ff82:0001``, which the dongle only creates when it enumerates
with the headset already linked (reporter's finding, #537). Test 1 sent the
MCHOSE X9's status read (output report 0x55, ``65 01 ...``, answered
``55 65 <level> <state>``) and Windows refused the I/O with
ERROR_INVALID_FUNCTION - so the transport or the report id differs on this
firmware, and this build finds out which without guessing at a protocol:

1. It logs the collection's HID report lengths (input / output / feature) before
   touching it, plus the exact native error text when a call fails.
2. It tries the status read as output report 0x55, then as a feature read of
   report 0x55, then as output report 0x00 (an unnumbered 64-byte report).
   Each attempt is the same read-only status request; the first that answers
   wins. A collection that refuses every form is logged and skipped.
3. It reads the wired headset's own collection too (``0d8c:0379``, product
   "Graphite 2 Pro (Wired Mode)") before the dongle, since the wired device may
   expose the channel even when the dongle refuses it.

The frame is M HUB's ``audio.blk.readBattery`` as recovered for the MCHOSE X9 in
``providers/mchose.py`` (``65 01`` on report 0x55, level at data byte 1, state
at data byte 2: 2 discharging, 3 charging, 4 full, 26 asleep). The C-Media
family's siblings answer ``55 65 <level> <state>``; this firmware is the one
that has to confirm it.

Nothing is written to the headset's settings; every attempt is a status read.
Unverified on hardware.
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

# wired first: the headset on its cable is a separate USB device that may expose
# the same vendor collections even when the dongle refuses them.
VID, PID = 0x4C4A, 0x4155
WIRED_VID, WIRED_PID = 0x0D8C, 0x0379
COLLECTIONS = ((0xFF90, 0x0001), (0xFF82, 0x0001))

REPORT = 0x55
PAYLOAD_LEN = 63
REQUEST = bytes([0x65, 0x01]).ljust(PAYLOAD_LEN, b"\0")

REPLY_ID = 0x65
DISCHARGING, CHARGING, FULL, ASLEEP = 2, 3, 4, 26
VALID_STATES = (DISCHARGING, CHARGING, FULL)

READ_MS = 400
ASLEEP_KEEP = 300
NAME = "IO Graphite 2 Pro"
KEY = "graphite2:4155"

Reading = Tuple[Optional[int], bool, bool]   # level, charging, charging known


@dataclass
class Source:
    vid: int
    pid: int
    product: str
    label: str


SOURCES = (
    Source(WIRED_VID, WIRED_PID, "graphite 2 pro", "wired"),
    Source(VID, PID, "graphite 2 pro wireless", "receiver"),
)


def matches(info: dict, source: Source) -> bool:
    product = info.get("product_string")
    return (info.get("vendor_id") == source.vid and info.get("product_id") == source.pid
            and isinstance(product, str) and product.strip().lower().startswith(source.product))


def candidate(infos, source: Source, page: int, usage: int):
    for d in infos:
        if (matches(d, source)
                and ((d.get("usage_page") or 0), (d.get("usage") or 0)) == (page, usage)):
            return d
    return None


def parse_status(resp) -> Optional[Reading]:
    """(level, charging, charging known) from a report 0x55 answer, or None.

    Accepts a reply with the report id (``55 65 ...``), with a leading zero for
    an unnumbered report (``00 55 65 ...``) or without any id (``65 ...``).
    """
    if not resp:
        return None
    data = bytes(resp)
    if len(data) >= 4 and data[0] == REPORT and data[1] == REPLY_ID:
        level, state = data[2], data[3]
    elif len(data) >= 5 and data[0] == 0x00 and data[1] == REPORT and data[2] == REPLY_ID:
        level, state = data[3], data[4]
    elif len(data) >= 3 and data[0] == REPLY_ID:
        level, state = data[1], data[2]
    else:
        return None
    if state == ASLEEP:
        return None, False, False
    if state not in VALID_STATES or level > 100:
        return None
    return level, state == CHARGING, state == CHARGING


def _caps(dev) -> str:
    fn = getattr(dev, "get_report_lengths", None)
    if fn is None:
        return "caps: not exposed"
    try:
        c = fn()
        return f"caps in={c.get('input')} out={c.get('output')} feat={c.get('feature')}"
    except Exception as e:  # pragma: no cover - backend specific
        return f"caps: {e}"


def _error(dev) -> str:
    fn = getattr(dev, "error", None)
    try:
        return f" error={fn()!r}" if fn is not None else ""
    except Exception:  # pragma: no cover
        return ""


class GraphiteProvider(Provider):
    name = "graphite2"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, bool, float, bool]] = None

    def _read_frame(self, dev, prefix: str) -> Optional[Reading]:
        for _ in range(3):
            try:
                resp = dev.read(64, READ_MS)
            except (OSError, ValueError) as e:
                self._diag.append(f"{prefix} read: {e}")
                return None
            if not resp:
                continue
            self._diag.append(f"{prefix} reply: {hexdump(resp, 16)}")
            parsed = parse_status(resp)
            if parsed is not None:
                return parsed
            self._diag.append(f"{prefix} not a status reply")
        return None

    def _try_collection(self, path, prefix: str) -> Optional[Reading]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"{prefix} open: {e}")
            return None
        try:
            self._diag.append(f"{prefix} {_caps(dev)}")

            # 1) the MCHOSE X9 form: output report 0x55
            try:
                sent = dev.write(bytes([REPORT]) + REQUEST)
            except (OSError, ValueError) as e:
                sent = -1
                self._diag.append(f"{prefix} write 0x55: {e}")
            if sent is not None and sent > 0:
                got = self._read_frame(dev, f"{prefix} out0x55")
                if got is not None:
                    return got
            else:
                self._diag.append(f"{prefix} write 0x55 refused ({sent}){_error(dev)}")

            # 2) as a feature read of the same report id
            getter = getattr(dev, "get_feature_report", None)
            if getter is not None:
                try:
                    feat = getter(REPORT, 64)
                except (OSError, ValueError) as e:
                    self._diag.append(f"{prefix} feature 0x55: {e}")
                    feat = None
                if feat:
                    self._diag.append(f"{prefix} feature 0x55: {hexdump(feat, 16)}")
                    got = parse_status(feat)
                    if got is not None:
                        return got

            # 3) an unnumbered 64-byte report (id 0x00)
            try:
                sent = dev.write(bytes([0x00]) + REQUEST)
            except (OSError, ValueError) as e:
                sent = -1
                self._diag.append(f"{prefix} write 0x00: {e}")
            if sent is not None and sent > 0:
                got = self._read_frame(dev, f"{prefix} out0x00")
                if got is not None:
                    return got
            else:
                self._diag.append(f"{prefix} write 0x00 refused ({sent}){_error(dev)}")
            return None
        except Exception as e:  # a broken collection must not fail the provider
            self._diag.append(f"{prefix} unexpected: {e}")
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
        infos: List[dict] = []
        for vid in (VID, WIRED_VID):
            try:
                infos += list(hidlist.enumerate(vid))
            except Exception as e:  # one vendor failing must not hide the other
                self._diag.append(f"  enumerate {vid:04x}: {e}")
        present = [s for s in SOURCES if any(matches(d, s) for d in infos)]
        if not present:
            return []
        self._diag.append("[Graphite 2 Pro] sources: " + ", ".join(
            f"{s.label} ({s.vid:04x}:{s.pid:04x})" for s in present))

        parsed = None
        for source in present:
            for page, usage in COLLECTIONS:
                d = candidate(infos, source, page, usage)
                if d is None:
                    self._diag.append(f"  {source.label} {page:04x}:{usage:04x} absent")
                    continue
                prefix = (f"  {source.label} {page:04x}:{usage:04x} "
                          f"iface={d.get('interface_number')}")
                self._diag.append(prefix)
                parsed = self._try_collection(d["path"], prefix)
                if parsed is not None:
                    break
            if parsed is not None:
                break

        if parsed is not None:
            level, charging, charging_known = parsed
            if level is not None:
                self._last = (level, charging, time.time(), charging_known)
                return [DeviceStatus(KEY, NAME, level, charging, True, self.name,
                                     kind="headset", charging_known=charging_known)]
            self._diag.append("  the headset is asleep or out of range")
        if self._last and time.time() - self._last[2] < ASLEEP_KEEP:
            level, charging, _seen, _known = self._last
            return [DeviceStatus(KEY, NAME, level, charging, False, self.name,
                                 kind="headset", charging_known=False)]
        return [DeviceStatus(KEY, NAME, None, False, False, self.name,
                             kind="headset", charging_known=False)]

    def diagnostics(self) -> List[str]:
        return list(self._diag)
