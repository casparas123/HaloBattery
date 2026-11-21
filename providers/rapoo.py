"""Rapoo gaming devices that push their battery by themselves.

The older VT9 Pro receiver 24AE:185A uses a separate passive seven-byte BB
format, restored from #109's reporter USBPcap and the test-build source:
https://github.com/user-attachments/files/32778389/wireshark.capture.pcapng.gz
https://github.com/casparas123/HaloBattery/blob/d5a5a5dfc6e93b2b26719f590e7dc65f1e3e4bdb/providers/rapoo.py
The capture's ten interrupt-IN packets on endpoint 82 are all
"bb b0 42 20 03 01 64": state at index 5, percentage at index 6. State 1
means discharging, 2 charging; transition/unknown states are refused. The
test-build source records the state meanings; the capture confirms only
100% discharging. Windows collection-sized reads may add zero padding after
the seven-byte payload. This family never uses the Gen-2 07/BD parsers below.

Windows lists ff00:0002 twice on this receiver, as two top-level collections
with their own paths, and the push rides one of them: test build 1 kept one
path per usage and heard nothing, test build 2 opened every path and read
"bb b0 42 20 03 01 57" - 87 %, the number Rapoo's app showed (#109,
2026-11-20). So these candidates are kept by path, never folded by usage.

On its USB cable the same mouse leaves the radio - the receiver goes silent -
and enumerates as 24AE:418A with the receiver's collection shape (the
reporter's two HID lists, 2026-11-21; Rapoo's app says "charging" then). The
cable is listened to first, the same way, and a mouse on its cable is shown
charging. If the cable stays silent while the receiver is enumerated too, the
cable id alone is shown as charging with the level from before it went in -
inferred from this exact receiver/cable pair, not read from a reply.

Rapoo's own web driver at hub.rapoo.com ("RAPOO HUB - VT Generation 2 Series
Web Driver") reads the battery of every device it supports without ever asking
for it: the device pushes a status report on input report 7 and the driver's
parsers decode it. All four of its protocol parsers (Realtek 2-in-1, Telink,
Telink base and VT nRF54L) share the same layout for that report:

    byte 0: connection type (low nibble: 0 = 2.4 GHz receiver, 1 = Bluetooth,
            2 = USB) and the sensor type (high nibble)
    byte 1: DPI gear
    byte 2-3: DPI X, byte 4-5: DPI Y
    byte 6: battery state - 0 invalid, 1 on battery, 2 charging
    byte 7: battery percent 0..100
    byte 8: Bluetooth mode, byte 9-10: report rates, byte 11: config

Nothing is ever written to the devices; the vendor collections are listened on
in turn and the one that answers is remembered, exactly like the 0xBB listener
for the 2023 receivers in issue #109 (nothing about which collection carries
the report is visible before hardware runs it).

Two devices are claimed, from issue #193's diagnostics - each over the two
ids it appears under, so one icon covers both of its states:

  * the VT7 (Gen-2) mouse: 24AE:1413 (its 2.4 GHz receiver; Rapoo's own device
    map points this receiver id at the "VT7" model) and 24AE:4613, the id the
    same mouse shows up under while it sits on its charging cable (seen in the
    reporter's 2026-10-03 run: 1413 gone from the tree, 4613 present, the
    byte-identical collection shape).
  * the V700DIY-98 keyboard: 24AE:4824 ("Rapoo Gaming Keyboard", the id from
    the first dump - the keyboard was plugged in for charging then) and
    24AE:1924 ("Rapoo 2.4G Wireless Device"), the id it presents over its own
    2.4 GHz receiver in the 2026-10-03 run.

The mouse is the same generation the web driver is named for, so the push is
expected there - charging included: both ids feed the same icon, and a reading
that reports charging wins over one that does not.

The keyboard does not use the report-7 shape: its receiver pushes a 0xBD
status frame - marker 0xBD, status variant 0x01, the state byte (1 on battery,
2 charging) at index 7 and the level at index 8. That layout is read from
Rapoo's own A HUB device layer (RapooDevice.dll, the KBForV700DIY status path,
which clamps index 8 to at most 100 and publishes it as the device's
Electricity property), and the reporter's two captures carry it verbatim:
`bd 01 00 07 00 46 00 01 54 ..` - state 1, level 84 - twenty minutes apart.
Both Gen-2 shapes are accepted on the Gen-2 ids, as the vendor layer's own
receiver dispatch does. The older VT9 Pro 185A is isolated to its BB parser.

Verified on the reporter's hardware: the mouse - level and charging. The
keyboard has not answered this provider yet; the next test build is the check.
"""
from __future__ import annotations

import re
import time
from functools import partial
from typing import Dict, List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

RAPOO_VID = 0x24AE
VT7_RECEIVER = 0x1413      # the mouse's 2.4 GHz receiver
VT7_CABLE = 0x4613         # ... and the same mouse on its charging cable
V700DIY_KEYBOARD = 0x4824  # the keyboard on its cable / plugged in
V700DIY_RECEIVER = 0x1924  # ... and its own 2.4 GHz receiver
VT9_PRO_RECEIVER = 0x185A  # older VT9 Pro, passive BB family from issue #109
VT9_PRO_CABLE = 0x418A     # ... and the same mouse on its USB cable (#109's HID lists)
VT9_PRO_NAME = "Rapoo VT9 Pro"
# pid -> (name, kind, the id its icon is keyed on): both states of one device
# share an icon, and a reading that reports charging wins in the merge below.
PIDS = {
    VT7_RECEIVER: ("Rapoo VT7 (Gen-2)", "mouse", VT7_RECEIVER),
    VT7_CABLE: ("Rapoo VT7 (Gen-2)", "mouse", VT7_RECEIVER),
    V700DIY_RECEIVER: ("Rapoo V700DIY-98", "keyboard", V700DIY_KEYBOARD),
    V700DIY_KEYBOARD: ("Rapoo V700DIY-98", "keyboard", V700DIY_KEYBOARD),
    0x14A9: ("Rapoo VT9 Pro V2", "mouse", 0x14A9),
    VT9_PRO_RECEIVER: (VT9_PRO_NAME, "mouse", VT9_PRO_RECEIVER),
    VT9_PRO_CABLE: (VT9_PRO_NAME, "mouse", VT9_PRO_RECEIVER),
}

VENDOR_PAGE = 0xFF00
PREFERRED_USAGES = (0x0002, 0x000E)   # the vendor collections every dump shows first
MOUSE_USAGE = (0x0001, 0x0002)        # never opened: the OS owns the pointer stream
KEYBOARD_USAGE = (0x0001, 0x0006)     # never opened: the OS owns the key stream

REPORT_ID = 0x07
STATUS_INDEX = 7                      # buffer indexes: byte 0 is the report id
LEVEL_INDEX = 8
INVALID = 0x00
ON_BATTERY = 0x01
CHARGING = 0x02

# the V700DIY-98 keyboard's receiver pushes its status on marker 0xBD variant 1:
# the state byte rides index 7 and the level index 8 (#193, measured).
BD_MARKER = 0xBD
BD_VARIANT = 0x01
BD_STATE_INDEX = 7
BD_LEVEL_INDEX = 8

READ_ATTEMPTS = 12                    # ~3 s per collection: the push comes every ~1-3 s
SWEEP_ATTEMPTS = 4                    # ~1 s for the extra candidates of a full scan: a
                                      # silent device then costs 3 s + 1 s + 1 s, not 9 s
                                      # (#215 review, @ahmedkhursheed23)
READ_TIMEOUT_MS = 250
MAX_CANDIDATES = 3                    # the dumps show this many vendor collections and up
QUIET_RESCAN_EVERY = 5                # a silent device is fully re-scanned occasionally
ASLEEP_KEEP = 300                     # s, as in the other receiver providers
VT9_PRO_ATTEMPTS = 16                # 4 s covers the observed ~3 s BB push cadence
VT9_PRO_BUFFER_LIMIT = 64            # existing read(64) caller bound, NOT a descriptor/report size
VT9_PRO_LINK = 0x03                  # byte 4 of every receiver push: the 2.4 GHz link
VT9_PRO_MAX_PATHS = 3                # 3 x 4 s stays inside the provider's 20 s deadline
                                     # (halo/polling.py); #109's ids list exactly three
VT9_PRO_IDS = (VT9_PRO_CABLE, VT9_PRO_RECEIVER)   # one device, polled cable first
VT9_PRO_KEY = f"rapoo:{VT9_PRO_RECEIVER:04x}"

Reading = Tuple[int, bool]


def parse_vt9pro(r, *, cable: bool = False) -> Optional[Reading]:
    """The old receiver's seven-byte BB/B0 push, allowing zero-only HID padding.

    The pinned test build accepts >=7 bytes. Windows HIDAPI/native reads use
    HIDP_CAPS.InputReportByteLength (the collection maximum), not the length
    of this report ID. Keep the existing read(64) caller bound and reject any
    meaningful trailing bytes; neither 32 nor 64 is a known descriptor size.

    Byte 4 names the link, 0x03 (2.4 GHz) in every receiver push. No push from
    the mouse on its cable (24AE:418A) has been seen yet, so cable=True takes
    any link byte there; every other check stays the same.
    """
    if (not isinstance(r, (bytes, bytearray, list, tuple)) or not 7 <= len(r) <= VT9_PRO_BUFFER_LIMIT
            or any(type(byte) is not int or not 0 <= byte <= 255 for byte in r)):
        return None
    if any(r[7:]):
        return None
    if r[0] != 0xBB or r[1] != 0xB0 or (not cable and r[4] != VT9_PRO_LINK):
        return None
    state, level = r[5], r[6]
    if state not in (1, 2) or not 0 <= level <= 100:
        return None
    return level, state == 2


def parse_bd(r) -> Optional[Reading]:
    """(level, charging) from the keyboard's 0xBD status push, or None.

    Marker 0xBD with status variant 1, the state byte (1 on battery, 2 charging)
    at index 7 and the level at index 8 - the layout the vendor's own device
    layer decodes (it clamps index 8 to at most 100 before publishing it as the
    Electricity property). The reporter's captures carry it verbatim:
    `bd 01 00 07 00 46 00 01 54 ..`.
    """
    if not r or len(r) <= BD_LEVEL_INDEX:
        return None
    if r[0] != BD_MARKER or r[1] != BD_VARIANT:
        return None
    state = r[BD_STATE_INDEX]
    if state not in (ON_BATTERY, CHARGING):
        return None
    level = r[BD_LEVEL_INDEX]
    if not 0 <= level <= 100:
        return None
    return level, state == CHARGING


def parse_report(r) -> Optional[Reading]:
    """(level, charging) from a battery push, or None when it is not one.

    Two shapes answer: the report-7 status the mouse pushes and the keyboard's
    0xBD status - see parse_bd.
    """
    if r and r[0] == BD_MARKER:
        return parse_bd(r)
    if not r or len(r) <= LEVEL_INDEX:
        return None
    if r[0] != REPORT_ID:
        return None
    status = r[STATUS_INDEX]
    if status not in (ON_BATTERY, CHARGING):
        return None                   # 0 = the device calls the reading invalid; not shown
    level = r[LEVEL_INDEX]
    if not 0 <= level <= 100:
        return None
    return level, status == CHARGING


def candidates(ifaces: List[dict]) -> List[dict]:
    """The collections to listen on, best first: vendor page, then the rest.

    Report 7 rides one of the device's vendor collections, but which of them
    Windows hands it out on is not known before a run, so every collection is
    a candidate except the plain mouse and keyboard ones (the OS owns those).
    The same usage listed twice is one collection; the vendor usages are kept
    in a fixed order.
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
        if (d.get("usage_page"), d.get("usage")) in (MOUSE_USAGE, KEYBOARD_USAGE):
            continue
        key = (d.get("usage_page"), d.get("usage"))
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return sorted(out, key=rank)


def vt9pro_collections(infos: List[dict], pid: int) -> List[dict]:
    """Interface 1's ff00:0002 / ff00:000e collections of one VT9 Pro id, by path.

    Windows lists ff00:0002 twice on the #109 receiver - two top-level
    collections with their own paths - and the push rides one of them: test
    build 1 kept one path per usage and heard nothing, test build 2 opened every
    path and read the level. So every path is kept (one listed twice is opened
    once), ff00:0002 first, each usage in enumeration order. The pointer,
    keyboard and consumer collections are never opened.
    """
    mine: List[dict] = []
    seen = set()
    for d in infos:
        if (d.get("product_id") != pid or d.get("vendor_id", RAPOO_VID) != RAPOO_VID
                or d.get("interface_number") != 1 or d.get("usage_page") != VENDOR_PAGE
                or d.get("usage") not in PREFERRED_USAGES or d.get("path") in seen):
            continue
        seen.add(d.get("path"))
        mine.append(d)
    return sorted(mine, key=lambda d: PREFERRED_USAGES.index(d["usage"]))


def path_tag(path) -> str:
    """' col06' from a Windows HID path, so diagnostics tell duplicate usages apart."""
    if isinstance(path, (bytes, bytearray)):
        path = bytes(path).decode("ascii", "replace")
    match = re.search(r"&col([0-9a-f]+)", str(path), re.IGNORECASE)
    return f" col{match.group(1).lower()}" if match else ""


class RapooProvider(Provider):
    name = "rapoo"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Dict[str, Tuple[int, bool, float]] = {}
        self._chosen: Dict[int, bytes] = {}
        self._quiet: Dict[int, int] = {}
        self._meta: Dict[str, Tuple[str, str]] = {}            # key -> (name, kind)

    def _listen(self, path: bytes, attempts: int = READ_ATTEMPTS, *, parser=parse_report) -> Optional[Reading]:
        """Read what the device pushes; nothing is ever sent."""
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            for _ in range(attempts):
                r = dev.read(64, READ_TIMEOUT_MS)
                if not r:
                    continue              # quiet until the next push
                reading = parser(r)
                self._diag.append(f"    report: {hexdump(r, 12)}"
                                  + ("" if reading else "  (not a battery shape)"))
                if reading is not None:
                    return reading
                time.sleep(0.02)
            self._diag.append(f"    nothing in ~{attempts * READ_TIMEOUT_MS / 1000:.1f} s "
                              "(device off, or another collection)")
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
        found: Dict[str, List[Tuple[int, bool, str, str]]] = {}   # key -> readings
        for pid, (name, kind, logical) in PIDS.items():
            if pid in VT9_PRO_IDS:
                continue                  # the older VT9 Pro and its cable: _poll_vt9pro
            mine = [d for d in infos if d["product_id"] == pid]
            if not mine:
                continue
            key = f"rapoo:{logical:04x}"
            self._meta[key] = (name, kind)
            self._diag.append(f"[Rapoo] pid={pid:04x} '{name}' "
                              f"'{(mine[0].get('product_string') or '').strip()}'")
            order = candidates(mine)
            if self._chosen.get(pid):
                order = sorted(order, key=lambda d: d["path"] != self._chosen[pid])
            # a device that has stayed silent is listened on its first candidate
            # only, and fully re-scanned every few polls in case it moved
            quiet = self._quiet.get(pid, 0)
            deep = self._chosen.get(pid) is None and (
                quiet == 0 or quiet % QUIET_RESCAN_EVERY == 0)
            budget = order[:MAX_CANDIDATES] if deep else order[:1]
            got = None
            for n, d in enumerate(budget):
                self._diag.append(f"  listening on iface={d.get('interface_number')} "
                                  f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
                # The first candidate is the one that carries the pushes, and it gets the
                # full window (one push period); the extra candidates of a full scan only
                # a short one - they are a fallback for a device that moved.
                got = self._listen(d["path"],
                                   READ_ATTEMPTS if n == 0 else SWEEP_ATTEMPTS)
                if got is not None:
                    self._chosen[pid] = d["path"]
                    break
            if got is not None:
                self._quiet[pid] = 0
                found.setdefault(key, []).append((got[0], got[1], name, kind))
                continue
            self._quiet[pid] = quiet + 1
            self._diag.append("  nothing heard; keeping the last level, greyed out"
                              if self._last.get(key) else
                              "  nothing heard yet and no earlier level")

        out: List[DeviceStatus] = []
        for key, readings in found.items():
            # one device over two ids: a reading that reports charging wins
            # over one that does not (a charging cable beats a stale receiver)
            readings.sort(key=lambda r: (not r[1],))
            level, charging, name, kind = readings[0]
            self._last[key] = (level, charging, time.time())
            out.append(DeviceStatus(key, name, level, charging, True, "rapoo",
                                    kind=kind))
        # silent: a receiver cannot tell a switched-off mouse from one that went
        # to sleep a few seconds ago, so keep the last value greyed out for a while
        now = time.time()
        for key, last in self._last.items():
            if key == VT9_PRO_KEY or key in found or now - last[2] >= ASLEEP_KEEP:
                continue
            name, kind = self._meta.get(key, ("Rapoo device", "mouse"))
            out.append(DeviceStatus(key, name, last[0], last[1], False, "rapoo",
                                    kind=kind))
        vt9 = self._poll_vt9pro(infos)
        if vt9 is not None:
            out.append(vt9)
        return out

    def _listen_vt9pro(self, pid: int, cols: List[dict], *, cable: bool) -> Optional[Reading]:
        """Listen on one VT9 Pro id's vendor paths; remember the one that answers.

        A silent id is listened on its first path only and fully re-scanned every
        few polls. A remembered path that is gone (the receiver was re-plugged
        between two polls) is forgotten, so the next scan covers every path.
        """
        chosen = self._chosen.get(pid)
        if chosen is not None and all(d["path"] != chosen for d in cols):
            self._chosen.pop(pid, None)
            self._quiet.pop(pid, None)
            chosen = None
        order = list(cols)
        if chosen is not None:
            order.sort(key=lambda d: d["path"] != chosen)
        quiet = self._quiet.get(pid, 0)
        deep = chosen is None and (quiet == 0 or quiet % QUIET_RESCAN_EVERY == 0)
        parser = partial(parse_vt9pro, cable=cable)
        for d in (order[:VT9_PRO_MAX_PATHS] if deep else order[:1]):
            self._diag.append(f"  listening on iface={d.get('interface_number')} "
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}"
                              f"{path_tag(d['path'])}")
            got = self._listen(d["path"], VT9_PRO_ATTEMPTS, parser=parser)
            if got is not None:
                self._chosen[pid] = d["path"]
                self._quiet[pid] = 0
                return got
        self._quiet[pid] = quiet + 1
        return None

    def _poll_vt9pro(self, infos: List[dict]) -> Optional[DeviceStatus]:
        """The older VT9 Pro over its receiver (185A) and its USB cable (418A): one icon."""
        receiver = vt9pro_collections(infos, VT9_PRO_RECEIVER)
        cable = vt9pro_collections(infos, VT9_PRO_CABLE)
        if not receiver and not cable:
            # unplugged: a later plug-in starts from nothing
            self._last.pop(VT9_PRO_KEY, None)
            for pid in VT9_PRO_IDS:
                self._chosen.pop(pid, None)
                self._quiet.pop(pid, None)
            return None
        now = time.time()
        if cable:
            self._diag.append(f"[Rapoo] pid={VT9_PRO_CABLE:04x} '{VT9_PRO_NAME}' on its USB cable "
                              f"'{(cable[0].get('product_string') or '').strip()}'")
            if receiver:
                self._diag.append(f"  receiver {VT9_PRO_RECEIVER:04x} not listened to: "
                                  "a mouse on its cable is off the radio")
            got = self._listen_vt9pro(VT9_PRO_CABLE, cable, cable=True)
            if got is not None:
                # on its cable the mouse charges, whatever the push's state byte
                # says (the frame itself is in the diagnostics above)
                self._last[VT9_PRO_KEY] = (got[0], True, now)
                return DeviceStatus(VT9_PRO_KEY, VT9_PRO_NAME, got[0], True, True, "rapoo",
                                    kind="mouse")
            if receiver:
                self._diag.append("  nothing from the cable: shown charging from the cable id")
                return self._vt9pro_on_cable(now)
            # the cable alone and silent: nothing ties it to the receiver, so no
            # charging is inferred - the greyed rule below
        else:
            self._diag.append(f"[Rapoo] pid={VT9_PRO_RECEIVER:04x} '{VT9_PRO_NAME}' "
                              f"'{(receiver[0].get('product_string') or '').strip()}'")
            got = self._listen_vt9pro(VT9_PRO_RECEIVER, receiver, cable=False)
            if got is not None:
                self._last[VT9_PRO_KEY] = (got[0], got[1], now)
                return DeviceStatus(VT9_PRO_KEY, VT9_PRO_NAME, got[0], got[1], True, "rapoo",
                                    kind="mouse")
        last = self._last.get(VT9_PRO_KEY)
        if last is not None and now - last[2] < ASLEEP_KEEP:
            self._diag.append("  nothing heard; keeping the last level, greyed out")
            return DeviceStatus(VT9_PRO_KEY, VT9_PRO_NAME, last[0], False, False, "rapoo",
                                kind="mouse", wake_on_input=True, charging_known=False)
        self._diag.append("  nothing heard yet and no earlier level")
        return None

    def _vt9pro_on_cable(self, now: float) -> DeviceStatus:
        """On its cable with the cable silent: charging, inferred from the cable id.

        24AE:418A is enumerated exactly while the mouse sits on its cable (the
        reporter's two HID lists, #109) and Rapoo's own app shows "charging"
        then, while the receiver beside it hears nothing. That pair is the only
        evidence, so this runs only while the receiver is enumerated too. The
        level is the last one read: a charging push a moment ago as it is,
        otherwise the one from before the cable went in, labelled as such.
        """
        last = self._last.get(VT9_PRO_KEY)
        if last is None:
            return DeviceStatus(VT9_PRO_KEY, VT9_PRO_NAME, None, True, True, "rapoo",
                                kind="mouse",
                                approx="charging (level not reported while charging)",
                                ui_message="state.charging_level_unknown")
        level, charging_then, when = last
        if charging_then and now - when < ASLEEP_KEEP:
            return DeviceStatus(VT9_PRO_KEY, VT9_PRO_NAME, level, True, True, "rapoo",
                                kind="mouse")
        return DeviceStatus(VT9_PRO_KEY, VT9_PRO_NAME, level, True, True, "rapoo",
                            kind="mouse",
                            approx=f"charging (about {level}% before it was plugged in)",
                            ui_message="state.charging_was", ui_params={"level": level})

    def diagnostics(self) -> List[str]:
        return list(self._diag)
