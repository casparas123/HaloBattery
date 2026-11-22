"""Sinowealth 2.4 GHz mouse receivers (`258a:002f`): the short status reports.

The receiver answers on its vendor collection (`ff00:0001`, interface 1) with a
7-byte feature report 5 - there is no interrupt read on that collection:

    send   SET_FEATURE 5   05 80 00 00 00 00 00 00     link status
    wait   ~100 ms
    read   GET_FEATURE 5   05 80 01 01                  mouse linked
                           05 80 00 ..                  mouse off or asleep
    send   SET_FEATURE 5   05 90 00 00 00 00 00 00     battery
    read   GET_FEATURE 5   05 90 11 64                  wireless: 0x11, level 0-100
                           05 90 10 01                  on the cable: charging
                           05 90 10 02                  on the cable: charged

The frame is the one OpenMouse captured from a Redragon M690 Pro on this
receiver (`captures/redragon-m690-pro`, `docs/redragon-m690-pro-testing.md`)
and `krisgfx9/Wireless-Device-Battery-Monitor` verified on a Redragon M602-KS.
Both projects are GPL/AGPL, so this is an independent implementation written
from the captured byte shapes and reply meanings, not a port of their code.

`258a:002f` is a shared OEM receiver. The same id carries mice from several
brands and the product string is the generic "2.4G Wireless Receiver", so no
brand-specific gate can tell them apart; the protocol is the receiver's own,
which is why the reading is named for the family rather than for one brand.

Interface 1 lists the same `ff00:0001` usage three times: a large settings
block on report 8, an input report 7, and the 7-byte feature report 5 this
exchange uses. hidapi on Windows exposes no report lengths, so every candidate
path is tried and the one that answers report 5 is used.

The wireless reply carries the level only - the protocol has no charging flag
on that path; the wired status is its only charge report. Both states are
parsed, and `charging_known` stays False whenever the reply cannot say.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

try:
    import hid  # type: ignore
except Exception:                    # pragma: no cover - only without the package
    hid = None

VID = 0x258A
RECEIVER_PID = 0x002F
NAME = "Sinowealth wireless mouse"
KEY = f"sinowealth:{RECEIVER_PID:04x}"

USAGE_PAGE = 0xFF00
USAGE = 0x0001
REPORT_ID = 0x05
REQUEST_SIZE = 8                     # the 7-byte report plus its report id
REPLY_SIZE = 8
RF_DELAY_S = 0.10                    # USB -> 2.4 GHz -> mouse -> back
ATTEMPTS = 2                         # a dormant receiver can swallow the first
ASLEEP_KEEP = 300                    # s, keep the last level greyed

LINK_CMD = 0x80
BATTERY_CMD = 0x90
LINK_UP = 0x01
LINK_DOWN = 0x00
WIRELESS = 0x11                      # reply type: level at byte 3
WIRED = 0x10                         # reply type: charge state at byte 3
CHARGING = 0x01
CHARGED = 0x02
CHARGED_LEVEL = 100                  # the charge reply carries no level; a charged
                                     # pack is full, which is what the maker's app shows


def make_request(command: int) -> List[int]:
    """The 8-byte feature request: report id, command, zero payload."""
    return [REPORT_ID, command] + [0x00] * (REQUEST_SIZE - 2)


def is_reply(reply, command: int) -> bool:
    """True when this is the documented answer to `command` (id and command echoed)."""
    return (bool(reply) and len(reply) >= 4 and reply[0] == REPORT_ID
            and reply[1] == command)


def parse_link(reply) -> Optional[bool]:
    """True linked, False mouse off or asleep, None when this is not the link reply."""
    if not is_reply(reply, LINK_CMD):
        return None
    if reply[2] == LINK_UP:
        return True
    if reply[2] == LINK_DOWN:
        return False
    return None


def parse_battery(reply) -> Optional[Tuple[Optional[int], bool, bool]]:
    """(level, charging, charging_known) from the battery reply, or None.

    `11 <level>` is the wireless reading: a level and no charge flag, so the
    charging state is unknown. `10 01` is the cable charging, `10 02` charged -
    both on the cable; the reply carries no level, and a charged pack is full.
    A wireless level of 0 means the 2.4 GHz link is not up yet: no reading.
    """
    if not is_reply(reply, BATTERY_CMD):
        return None
    state, value = reply[2], reply[3]
    if state == WIRELESS:
        return (value, False, False) if 1 <= value <= 100 else None
    if state == WIRED:
        if value == CHARGING:
            return None, True, True
        if value == CHARGED:
            return CHARGED_LEVEL, True, True
    return None


class SinowealthReceiverProvider(Provider):
    name = "sinowealth_rx"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, float]] = None      # (level, when)

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        try:
            infos = hidlist.enumerate(VID)
        except Exception as e:        # pragma: no cover - the package misbehaving
            log.warning("hid.enumerate(sinowealth): %s", e)
            return []
        mine = [d for d in infos if d.get("product_id") == RECEIVER_PID]
        if not mine:
            return []
        self._diag.append(f"[Sinowealth] pid={RECEIVER_PID:04x} '{NAME}'")
        try:
            return self._poll_receiver(mine)
        except Exception as e:        # a provider must never take the poll down
            self._diag.append(f"  poll error: {e}")
            return [self._status(self._kept_level(), online=False)]

    def _poll_receiver(self, infos: List[dict]) -> List[DeviceStatus]:
        # the same usage appears three times; key by path and keep every candidate
        seen = set()
        candidates = []
        for d in infos:
            if ((d.get("usage_page") or 0) != USAGE_PAGE or (d.get("usage") or 0) != USAGE
                    or d.get("path") in seen):
                continue
            seen.add(d.get("path"))
            candidates.append(d)
        if not candidates:
            self._diag.append(f"  no ff{USAGE_PAGE & 0xFF:02x}:{USAGE:04x} collection "
                              f"among {len(infos)}; nothing opened")
            return [self._status(self._kept_level(), online=False)]
        for d in candidates:
            self._diag.append(f"  trying iface={d.get('interface_number')} "
                              f"{(d.get('usage_page') or 0):04x}:{(d.get('usage') or 0):04x}")
            result = self._probe(d["path"])
            if result is None:
                continue
            if result == "asleep":
                self._diag.append("  link down: mouse off or asleep")
                return [self._status(self._kept_level(), online=False)]
            if result[0] == "no-level":
                self._diag.append("  linked, but no usable battery reply")
                return [self._status(None, online=True)]
            level, charging, known = result[1]
            if level is not None:
                self._last = (level, time.time())
            self._diag.append(f"  -> {level if level is not None else '?'}%"
                              + (" charging" if charging else "")
                              + ("" if known else " (charging not reported)"))
            self._diag.append((f"  collection: iface={d.get('interface_number')} "
                               f"{(d.get('usage_page') or 0):04x}:{(d.get('usage') or 0):04x}"))
            return [DeviceStatus(KEY, NAME, level, charging, True, "sinowealth_rx",
                                 kind="mouse", charging_known=known)]
        self._diag.append("  no reply from any collection (mouse off, asleep or unpaired)")
        return [self._status(self._kept_level(), online=False)]

    def _probe(self, path: bytes):
        """One collection: "asleep", ("ok", (level, charging, known)),
        ("no-level", None) when linked without a level, or None when it does not answer."""
        dev = hid.device()  # type: ignore[union-attr]
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            link = None
            for attempt in range(ATTEMPTS):
                link = self._ask(dev, LINK_CMD, attempt)
                if link is not None:
                    break
            linked = parse_link(link)
            if linked is None:
                return None
            if not linked:
                return "asleep"
            battery = None
            for attempt in range(ATTEMPTS):
                battery = self._ask(dev, BATTERY_CMD, attempt)
                if battery is not None:
                    break
            parsed = parse_battery(battery)
            return ("ok", parsed) if parsed is not None else ("no-level", None)
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _ask(self, dev, command: int, attempt: int) -> Optional[List[int]]:
        try:
            sent = dev.send_feature_report(bytes(make_request(command)))
            if sent is None or sent < 0:
                self._diag.append(f"    {command:02x} attempt {attempt + 1}: "
                                  f"feature refused ({sent})")
                return None
            time.sleep(RF_DELAY_S)
            reply = dev.get_feature_report(REPORT_ID, REPLY_SIZE)
        except (OSError, IOError, ValueError, AttributeError) as e:
            self._diag.append(f"    {command:02x} attempt {attempt + 1}: {e}")
            return None
        if reply:
            self._diag.append(f"    {command:02x} attempt {attempt + 1}: "
                              f"{hexdump(reply, REPLY_SIZE)}")
        return list(reply) if reply else None

    def _kept_level(self) -> Optional[int]:
        if self._last and time.time() - self._last[1] < ASLEEP_KEEP:
            return self._last[0]
        return None

    @staticmethod
    def _status(level: Optional[int], online: bool) -> DeviceStatus:
        # A receiver that is still plugged in but silent must keep its icon: an
        # empty list removes it, which reads as "the device disappeared".
        return DeviceStatus(KEY, NAME, level, False, online, "sinowealth_rx",
                            kind="mouse", wake_on_input=True, charging_known=False)

    def diagnostics(self) -> List[str]:
        return list(self._diag)
