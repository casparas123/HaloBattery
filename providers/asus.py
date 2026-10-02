"""ASUS ROG / TUF wireless mice and headsets, on their 2.4 GHz receiver or on the cable.

Source: G-Helper (seerge/g-helper, app/Peripherals/Mouse/AsusMouse.cs and the model
files in app/Peripherals/Mouse/Models). Only the protocol facts are used here (ids,
command, byte positions); the code is written for this app.

The exchange, on the vendor collection of interface 0 ("mi_00" in G-Helper):

    request   00 12 07          report id 0, command 12 07 (battery), zeros up to 65 bytes
    reply     00 12 07 ...      the echo of the command; after the report id:
                  byte 4        battery: a percentage, or a level 0..4 on older models
                  byte 9        charging when not 0
    error     00 ff aa ...      the mouse does not know the command
    zeros     00 00 00 ...      no reply (the mouse is off or asleep)

hidapi on Windows leaves out report id 0, so a reply starts with 12 07. A reply with
the report id in front is accepted too.

G-Helper takes a battery value of 0 without charging as "the mouse is in standby",
not as an empty battery. This provider does the same: no reading.

Not included (they use a different collection, report id or byte position in
G-Helper, and none was on hand to test): the OMNI receiver (0B05:1ACE), the Harpe II
Ace, Keris II Ace / Origin, Harpe Ace Mini / Extreme, Strix Carry, Gladius II Wireless
and the MD200.

The ROG Strix Go 2.4 headset (0B05:18D6, #190) is a different family in the same
brand, from @vancinis's G-Helper work (app/Peripherals/Headset on the
feat/rog-strix-go-24-support branch, tested on their own headset):

    request   feature report 0xFF, 64 bytes: FF 08 00 FD 04 12 F1 03 52 01 00 ...
    reply     feature report: FF 1B 05 FE ... 0E YY 12 ... - level byte 13, 0x40 = 64%
    error     FF AA at bytes 1-2: the packet is not known to the firmware
    zeros     bytes 1-3 all zero: headset off or asleep (no reading)

The collection is picked as G-Helper picks it: the first of the receiver's
collections whose feature report is at least 64 bytes long (HidP_GetCaps), not by
interface number or usage page. A level of 0 or one above 100 is refused rather
than shown, and no charging state is reported - G-Helper has not identified that
byte either.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log
from .gwolves import feature_length

ASUS_VID = 0x0B05
INTERFACE = 0

REQUEST = [0x00, 0x12, 0x07]         # report id 0, battery command
ECHO = (0x12, 0x07)
ERROR = (0xFF, 0xAA)
PACKET_LENGTH = 65                   # G-Helper's default packet size (report id + 64)
LEVEL_BYTE = 4                       # after the echo is found at 0 and 1
CHARGING_BYTE = 9

READ_TIMEOUT_MS = 300                # G-Helper's USB timeout
READ_ATTEMPTS = 8                    # other reports on the collection are skipped
WRITE_ATTEMPTS = 3

PERCENT, STEPS = 1, 25               # scale of the battery byte

# the ROG Strix Go 2.4 headset (#190): a feature-report exchange of its own
HEADSET_PID = 0x18D6
HEADSET_NAME = "ROG Strix Go 2.4"
HEADSET_REPORT_ID = 0xFF
HEADSET_REQUEST = [0xFF, 0x08, 0x00, 0xFD, 0x04, 0x12, 0xF1, 0x03, 0x52, 0x01]
HEADSET_PACKET_LENGTH = 64
HEADSET_LEVEL_BYTE = 13
HEADSET_SLEEP = 0.035                # G-Helper waits 35 ms between send and read
HEADSET_ATTEMPTS = 3                 # its retry count on a read timeout
HEADSET_ERROR = (0xFF, 0xAA)         # reply bytes 1-2: packet not known to firmware

# pid -> (name, scale). Wired and wireless pids of one mouse share the name, so they
# share one icon.
KNOWN: Dict[int, Tuple[str, int]] = {
    0x1A72: ("ROG Gladius III Aimpoint", PERCENT),
    0x1A70: ("ROG Gladius III Aimpoint", PERCENT),
    0x1B0C: ("ROG Gladius III Eva 2", PERCENT),
    0x1B0A: ("ROG Gladius III Eva 2", PERCENT),
    0x197F: ("ROG Gladius III Wireless", PERCENT),
    0x197D: ("ROG Gladius III Wireless", PERCENT),
    0x1A1A: ("ROG Chakram X", PERCENT),
    0x1A18: ("ROG Chakram X", PERCENT),
    0x1A94: ("ROG Harpe Ace Aim Lab Edition", PERCENT),
    0x1A92: ("ROG Harpe Ace Aim Lab Edition", PERCENT),
    0x1A68: ("ROG Keris Wireless Aimpoint", PERCENT),
    0x1A66: ("ROG Keris Wireless Aimpoint", PERCENT),
    0x1979: ("ROG Spatha X", PERCENT),
    0x1977: ("ROG Spatha X", PERCENT),
    0x19F4: ("TUF Gaming M4 Wireless", PERCENT),
    0x1A8D: ("TX Gaming Mouse", PERCENT),
    0x1AF5: ("TX Gaming Mouse Mini", PERCENT),
    0x1AF3: ("TX Gaming Mouse Mini", PERCENT),
    0x1C57: ("TUF Gaming Mini Miku Edition", PERCENT),
    0x1C56: ("TUF Gaming Mini Miku Edition", PERCENT),
    # older models: the battery byte is a level 0..4, G-Helper multiplies it by 25
    0x18E5: ("ROG Chakram", STEPS),
    0x18E3: ("ROG Chakram", STEPS),
    0x1960: ("ROG Keris Wireless", STEPS),
    0x195E: ("ROG Keris Wireless", STEPS),
    0x1A59: ("ROG Keris EVA Edition", STEPS),
    0x1A57: ("ROG Keris EVA Edition", STEPS),
    0x1908: ("ROG Pugio II", STEPS),
    0x1906: ("ROG Pugio II", STEPS),
    0x1949: ("ROG Strix Impact II Wireless", STEPS),
    0x1947: ("ROG Strix Impact II Wireless", STEPS),
}

Reading = Tuple[int, bool, str]     # level %, charging, approximate text ("" = exact)


def _offset(r: List[int], pair: Tuple[int, int]) -> Optional[int]:
    """Where `pair` starts in a reply: 0 (hidapi left out report id 0) or 1 (report id
    in front). None when the reply does not start with it."""
    if len(r) > 1 and (r[0], r[1]) == pair:
        return 0
    if len(r) > 2 and r[0] == 0x00 and (r[1], r[2]) == pair:
        return 1
    return None


def has_echo(r: List[int]) -> bool:
    return _offset(r, ECHO) is not None


def is_error(r: List[int]) -> bool:
    return _offset(r, ERROR) is not None


def parse_headset_reply(r) -> Optional[int]:
    """The level from a Strix Go 2.4 reply, or None when this is not one.

    The byte positions are G-Helper's (StrixGo24.cs), whose buffer carries the
    report id at byte 0: the error marker FF AA at bytes 1-2, an all-zero run there
    means the headset is off or asleep, and the level is byte 13. hidapi keeps the
    report id of a numbered feature report in front as well; a reply without it is
    accepted too, with the same positions applied to the bytes after it."""
    if not r or len(r) < 4:
        return None
    skip = 0 if r[0] == HEADSET_REPORT_ID else 1

    def at(n):                       # the byte G-Helper's buffer has at n
        i = n - skip
        return r[i] if 0 <= i < len(r) else None

    if at(1) == HEADSET_ERROR[0] and at(2) == HEADSET_ERROR[1]:
        return None
    if at(1) == 0 and at(2) == 0 and at(3) == 0:
        return None
    level = at(HEADSET_LEVEL_BYTE)
    if level is None or not 1 <= level <= 100:
        return None
    return level


def parse_reply(r: List[int], scale: int) -> Optional[Reading]:
    """A reply that echoes 12 07 -> (level, charging, text). None for anything else,
    for the standby value (0 and not charging) and for a value out of range."""
    m = _offset(r, ECHO)
    if m is None or len(r) < m + CHARGING_BYTE + 1:
        return None
    raw = r[m + LEVEL_BYTE]
    charging = r[m + CHARGING_BYTE] != 0
    if raw == 0 and not charging:
        return None                                  # standby, not empty
    if scale == STEPS:
        if raw > 4:
            return None
        level = raw * STEPS
        text = f"about {level}%" + (", charging" if charging else "")
        return level, charging, text
    if raw > 100:
        return None
    return raw, charging, ""


class AsusProvider(Provider):
    name = "asus"

    def __init__(self):
        self._diag: List[str] = []
        self._failing: Dict[str, bool] = {}

    def _read(self, path, scale: int) -> Optional[Reading]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            # drop reports that are already waiting, as G-Helper does before a request
            try:
                dev.set_nonblocking(True)
                for _ in range(16):
                    if not dev.read(PACKET_LENGTH):
                        break
                dev.set_nonblocking(False)
            except (OSError, IOError, ValueError):
                pass
            for _ in range(WRITE_ATTEMPTS):
                try:
                    dev.write(REQUEST + [0x00] * (PACKET_LENGTH - len(REQUEST)))
                except (OSError, IOError, ValueError) as e:
                    self._diag.append(f"    write: {e}")
                    return None
                for _ in range(READ_ATTEMPTS):
                    r = list(dev.read(PACKET_LENGTH, READ_TIMEOUT_MS) or [])
                    if not r:
                        break                        # timeout: send the request again
                    if is_error(r):
                        self._diag.append(f"    reply: error {hexdump(r, 12)}")
                        return None
                    if not any(r):
                        self._diag.append("    reply: all zeros (off or asleep)")
                        return None
                    if not has_echo(r):
                        continue                     # a button or profile event: skip it
                    self._diag.append(f"    reply: {hexdump(r, 12)}")
                    res = parse_reply(r, scale)
                    if res is None:
                        self._diag.append("    battery 0 and not charging (standby), or out of range")
                    return res
            self._diag.append("    no reply with the 12 07 echo")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _read_headset(self, path) -> Optional[int]:
        """One feature-report exchange with the ROG Strix Go 2.4 (a mirror of
        G-Helper's WriteForResponse): send the packet, wait 35 ms, read the feature
        report back; a read that comes back empty re-sends, up to its retry count.
        None for the error marker, for an all-zero reply (off or asleep) and for a
        level out of range."""
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            request = HEADSET_REQUEST + [0x00] * (HEADSET_PACKET_LENGTH
                                                - len(HEADSET_REQUEST))
            for attempt in range(HEADSET_ATTEMPTS):
                try:
                    dev.send_feature_report(request)
                except (OSError, IOError, ValueError) as e:
                    self._diag.append(f"    send: {e}")
                    return None
                time.sleep(HEADSET_SLEEP)
                try:
                    r = list(dev.get_feature_report(HEADSET_REPORT_ID,
                                                    HEADSET_PACKET_LENGTH) or [])
                except (OSError, IOError, ValueError) as e:
                    self._diag.append(f"    read (attempt {attempt + 1}): {e}")
                    continue
                if not r:
                    self._diag.append(f"    read (attempt {attempt + 1}): empty")
                    continue
                self._diag.append(f"    reply: {hexdump(r, 16)}")
                level = parse_headset_reply(r)
                if level is None:
                    self._diag.append("    not a battery reply (error, off/asleep or "
                                      "out of range)")
                return level
            self._diag.append("    no feature-report reply")
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
            infos = hidlist.enumerate(ASUS_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(asus): %s", e)
            return []

        found: Dict[str, Reading] = {}
        for d in infos:
            pid = d["product_id"]
            if pid not in KNOWN:
                continue
            if d.get("interface_number") != INTERFACE or d.get("usage_page", 0) < 0xFF00:
                continue                                 # only the vendor collection
            name, scale = KNOWN[pid]
            self._diag.append(f"[ASUS] {name} pid={pid:04x} usage="
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
            res = self._read(d["path"], scale)
            if res is None:
                continue
            self._diag.append(f"  -> {res[2] or str(res[0]) + '%'}{' charging' if res[1] else ''}")
            # the same mouse on the cable and on the receiver: keep the charging reading
            if name not in found or (res[1] and not found[name][1]):
                found[name] = res

        out: List[DeviceStatus] = []
        for name, (level, charging, text) in found.items():
            key = "asus:" + name.lower().replace(" ", "-")
            out.append(DeviceStatus(key, name, level, charging, True, "asus", text, kind="mouse"))

        # the ROG Strix Go 2.4 headset (#190, docstring): its own collection rule and
        # an icon key of its own
        hinfos = [d for d in infos if d["product_id"] == HEADSET_PID]
        if hinfos:
            chosen = None
            for d in hinfos:
                n = feature_length(d["path"])
                if n is None or n >= HEADSET_PACKET_LENGTH:
                    chosen = d
                    break
                self._diag.append(f"  {HEADSET_NAME} "
                                  f"{d.get('usage_page', 0):04x}:"
                                  f"{d.get('usage', 0):04x} feature={n}: too short; "
                                  "skipped")
            if chosen is None:
                self._diag.append(f"[ASUS] {HEADSET_NAME}: no collection with a "
                                  "64-byte feature report")
            else:
                self._diag.append(f"[ASUS] {HEADSET_NAME} pid={HEADSET_PID:04x} usage="
                                  f"{chosen.get('usage_page', 0):04x}:"
                                  f"{chosen.get('usage', 0):04x}")
                level = self._read_headset(chosen["path"])
                if level is not None:
                    self._diag.append(f"  -> {level}%")
                    out.append(DeviceStatus("asus:" + HEADSET_NAME.lower().replace(" ", "-"),
                                            HEADSET_NAME, level, False, True, "asus",
                                            kind="headset"))
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
