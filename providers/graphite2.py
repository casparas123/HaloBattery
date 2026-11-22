"""IO by Red Square Graphite 2 Pro Wireless (4c4a:4155 dongle, 0d8c:0379 wired).

Test 4: the reporter's report descriptors settle the transport. The dongle's
vendor collections are a pair, not one channel:

* ``ff90:0001`` - **input only**, report 0x55, 63 data bytes: the status side.
* ``ff82:0001`` - **input and output**, report 0x41, 63 data bytes each way: the
  command side. It declares no feature report at all.

That explains both earlier failures exactly: test 1 wrote output report 0x55 to
ff90 (which has no output report) and test 2 read a feature report (which neither
collection declares). So the family frame is sent on the collection that does
have an output report, using its own report id:

    write  ff82  0x41 + 65 01 + zeros        (M HUB's audio.blk.readBattery)
    read   ff82  0x41 65 <level> <state>     (if the dongle answers in place)
    read   ff90  0x55 65 <level> <state>     (if it answers on the status side)

The answer may arrive on either side, so the build reads both: the command
collection first, then the status collection. It also listens on ff90 once before
sending anything, in case this firmware pushes a frame on its own.

Nothing outside the two declared reports is touched - no feature reads, no writes
to ff90. Unverified on hardware.
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

STATUS_PAGE, STATUS_USAGE = 0xFF90, 0x0001
COMMAND_PAGE, COMMAND_USAGE = 0xFF82, 0x0001
STATUS_ID, COMMAND_ID = 0x55, 0x41

PAYLOAD_LEN = 63
REQUEST = bytes([0x65, 0x01]).ljust(PAYLOAD_LEN, b"\0")

REPLY_ID = 0x65
DISCHARGING, CHARGING, FULL, ASLEEP = 2, 3, 4, 26
VALID_STATES = (DISCHARGING, CHARGING, FULL)

READ_MS = 400
PASSIVE_TRIES = 2
REPLY_TRIES = 3
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


def parse_status(resp, report_id: int) -> Optional[Reading]:
    """(level, charging, charging known) from a 0x41/0x55 status frame, or None."""
    if not resp:
        return None
    data = bytes(resp)
    if len(data) >= 4 and data[0] == report_id and data[1] == REPLY_ID:
        level, state = data[2], data[3]
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

    def _open(self, info: dict, prefix: str):
        dev = hid.device()
        try:
            dev.open_path(info["path"])
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"{prefix} open: {e}")
            return None
        self._diag.append(f"{prefix} {_caps(dev)}")
        return dev

    @staticmethod
    def _close(dev) -> None:
        try:
            dev.close()
        except Exception:
            pass

    def _read_frames(self, dev, prefix: str, report_id: int, tries: int) -> Optional[Reading]:
        for _ in range(tries):
            try:
                resp = dev.read(64, READ_MS)
            except (OSError, ValueError) as e:
                self._diag.append(f"{prefix} read: {e}")
                return None
            if not resp:
                continue
            self._diag.append(f"{prefix} frame: {hexdump(resp, 16)}")
            parsed = parse_status(resp, report_id)
            if parsed is not None:
                return parsed
            self._diag.append(f"{prefix} not a status frame")
        return None

    def _listen(self, info: dict, prefix_label: str, tries: int):
        prefix = f"  {prefix_label} {STATUS_PAGE:04x}:{STATUS_USAGE:04x}"
        dev = self._open(info, prefix)
        if dev is None:
            return None
        try:
            return self._read_frames(dev, prefix, STATUS_ID, tries)
        finally:
            self._close(dev)

    def _command(self, info: dict, prefix_label: str):
        prefix = f"  {prefix_label} {COMMAND_PAGE:04x}:{COMMAND_USAGE:04x}"
        dev = self._open(info, prefix)
        if dev is None:
            return None
        try:
            frame = bytes([COMMAND_ID]) + REQUEST
            try:
                sent = dev.write(frame)
            except (OSError, ValueError) as e:
                self._diag.append(f"{prefix} write: {e}")
                return None
            if sent is None or sent <= 0:
                self._diag.append(f"{prefix} write refused ({sent}){_error(dev)}")
                return None
            return self._read_frames(dev, f"{prefix} cmd", COMMAND_ID, REPLY_TRIES)
        finally:
            self._close(dev)

    def _query(self, source: Source, status_info, command_info):
        # 1) a frame the dongle may push on its own
        if status_info is not None:
            got = self._listen(status_info, f"{source.label} idle", PASSIVE_TRIES)
            if got is not None:
                return got
        # 2) the status request on the collection that has an output report
        if command_info is not None:
            got = self._command(command_info, f"{source.label} command")
            if got is not None:
                return got
            # 3) the same request may be answered on the status collection instead
            if status_info is not None:
                got = self._listen(status_info, f"{source.label} after command", REPLY_TRIES)
                if got is not None:
                    return got
        return None

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
            status_info = candidate(infos, source, STATUS_PAGE, STATUS_USAGE)
            command_info = candidate(infos, source, COMMAND_PAGE, COMMAND_USAGE)
            if status_info is None:
                self._diag.append(f"  {source.label} {STATUS_PAGE:04x}:{STATUS_USAGE:04x} absent")
            if command_info is None:
                self._diag.append(f"  {source.label} {COMMAND_PAGE:04x}:{COMMAND_USAGE:04x} absent")
            if status_info is None and command_info is None:
                continue
            try:
                parsed = self._query(source, status_info, command_info)
            except Exception as e:  # a broken collection must not fail the provider
                self._diag.append(f"  {source.label} unexpected: {e}")
                parsed = None
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
