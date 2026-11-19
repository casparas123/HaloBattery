"""Glorious Model D2 Wireless over its Pixart receiver (#281).

The D2 Wireless is a split-interface Pixart receiver (`093a:824d`): the
feature collection `ff00:0001` on interface 1 accepts a GetFeature *trigger*,
but the answer arrives as an input report on the sibling collection
`ff00:ff00` (8 bytes).  This is GloriousBatteryMonitor-Go's `CandidateF`
probe; its `CandidateD` GetFeature-only path is deliberately skipped on split
interfaces because col01 returns firmware-status bytes rather than a battery.

    trigger   GetFeature(report id 0x03) on ff00:0001, then 0x00/0x01/0x02
    reply     on ff00:ff00: 06 FB <level> ...

The level is `reply[2]` for the normal frame; the reference also accepts a
plausible level in bytes 1..10 for other firmware.  Charging is the wired
`824a` identity (the same PID change GloriousBatteryMonitor-Go uses); there is
no charge bit in the reply, and byte 3 = 0x01 in discharging captures, so it is
not treated as one.

The first trigger (report id 0x03) can take several seconds on the receiver;
later polls try the same id first and use a shorter timeout.  When the wired
pid and the receiver are both present, the wired collection is tried first so
a receiver that still answers cannot hide the charging state.  Nothing is
written to the device.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

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

PAGE = 0xFF00
TRIGGER_USAGE = 0x0001
INPUT_USAGE = 0xFF00
TRIGGER_LEN = 64
INPUT_LEN = 8
# (report id, timeout in seconds); 0x03 is the one the receiver processes in
# about five seconds.  The rest are the reference's fallbacks.
PROBES = ((0x03, 6.0), (0x00, 2.0), (0x01, 2.0), (0x02, 2.0))
ASLEEP_KEEP = 300
PASSIVE_TIMEOUT = 4.0                # s, before the slower GetFeature trigger


def parse_reading(r) -> Optional[int]:
    """The level from a col05 input report, or None."""
    if not r:
        return None
    b = list(r)
    if len(b) >= 3 and b[0] == 0x06 and b[1] == 0xFB:
        if 1 <= b[2] <= 100:
            return b[2]
        return None
    # Other firmware: the reference scans bytes 1..10 for a plausible level.
    for i in range(1, min(11, len(b))):
        if 1 <= b[i] <= 100:
            return b[i]
    return None


def collections(infos: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    """The trigger and sibling input collections of the D2 receiver."""
    trigger = None
    inp = None
    for d in infos:
        if d.get("product_id") not in PIDS:
            continue
        page, usage = d.get("usage_page"), d.get("usage")
        if (page, usage) == (PAGE, TRIGGER_USAGE):
            trigger = trigger or d
        elif (page, usage) == (PAGE, INPUT_USAGE):
            inp = inp or d
    return trigger, inp


class GloriousD2Provider(Provider):
    name = "glorious_pixart"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, float]] = None
        self._working_rid: Optional[int] = None

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
        self._diag.append("[Glorious D2] pid(s)="
                          + ", ".join(f"{d.get('product_id', 0):04x}" for d in mine))
        receiver = [d for d in mine if d.get("product_id") == PID_RECEIVER]
        cable = [d for d in mine if d.get("product_id") == PID_CABLE]
        if cable:
            self._diag.append(f"  on the cable: {PID_CABLE:04x}")

        # The cable is the charging signal and wins when both ids are present:
        # the receiver can still answer while the mouse is plugged in, and must
        # not hide the cable state. A cable-only reading falls back to the
        # receiver only when the wired collection has no answer.
        for group, plugged in ((cable, True), (receiver, False)):
            trigger, inp = collections(group)
            if trigger is None or inp is None:
                if group:
                    self._diag.append("  no trigger/input collection pair")
                continue
            self._diag.append(f"  candidate F: trigger iface={trigger.get('interface_number')} "
                              f"{PAGE:04x}:{TRIGGER_USAGE:04x}; input iface={inp.get('interface_number')} "
                              f"{PAGE:04x}:{INPUT_USAGE:04x}")
            # Some D2 firmware pushes the 06 FB frame on col05 without a
            # trigger; the reference tries that passive read first. It also
            # leaves a receiver in a game alone when the feature channel is
            # briefly unavailable, instead of forcing a failed GetFeature.
            passive = self._read_passive(inp["path"])
            if passive is not None:
                self._last = (passive, time.time())
                self._diag.append(f"  -> {passive}% (pushed)")
                return [DeviceStatus(KEY, NAME, passive, bool(cable), True,
                                     "glorious", kind="mouse")]
            level = self._read_cross(trigger["path"], inp["path"])
            if level is not None:
                charging = plugged or bool(cable)
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
                                 "glorious", kind="mouse", wake_on_input=True)]
        return [DeviceStatus(KEY, NAME, None, False, False, "glorious", kind="mouse",
                             wake_on_input=True)]

    def _read_passive(self, input_path) -> Optional[int]:
        """Read a battery frame the receiver may push without a trigger."""
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(input_path)
        except (OSError, IOError) as e:
            self._diag.append(f"    passive input open: {e}")
            return None
        try:
            reply = dev.read(INPUT_LEN, int(PASSIVE_TIMEOUT * 1000))
            if reply:
                self._diag.append(f"    passive reply: {hexdump(reply, 12)}")
                return parse_reading(reply)
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"    passive read: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return None

    def _read_cross(self, trigger_path, input_path) -> Optional[int]:
        """GetFeature trigger on col01, read the reply on col05, no writes."""
        in_dev = hid.device()  # type: ignore[union-attr]
        try:
            in_dev.open_path(input_path)
        except (OSError, IOError) as e:
            self._diag.append(f"    input open: {e}")
            return None
        probes = list(PROBES)
        if self._working_rid is not None:
            probes = [(self._working_rid, 2.0)] + [p for p in probes if p[0] != self._working_rid]
        try:
            for rid, timeout in probes:
                trigger = hid.device()  # type: ignore[union-attr]
                try:
                    trigger.open_path(trigger_path)
                except (OSError, IOError) as e:
                    self._diag.append(f"    trigger open: {e}")
                    return None
                try:
                    try:
                        trigger.get_feature_report(rid, TRIGGER_LEN)
                        self._diag.append(f"    trigger rid={rid:#04x}: accepted")
                    except (OSError, IOError, ValueError) as e:
                        # The receiver can process the request and still fail
                        # the feature read; the answer is on col05.
                        self._diag.append(f"    trigger rid={rid:#04x}: {e}")
                finally:
                    try:
                        trigger.close()
                    except Exception:
                        pass
                try:
                    reply = in_dev.read(INPUT_LEN, int(timeout * 1000))
                except (OSError, IOError, ValueError) as e:
                    self._diag.append(f"    rid={rid:#04x} input read: {e}")
                    continue
                if not reply:
                    continue
                self._diag.append(f"    rid={rid:#04x} reply: {hexdump(reply, 12)}")
                level = parse_reading(reply)
                if level is not None:
                    self._working_rid = rid
                    return level
            return None
        finally:
            try:
                in_dev.close()
            except Exception:
                pass

    def diagnostics(self) -> List[str]:
        return list(self._diag)
