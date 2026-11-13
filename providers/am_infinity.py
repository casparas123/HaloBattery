"""Angry Miao AM Infinity 8K on its 2.4 GHz receiver/basetta (3151:5007).

The receiver mirrors the mouse's charge into a feature report once it receives
the vendor's status poll, so the read is a two-step sequence on the usage page
0xFFFF / usage 0x02 control collection:

    request   feature report id 0x00:  00 F7 00 ... (zero payload)
    wait      50 ms for the 2.4G round-trip
    reply     feature report 0xF7:     F7 00 64 ...  (main charge in byte 2,
                                        the dock's spare cell in byte 10)

That layout is the one the device-specific Linux driver documents -
TsFreddie/powermiao8k (GPL-2.0): SET_FEATURE report 0 with command 0xF7, sleep
50 ms, GET_FEATURE report 0xF7 (65 bytes), main at byte 2, spare at byte 10,
values above 100 treated as unknown. The same driver notes the wired id
(3151:402A) exposes no battery over HID.

The AJAZZ AJ159 APEX shares this USB id and answers a different report, 0x05
(`05 00 00 64 ...`, charge at byte 3 on Windows, byte 2 without the report id),
as documented by the AJAZZ Control Center project (Aiacos/ajazz-control-center,
GPL-3.0). That report is kept as a fallback, so the provider works for either
firmware behind the shared id. A junk byte where zero padding belongs is
refused, and a zero main charge means the 2.4G link is not up yet - never a
wrong 0 %. Neither report carries a charging flag, so the charger is reported
as unknown rather than as "not charging".

Both the 65-byte poll used by the reference apps and the 67-byte one their
hardware probes try are sent. Nothing else is ever written to the device.

**Unverified** on the AM Infinity itself: the 0xF7 exchange is documented by
the device-specific driver and the 0x05 one confirmed by its reference project
on the AJAZZ sibling, but the receiver of #72 still has to answer once before
either is confirmed on this model.
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

VID = 0x3151
PID = 0x5007
NAME = "AM Infinity 8K Mouse"

CONTROL_USAGE_PAGE = 0xFFFF      # the control collection: usage page 0xFFFF ...
CONTROL_USAGE = 0x02             # ... and usage 0x02 (interface 2 / MI_02 in the reference)

STATUS_POLL_OPCODE = 0xF7        # the vendor's status poll, zero payload
AM_REPORT_ID = 0xF7              # the AM Infinity's battery report (powermiao8k)
AJAZZ_REPORT_ID = 0x05           # the AJAZZ AJ159 APEX's report, same USB id
POLL_LENGTHS = (65, 67)          # the app sends 65; the hardware probe tries both
SETTLE_S = 0.05                  # powermiao8k waits 50 ms for the 2.4G round-trip
SPARE_DISTANCE = 8               # spare cell: main index + 8 in the 0xF7 report
ASLEEP_KEEP = 300                # s, as in the other receiver providers


def control_collection(ifaces: List[dict]) -> Optional[dict]:
    """The one collection the status poll may go to: usage page 0xFFFF, usage 0x02."""
    for d in ifaces:
        if d.get("usage_page") == CONTROL_USAGE_PAGE and d.get("usage") == CONTROL_USAGE:
            return d
    return None


def parse_am_report(frame) -> Optional[Tuple[int, Optional[int]]]:
    """(main, spare) from the AM Infinity's 0xF7 report, or None.

    Windows hidapi keeps the requested report id at index 0 (`F7 00 64 ...`,
    main at byte 2); a read without the id prefix has it at byte 1. The byte
    between the id and the main charge must be zero - a non-zero there is junk
    a wireless reconnect can return and is refused. A main charge of 0 means
    the link is not up yet; above 100 is the driver's "unknown". The spare
    cell is optional: 0 there means the dock is empty.
    """
    if not frame or len(frame) < 3:
        return None
    base = 2 if frame[0] == AM_REPORT_ID else 1
    if frame[base - 1] != 0:
        return None
    if len(frame) <= base:
        return None
    main = frame[base]
    if not 1 <= main <= 100:
        return None
    spare: Optional[int] = None
    if len(frame) > base + SPARE_DISTANCE:
        candidate = frame[base + SPARE_DISTANCE]
        if 1 <= candidate <= 100:
            spare = candidate
    return main, spare


def parse_ajazz(frame) -> Optional[int]:
    """Charge percent from the AJAZZ sibling's 0x05 report, or None.

    Windows hidapi keeps the requested report id at index 0 (`05 00 00 64 ...`,
    charge at byte 3); an unnumbered Linux read has no id prefix (charge at
    byte 2). The bytes between the id and the charge are zero padding - a
    non-zero there is junk a wireless reconnect can return and is refused. A
    charge of 0 means the link is not up yet.
    """
    if not frame or len(frame) < 4:
        return None
    charge_index = 3 if frame[0] == AJAZZ_REPORT_ID else 2
    for i in range(1, charge_index):
        if frame[i] != 0:
            return None
    charge = frame[charge_index]
    if charge == 0:
        return None
    return min(charge, 100)


class AmInfinityProvider(Provider):
    name = "am_infinity"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, float]] = None    # (charge, when)

    def _read(self, path: bytes) -> Optional[Tuple[int, Optional[int]]]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            for length in POLL_LENGTHS:
                poll = [0x00, STATUS_POLL_OPCODE] + [0x00] * (length - 2)
                try:
                    dev.send_feature_report(poll)
                except (OSError, ValueError) as e:
                    self._diag.append(f"    send (len={length}): {e}")
                    continue
                time.sleep(SETTLE_S)

                try:
                    resp = dev.get_feature_report(AM_REPORT_ID, 65)
                except (OSError, ValueError):
                    resp = None
                if resp:
                    self._diag.append(f"    {AM_REPORT_ID:02x} reply: {hexdump(resp, 12)}")
                parsed = parse_am_report(resp)
                if parsed is not None:
                    return parsed

                try:
                    resp5 = dev.get_feature_report(AJAZZ_REPORT_ID, 65)
                except (OSError, ValueError):
                    resp5 = None
                if resp5:
                    self._diag.append(f"    {AJAZZ_REPORT_ID:02x} reply: {hexdump(resp5, 12)}")
                charge = parse_ajazz(resp5)
                if charge is not None:
                    self._diag.append("    (answered the AJAZZ sibling's 0x05 layout)")
                    return charge, None

            self._diag.append("    no charge in 0xf7 or 0x05 (mouse off or asleep, "
                              "or the 2.4G link is not up)")
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
            log.warning("hid.enumerate(am_infinity): %s", e)
            return []
        ifaces = [d for d in infos if d.get("product_id") == PID]
        if not ifaces:
            return []
        self._diag.append(f"[AM Infinity] pid={PID:04x} "
                          f"'{(ifaces[0].get('product_string') or '').strip()}'")

        d = control_collection(ifaces)
        if d is None:
            self._diag.append(f"  no {CONTROL_USAGE_PAGE:04x}:{CONTROL_USAGE:04x} "
                              f"collection: nothing sent")
        else:
            self._diag.append(f"  iface={d.get('interface_number')} usage="
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
            charge = self._read(d["path"])
            if charge is not None:
                level, spare = charge
                if spare is not None:
                    self._diag.append(f"    spare cell on the dock: {spare}%")
                self._last = (level, time.time())
                return [DeviceStatus("am_infinity", NAME, level, False, True,
                                     "am_infinity", kind="mouse",
                                     charging_known=False)]

        # Silent receiver: it cannot tell a sleeping mouse from a switched-off
        # one, so the last value stays (greyed out) for a while, then the icon
        # is hidden and comes back with the next reading.
        if self._last and time.time() - self._last[1] < ASLEEP_KEEP:
            return [DeviceStatus("am_infinity", NAME, self._last[0], False, False,
                                 "am_infinity", kind="mouse", charging_known=False)]
        return []

    def diagnostics(self) -> List[str]:
        return list(self._diag)
