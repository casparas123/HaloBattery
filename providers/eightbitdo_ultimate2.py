"""8BitDo Ultimate 2 Wireless in XInput mode (2dc8:310b), #461.

The controller appears as an Xbox pad, so providers/xinput.py owns the icon;
this module only supplies the battery reading.  SDL's 8BitDo driver reads the
controller's enhanced input report on the vendor collection:

    usage page 0xFF7A, usage 0x0001
    byte 14: bit 7 = charging, bits 0-6 = level in %

The same layout is used by the Pro 2/Pro 3 and is documented in
providers/eightbitdo.py.  The Ultimate 2's report is 34 bytes on firmware
v1.03 (12 bytes on v1.02, whose report has no battery field); the v1.03 report
can arrive about 250 ms after the controller wakes, so the reader listens for
a short window instead of sending anything.

Only input reports are read, and only from the vendor collection: the
XInput-facing 0001:0005 collection belongs to Windows' XInput stack, and
reading it there made XInput fail with "read error" and the pad disappear
(#461, test build 3). On the 2.4 GHz dongle the vendor collection stays silent
on a real controller, so the level is only available over Bluetooth, which the
Bluetooth provider reads.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import hexdump, log

VID = 0x2DC8
PID = 0x310B
PAGE, USAGE = 0xFF7A, 0x0001
# The XInput-facing collection. It is deliberately never opened: doing so breaks
# XInput with "read error" (#461, test build 3).
GAMEPAD_PAGE, GAMEPAD_USAGE = 0x0001, 0x0005
BATTERY_BYTE = 14
# SDL reads the power state only when the report is at least this long: the
# 34-byte v1.03 report is the one that carries byte 14 (shorter reports are
# the ordinary gamepad frame, whose byte 14 is input data).
MIN_REPORT = 34
WINDOW = 0.8
MAX_REPORTS = 32


@dataclass
class Reading:
    name: str
    level: int
    charging: bool
    approx: str = ""


def parse_report(data) -> Optional[Reading]:
    """A battery reading from an enhanced report, or None.

    Byte 14 is the same field SDL's 8BitDo driver reads.  An ordinary report
    padded with zeros has 0 there and is ignored; the controller is running,
    so a 0 must not become a low-battery alert.
    """
    if not data or len(data) < MIN_REPORT:
        return None
    level = data[BATTERY_BYTE] & 0x7F
    charging = bool(data[BATTERY_BYTE] & 0x80)
    if level == 0 or level > 100:
        return None
    return Reading("8BitDo Ultimate 2", level, charging)


def read_connected(diag: List[str]) -> Optional[Reading]:
    """Listen once for the controller's battery report; None if it is silent."""
    if hid is None:
        return None
    try:
        infos = hidlist.enumerate(VID)
    except Exception as e:  # pragma: no cover
        log.warning("hid.enumerate(8bitdo ultimate2): %s", e)
        return None
    mine = [d for d in infos if d.get("product_id") == PID]
    # Never open the XInput-facing collection: Windows' XInput stack owns it, and
    # reading it there makes XInput fail with "read error" and the pad vanish
    # (#461, test build 3). Only the vendor collection is read.
    for d in [x for x in mine if (x.get("usage_page"), x.get("usage")) == (PAGE, USAGE)]:
        page, usage = d.get("usage_page"), d.get("usage")
        where = f"{page:04x}:{usage:04x}" if page is not None and usage is not None else "?"
        diag.append(f"    [8BitDo Ultimate 2] trying iface={d.get('interface_number')} {where}")
        dev = hid.device()
        try:
            dev.open_path(d["path"])
        except (OSError, IOError) as e:
            diag.append(f"    [8BitDo Ultimate 2] open: {e}")
            continue
        try:
            try:
                dev.set_nonblocking(True)
            except Exception:
                pass
            deadline = time.time() + WINDOW
            seen = 0
            lengths = set()
            while time.time() < deadline and seen < MAX_REPORTS:
                try:
                    data = dev.read(64)
                except (OSError, ValueError) as e:
                    diag.append(f"    [8BitDo Ultimate 2] read: {e}")
                    break
                if not data:
                    time.sleep(0.01)
                    continue
                seen += 1
                lengths.add(len(data))
                got = parse_report(data)
                if got is not None:
                    diag.append(f"    [8BitDo Ultimate 2] report {data[0]:#04x} "
                                f"len={len(data)}: {hexdump(data, 32)} -> {got.level}%"
                                f"{' charging' if got.charging else ''}")
                    return got
            if not seen:
                diag.append(f"    [8BitDo Ultimate 2] nothing on {where}")
            else:
                diag.append(f"    [8BitDo Ultimate 2] {seen} report(s) on {where}, "
                            f"lengths {sorted(lengths)}, no level in byte {BATTERY_BYTE}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
    diag.append("    [8BitDo Ultimate 2] no matching collection")
    return None
