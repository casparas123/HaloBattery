"""IO by Red Square Graphite 2 Pro Wireless headset on its 2.4 GHz dongle (4c4a:4155).

The dongle is a C-Media device (its USB strings read "C-Media Electronics Inc." /
"Graphite 2 Pro Wireless") and exposes HID interfaces in the same shape as the
MCHOSE X9's dongle, which ``providers/mchose.py`` already reads: the vendor
collection ``ff90:0001`` carries a 64-byte input report 0x55 and a 64-byte output
report 0x55, and a battery status read is one output report answered on the same
input report:

    write   55 65 01 00 ...                     (report id 0x55 + 63 data bytes)
    answer  55 65 <level> <state> ...            30 ms on the X9 (55 65 28 02)

``level`` is the percentage, ``state`` is 2 discharging, 3 charging, 4 full and 26
asleep (no level shown). The frame is M HUB's ``audio.blk.readBattery`` and the
decoder is its ``parseBatteryResponse``, both recovered in
``providers/mchose.py``; the sister-family community driver
(github.com/JoaoKSS/MCHOSE_v9_PRO_Controller) sends the same ``65 01`` frame. It is
a status read only - nothing is configured, written to flash or tuned.

What is NOT proven here is that this headset's firmware answers it. The reporter
of #537 (a thorough report of the dongle's descriptors) found every collection
silent when only listening, and feature report 0xAA on ``ff22:0001`` read back an
error, which is what a collection with no feature reports does. No vendor app
shows a level, so there is nothing to compare a first answer with.

Two collections are tried, in the order the reporter's own enumeration shows
them: ``ff90:0001`` first (the X9 answers there), then ``ff82:0001`` (the other
64/64 collection). A collection that cannot carry report 0x55 refuses the write
at once and is skipped. Reports other than 0x55, a state outside the four above,
a level above 100 and a reply whose state byte is missing are refused rather than
turned into a reading. Sometimes the dongle enumerates without ff90/ff82 at all -
the reporter saw both cases - and then the dongle is present but the headset is
not linked, which is reported as such and nothing is sent.

Charging: state 3 is decoded as charging because that is what the byte means for
this family, but it has never been seen on this model, and a sleeping headset
reports 26 with no level. A reading from state 2 therefore leaves the charging
state unknown rather than asserting "not charging" (``charging_known=False``).

Only the dongle's id 4c4a:4155 is claimed, and only while its product string says
"Graphite 2 Pro Wireless": the vendor id belongs to JieLi's USB stack and the
dongle reports C-Media strings, so the product string is what pins the model.
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

VID = 0x4C4A
PID = 0x4155
NAME = "IO Graphite 2 Pro"
KEY = "graphite2:4155"
PRODUCT = "graphite 2 pro wireless"

# Both collections are 64-byte input / 64-byte output with report 0x55. ff90 is
# where the X9 answers; ff82 is the other one the reporter's enumeration showed.
COLLECTIONS = ((0xFF90, 0x0001), (0xFF82, 0x0001))
REPORT = 0x55
PAYLOAD_LEN = 63                       # data bytes behind the report id
REQUEST = bytes([0x65, 0x01]).ljust(PAYLOAD_LEN, b"\0")

REPLY_ID = 0x65                        # first data byte of a status answer
LEVEL_BYTE = 2                         # counted from the report id, as mchose.py does
STATE_BYTE = 3
DISCHARGING, CHARGING, FULL, ASLEEP = 2, 3, 4, 26
VALID_STATES = (DISCHARGING, CHARGING, FULL)

READ_MS = 400
ATTEMPTS = 2
ASLEEP_KEEP = 300                      # s a level survives while the headset sleeps

Reading = Tuple[Optional[int], bool, bool]   # level, charging, charging known


def is_graphite(info: dict) -> bool:
    """The Graphite 2 Pro dongle: the id plus the product string it publishes."""
    product = info.get("product_string")
    pid = info.get("product_id")
    return (info.get("vendor_id") == VID and type(pid) is int and pid == PID
            and isinstance(product, str) and product.strip().lower() == PRODUCT)


def candidate(infos, page: int, usage: int):
    """The first enumerated collection on this page/usage; None when it is absent."""
    for d in infos:
        if (not is_graphite(d)
                or ((d.get("usage_page") or 0), (d.get("usage") or 0)) != (page, usage)):
            continue
        return d
    return None


def parse_status(resp) -> Optional[Reading]:
    """(level, charging, charging_known) from a report 0x55 status answer, or None.

    Asleep (state 26) is a real answer with no level: the headset is off or out of
    range, so the caller keeps its last reading. Everything malformed is refused.
    """
    if not resp or len(resp) <= STATE_BYTE:
        return None
    data = bytes(resp)
    if data[0] != REPORT or data[1] != REPLY_ID:
        return None
    level, state = data[LEVEL_BYTE], data[STATE_BYTE]
    if state == ASLEEP:
        return None, False, False
    if state not in VALID_STATES or level > 100:
        return None
    return level, state == CHARGING, state == CHARGING


class GraphiteProvider(Provider):
    name = "graphite2"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, bool, float, bool]] = None

    def _read(self, path) -> Optional[Reading]:
        """One status read on one collection; None when it does not answer."""
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            for attempt in range(ATTEMPTS):
                try:
                    wrote = dev.write(bytes([REPORT]) + REQUEST)
                except (OSError, ValueError) as e:
                    self._diag.append(f"    write: {e}")
                    return None
                if wrote is None or wrote < 0:
                    self._diag.append(f"    write refused ({wrote}): this collection "
                                      "cannot carry report 0x55")
                    return None
                try:
                    resp = dev.read(64, READ_MS)
                except (OSError, ValueError) as e:
                    self._diag.append(f"    read: {e}")
                    return None
                if resp:
                    parsed = parse_status(resp)
                    if parsed is not None:
                        self._diag.append(f"    answered: {hexdump(resp, 12)}")
                        return parsed
                    self._diag.append(f"    {hexdump(resp, 12)} (not a status reply)")
                elif attempt + 1 < ATTEMPTS:
                    self._diag.append("    nothing, asking again")
            self._diag.append("    no reply")
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
            log.warning("hid.enumerate(graphite 2 pro): %s", e)
            return []
        if not any(is_graphite(d) for d in infos):
            return []
        self._diag.append("[Graphite 2 Pro] dongle present; asking for the headset's status")
        parsed = None
        for page, usage in COLLECTIONS:
            d = candidate(infos, page, usage)
            if d is None:
                self._diag.append(f"  {page:04x}:{usage:04x} not enumerated "
                                  "(the headset is not linked to the dongle)")
                continue
            self._diag.append(f"  {page:04x}:{usage:04x} iface={d.get('interface_number')}")
            parsed = self._read(d["path"])
            if parsed is not None:
                break
        if parsed is not None:
            level, charging, charging_known = parsed
            if level is not None:
                self._last = (level, charging, time.time(), charging_known)
                return [DeviceStatus(KEY, NAME, level, charging, True, self.name,
                                     kind="headset", charging_known=charging_known)]
            # asleep/off: a real answer with no level, so the last one stays greyed out
            self._diag.append("  the headset is asleep or out of range")
        if self._last and time.time() - self._last[2] < ASLEEP_KEEP:
            level, charging, _seen, _known = self._last
            return [DeviceStatus(KEY, NAME, level, charging, False, self.name,
                                 kind="headset", charging_known=False)]
        # the dongle is there but the headset is off, out of range, or never answered:
        # the icon stays so the user can see the dongle is alive, without a number
        return [DeviceStatus(KEY, NAME, None, False, False, self.name,
                             kind="headset", charging_known=False)]

    def diagnostics(self) -> List[str]:
        return list(self._diag)
