"""Royal Kludge S85 over its SINOWEALTH 2.4 GHz receiver (258a:0150).

The receiver exposes the keyboard's control channel as ``ff02:0002`` and answers
the CompX keyboard power query documented by
Rainbow132/2.4G-wireless-device-battery-level-acquisition:

    request  13 05 01 00 ... 00 19       (20 bytes, report id 0x13)
    reply    13 0a ... <level> <linked>  (level at byte 6, linked at byte 7)

The first reply to the query is a short acknowledgment; the status frame with
the level arrives after it, so the receiver is polled for a short window rather
than reading once.  Only the read-only power query is sent.

The receiver does not expose a charging query.  The level is shown without a
charging state instead of guessing one; a cable check is part of the test.

A sleeping keyboard gets the same ``13 0a`` shape back with the link byte 0
and a **placeholder** in the level byte: the reporter's unit answered level 1
and 0 while the keyboard was at 85 % (#494 test build 2).  Only a frame whose
link byte is non-zero carries a level; the offline frames are logged and
ignored.

While the keyboard is on its USB cable it enumerates as ``258a:0193``, answers
the same query and is shown charging.  A keyboard that stays in 2.4 GHz mode
while a cable charges it cannot be shown as charging: its reply has no
charging field (see docs/protocols.md).
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

VID = 0x258A
PID = 0x0150                 # 2.4 GHz receiver
CABLE_PID = 0x0193           # the keyboard on its USB cable
PIDS = (PID, CABLE_PID)
NAME = "Royal Kludge S85"
KEY = "rk:s85"
CONTROL_USAGE = (0xFF02, 0x0002)
REQUEST = [0x13, 0x05, 0x01] + [0x00] * 16 + [0x19]
REPORT_ID = 0x13
STATUS = 0x0A
LEVEL_INDEX = 6
LINK_INDEX = 7
PAYLOAD_LEN = 20
READ_TIMEOUT_MS = 150
WINDOW_S = 2.0
ASLEEP_KEEP = 300


def is_s85(info: dict) -> bool:
    return (info.get("product_id") in PIDS
            and (info.get("usage_page"), info.get("usage")) == CONTROL_USAGE)


def is_status_frame(r) -> bool:
    """True for the ``13 0a`` shape, online or offline."""
    return bool(r) and len(r) > LINK_INDEX and r[0] == REPORT_ID and r[1] == STATUS


def is_offline_frame(r) -> bool:
    """The receiver's answer for a sleeping keyboard: link byte 0, no level."""
    return is_status_frame(r) and r[LINK_INDEX] == 0


def parse_reply(r) -> Optional[Tuple[int, bool]]:
    """(level, linked) from the S85 status frame, or None.

    An offline frame (link byte 0) is refused: its level byte is a placeholder,
    measured as 1 and 0 on the reporter's keyboard while it was at 85 %.
    """
    if not is_status_frame(r) or is_offline_frame(r):
        return None
    b = list(r)
    level = b[LEVEL_INDEX]
    if not 0 <= level <= 100:
        return None
    return level, b[LINK_INDEX] != 0


class RkS85Provider(Provider):
    name = "rk_s85"

    def __init__(self):
        self._diag: List[str] = []
        # level, when, read on the cable (so the text can say where it came from)
        self._last: Optional[Tuple[int, float, bool]] = None

    def _read(self, path) -> Optional[Tuple[int, bool]]:
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        frames = {"ack": 0, "offline": 0, "status": 0, "other": 0}
        try:
            deadline = time.monotonic() + WINDOW_S
            last_send = 0.0
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now - last_send >= 0.25:
                    try:
                        dev.write(REQUEST)
                    except (OSError, ValueError) as e:
                        self._diag.append(f"  write: {e}")
                        return None
                    last_send = now
                r = dev.read(64, READ_TIMEOUT_MS)
                if not r:
                    continue
                if len(r) > 1 and r[0] == REPORT_ID and r[1] != STATUS:
                    frames["ack"] += 1
                elif is_offline_frame(r):
                    frames["offline"] += 1
                    self._diag.append(f"  offline frame: {hexdump(r, 20)}: link byte 0, "
                                      f"level byte {r[LEVEL_INDEX]} is a placeholder and is ignored")
                    continue
                elif is_status_frame(r):
                    frames["status"] += 1
                    self._diag.append(f"  status frame: {hexdump(r, 20)}")
                else:
                    frames["other"] += 1
                    self._diag.append(f"  reply: {hexdump(r, 20)}")
                parsed = parse_reply(r)
                if parsed is not None:
                    self._diag.append("  frames: " + ", ".join(
                        f"{count} {kind}" for kind, count in frames.items() if count))
                    return parsed
            self._diag.append("  frames: " + ", ".join(
                f"{count} {kind}" for kind, count in frames.items() if count))
            self._diag.append("  no status frame (keyboard asleep or receiver idle)")
            return None
        except (OSError, IOError, ValueError) as e:
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
        try:
            infos = hidlist.enumerate(VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(rk s85): %s", e)
            return []
        control = [d for d in infos if is_s85(d)]
        if not control:
            return []
        # The cable id is the charging signal: when the keyboard is plugged into
        # USB it switches from the receiver to 0193 and the receiver can stop
        # answering.  Ask the cable first, then the receiver.
        groups = {}
        for d in control:
            groups.setdefault(d["product_id"], []).append(d)
        cable = CABLE_PID in groups
        order = [CABLE_PID, PID] if cable else [PID]
        for pid in order:
            group = groups.get(pid)
            if not group:
                continue
            self._diag.append(f"[RK S85] pid={pid:04x} "
                              f"{'USB cable' if pid == CABLE_PID else '2.4 GHz receiver'}")
            parsed = self._read(group[0]["path"])
            if parsed is not None:
                level, linked = parsed
                charging = pid == CABLE_PID
                self._last = (level, time.time(), charging)
                self._diag.append(f"  -> {level}% charging={charging} linked={linked}")
                return [DeviceStatus(KEY, NAME, level, charging, True, "rk_s85",
                                     kind="keyboard", charging_known=pid == CABLE_PID,
                                     via="USB cable" if pid == CABLE_PID else "2.4 GHz receiver")]
        if cable:
            # on the cable, but its query answered nothing usable: charging, and
            # the level only if one was read recently enough to be worth showing
            last = self._last
            if last is None or time.time() - last[1] >= ASLEEP_KEEP:
                return [DeviceStatus(KEY, NAME, None, True, True, "rk_s85",
                                     kind="keyboard", charging_known=True, via="USB cable",
                                     approx="charging (level not reported while charging)",
                                     ui_message="state.charging_level_unknown")]
            level, _, on_cable = last
            if on_cable:
                return [DeviceStatus(KEY, NAME, level, True, True, "rk_s85",
                                     kind="keyboard", charging_known=True, via="USB cable")]
            return [DeviceStatus(KEY, NAME, level, True, True, "rk_s85",
                                 kind="keyboard", charging_known=True, via="USB cable",
                                 approx=f"charging (about {level}% before it was plugged in)",
                                 ui_message="state.charging_was", ui_params={"level": level})]
        if self._last and time.time() - self._last[1] < ASLEEP_KEEP:
            return [DeviceStatus(KEY, NAME, self._last[0], False, False, "rk_s85",
                                 kind="keyboard", charging_known=False,
                                 wake_on_input=True)]
        return [DeviceStatus(KEY, NAME, None, False, False, "rk_s85",
                             kind="keyboard", charging_known=False,
                             wake_on_input=True)]

    def diagnostics(self) -> List[str]:
        return list(self._diag)
